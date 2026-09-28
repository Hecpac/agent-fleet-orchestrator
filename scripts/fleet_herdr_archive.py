"""Herdr archives v2-v7 and opt-in v8 minimal physical-scope evidence.

The existing CMUX archive format remains unchanged. This format records a Git
tree (including uncommitted candidate changes) without creating a commit.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
from typing import Any

import fleet_acceptance
import fleet_archive_tree
import fleet_git_snapshot
import fleet_artifacts
import fleet_herdr_inference
import fleet_compiled
import fleet_json
import fleet_herdr_evidence
import fleet_herdr_permissions
import fleet_herdr_profile
import fleet_herdr_metrics
import fleet_herdr_sdd
import fleet_herdr_scope
import fleet_functional
import fleet_mission
import fleet_mission_state as state
import fleet_safe_paths

# Archive schema -> profile for versioned readers. Physical scope is an overlay
# schema on the one profile that admits it, not a profile of its own.
VERSIONED_ARCHIVE_PROFILES = {
    **{profile.archive_schema_version: profile for profile in fleet_herdr_profile.VERSIONED},
    fleet_herdr_scope.ARCHIVE_VERSION: fleet_herdr_profile.PHYSICAL_SCOPE_PROFILE,
}
READABLE_ARCHIVE_SCHEMAS = frozenset({2, 3, 4, 5, *VERSIONED_ARCHIVE_PROFILES})
# Functional evidence exists from legacy v4 and in every versioned schema.
FUNCTIONAL_ARCHIVE_SCHEMAS = frozenset({4, 5, *VERSIONED_ARCHIVE_PROFILES})

MAX_BYTES = 32 * 1024 * 1024
ROLES = {"lead", "worker", "reviewer", "verifier"}


class HerdrArchiveError(RuntimeError):
    pass


def _bytes(value: Any) -> bytes:
    return fleet_json.canonical_bytes(value) + b"\n"


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read(store: fleet_safe_paths.RootedFS, relative: Path) -> bytes:
    return store.read_regular(relative, directory_modes=(0o700,) * (len(relative.parts) - 1),
                              file_mode=0o600, require_single_link=True, max_bytes=MAX_BYTES)


def _write(store: fleet_safe_paths.RootedFS, relative: Path, content: bytes) -> None:
    try:
        store.atomic_write(relative, content,
            directory_modes=(0o700,) * (len(relative.parts) - 1), file_mode=0o600)
    except fleet_safe_paths.SafePathError as exc:
        raise HerdrArchiveError(f"immutable artifact differs or is unsafe: {relative}: {exc}") from exc


def _git(repo: Path, *args: str, environment: dict[str, str] | None = None, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        env=environment or fleet_git_snapshot.git_environment(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, input=input_bytes, timeout=60, check=False)
    if result.returncode:
        raise HerdrArchiveError(result.stderr.decode(errors="replace")[:2000])
    if len(result.stdout) > MAX_BYTES:
        raise HerdrArchiveError("Git snapshot exceeds size limit")
    return result.stdout


def snapshot(candidate_repo: Path, *, expected_base: str | None = None,
             selected_paths: list[str] | None = None) -> tuple[str, bytes, bytes]:
    """Use a private index; never stage into the candidate's real index."""
    repo = candidate_repo.resolve(strict=True)
    if Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve() != repo:
        raise HerdrArchiveError("candidate must be the exact Git root")
    base = _git(repo, "rev-parse", "HEAD").decode().strip()
    if expected_base is not None and base != expected_base:
        raise HerdrArchiveError("candidate HEAD differs from mission baseline")
    with tempfile.TemporaryDirectory(prefix="fleet-herdr-index-") as temporary:
        environment = {**fleet_git_snapshot.git_environment(), "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(repo, "read-tree", base, environment=environment)
        if selected_paths is None:
            _git(repo, "add", "--all", "--", ".", environment=environment)
        elif selected_paths:
            for name in selected_paths:
                fleet_herdr_scope.path(name)
            environment["GIT_LITERAL_PATHSPECS"] = "1"
            _git(repo, "add", "--all", "--force", "--pathspec-from-file=-", "--pathspec-file-nul",
                 environment=environment, input_bytes=b"\0".join(p.encode() for p in selected_paths) + b"\0")
        tree_sha = _git(repo, "write-tree", environment=environment).decode().strip()
        patch = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", base, tree_sha)
        _git(repo, "read-tree", base, environment=environment)
        if patch:
            _git(repo, "apply", "--cached", "--binary", "--whitespace=nowarn", "-", environment=environment, input_bytes=patch)
        if _git(repo, "write-tree", environment=environment).decode().strip() != tree_sha:
            raise HerdrArchiveError("patch does not reproduce frozen tree from baseline")
    object_format = _git(repo, "rev-parse", "--show-object-format").decode().strip()
    tree = fleet_git_snapshot.raw_tree_tar(repo, tree_sha, object_format)
    if fleet_archive_tree.tree_hash_from_tar(tree, object_format) != tree_sha:
        raise HerdrArchiveError("snapshot Git tree proof failed")
    if _git(repo, "rev-parse", "HEAD").decode().strip() != base:
        raise HerdrArchiveError("candidate HEAD changed during snapshot")
    return tree_sha, tree, patch


def freeze(runs_dir: Path, mission_id: str, candidate_repo: Path) -> dict[str, Any]:
    """Freeze the candidate before reviewers run; replay rejects later drift."""
    compiled, current = fleet_mission.load_mission_compiled(runs_dir, mission_id, mode="effect")
    root = Path("missions") / mission_id
    if _git(candidate_repo, "rev-parse", "HEAD").decode().strip() != current["base_sha"]:
        raise HerdrArchiveError("candidate HEAD differs from mission baseline")
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        options = fleet_json.loads(_read(store, root / "runtime-options.json"))
    scope_contract = fleet_herdr_scope.validate_binding(current, options)
    selected = None
    if scope_contract is not None:
        baseline, observed = fleet_herdr_scope.inspect(runs_dir, current, candidate_repo, scope_contract)
        selected = sorted(set(baseline["tracked_paths"]) | set(observed["delivery_paths"]))
    tree_sha, tree, patch = snapshot(candidate_repo, expected_base=current["base_sha"], selected_paths=selected)
    if scope_contract is not None:
        fleet_herdr_scope.inspect(runs_dir, current, candidate_repo, scope_contract, tree_sha=tree_sha, tree=tree)
    frozen = {
        "schema_version": 1, "mission_id": mission_id,
        "compiled_digest": compiled["compiled_digest"], "base_sha": current["base_sha"],
        "candidate_repo": str(candidate_repo.resolve(strict=True)), "tree_sha": tree_sha,
        "tree_artifact_id": fleet_artifacts.put_bytes(runs_dir, mission_id, tree)["artifact_id"],
        "patch_artifact_id": fleet_artifacts.put_bytes(runs_dir, mission_id, patch)["artifact_id"],
    }
    try:
        sdd = fleet_herdr_sdd.verify(runs_dir, current)
    except state.MissionStateError as exc:
        raise HerdrArchiveError(f"candidate freeze SDD binding: {exc}") from exc
    if sdd is not None:
        frozen["sdd_plan_sha256"] = sdd["plan_sha256"]
    if scope_contract is not None:
        frozen.update(scope_contract_sha256=current[fleet_herdr_scope.FIELD],
                      scope_baseline_artifact_id=current["herdr_scope_baseline"]["baseline_artifact_id"])
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        _write(store, root / "candidate-freeze.json", _bytes(frozen))
    return frozen


def freeze_research(runs_dir: Path, mission_id: str, candidate_repo: Path,
                    *, profile_digest: str) -> dict[str, Any]:
    """Select the immutable tree Research investigates, before Build admission."""
    compiled, current = fleet_mission.load_mission_compiled(runs_dir, mission_id, mode="effect")
    try:
        profile = fleet_herdr_profile.resolve_profile(compiled)
    except fleet_herdr_profile.ProfileError as exc:
        raise HerdrArchiveError(str(exc)) from exc
    if profile is not fleet_herdr_profile.RESEARCH or profile_digest != profile.digest:
        raise HerdrArchiveError("investigated snapshot profile binding mismatch")
    root = Path("missions") / mission_id
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        prior_raw = store.read_regular_optional(root / "research-snapshot.json",
            directory_modes=(0o700, 0o700), file_mode=0o600, max_bytes=MAX_BYTES)
    if prior_raw is not None:
        prior = fleet_json.loads(prior_raw)
        verify_research_snapshot(runs_dir, mission_id, prior,
                                 expected_profile_digest=profile_digest)
        return prior
    if any(a["request_key"] == "herdr:build" for a in current["admissions"].values()):
        raise HerdrArchiveError("a missing investigated snapshot cannot be selected after Build admission")
    tree_sha, tree, patch = snapshot(candidate_repo, expected_base=current["base_sha"])
    receipt = {"schema_version": 1, "kind": "herdr_investigated_snapshot",
        "mission_id": mission_id, "compiled_digest": compiled["compiled_digest"],
        "herdr_profile": profile.profile_id, "herdr_profile_sha256": profile.digest,
        "base_sha": current["base_sha"], "candidate_repo": str(candidate_repo.resolve(strict=True)),
        "tree_sha": tree_sha,
        "tree_artifact_id": fleet_artifacts.put_bytes(runs_dir, mission_id, tree)["artifact_id"],
        "patch_artifact_id": fleet_artifacts.put_bytes(runs_dir, mission_id, patch)["artifact_id"]}
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        _write(store, root / "research-snapshot.json", _bytes(receipt))
    verify_research_snapshot(runs_dir, mission_id, receipt,
                             expected_profile_digest=profile_digest)
    return receipt


def verify_research_snapshot(runs_dir: Path, mission_id: str, receipt: Any,
                             *, expected_profile_digest: str) -> dict[str, Any]:
    """Verify stored receipt and CAS without consulting the mutable candidate tree."""
    compiled, current = fleet_mission.load_mission_compiled(runs_dir, mission_id, mode="effect")
    try:
        profile = fleet_herdr_profile.resolve_profile(compiled)
        with fleet_safe_paths.RootedFS(runs_dir) as store:
            raw = _read(store, Path("missions") / mission_id / "research-snapshot.json")
        stored = fleet_json.loads(raw)
        if raw != _bytes(stored) or stored != receipt:
            raise HerdrArchiveError("investigated snapshot receipt changed")
        expected = {"schema_version": 1, "kind": "herdr_investigated_snapshot",
            "mission_id": mission_id, "compiled_digest": compiled["compiled_digest"],
            "herdr_profile": profile.profile_id, "herdr_profile_sha256": profile.digest,
            "base_sha": current["base_sha"]}
        if profile is not fleet_herdr_profile.RESEARCH or expected_profile_digest != profile.digest:
            raise HerdrArchiveError("unsupported investigated snapshot profile")
        if any(stored.get(key) != value for key, value in expected.items()):
            raise HerdrArchiveError("investigated snapshot identity mismatch")
        if (set(stored) != set(expected) | {"candidate_repo", "tree_sha", "tree_artifact_id", "patch_artifact_id"}
                or not state.GIT_OID.fullmatch(str(stored.get("tree_sha", "")))
                or any(not state.SHA256.fullmatch(str(stored.get(key, "")))
                       for key in ("tree_artifact_id", "patch_artifact_id"))):
            raise HerdrArchiveError("investigated snapshot receipt schema is invalid")
        tree = fleet_artifacts.get_bytes(runs_dir, mission_id, stored["tree_artifact_id"])
        fleet_artifacts.get_bytes(runs_dir, mission_id, stored["patch_artifact_id"])
        object_format = "sha1" if len(stored["tree_sha"]) == 40 else "sha256"
        if fleet_archive_tree.tree_hash_from_tar(tree, object_format) != stored["tree_sha"]:
            raise HerdrArchiveError("investigated snapshot tree CAS is invalid")
        return stored
    except (fleet_herdr_profile.ProfileError, fleet_json.FleetJSONError,
            fleet_artifacts.ArtifactError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, HerdrArchiveError):
            raise
        raise HerdrArchiveError("investigated snapshot evidence is invalid") from exc


def create(runs_dir: Path, mission_id: str, candidate_repo: Path,
           role_results: dict[str, Any], backend_state: dict[str, Any]) -> dict[str, Any]:
    compiled, current = fleet_mission.load_mission_compiled(runs_dir, mission_id, mode="effect")
    root = Path("missions") / mission_id
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        if (not current.get("herdr_archive_selection")
                and "herdr-archive" in store.list_directory(root, directory_modes=(0o700, 0o700))
                and store.read_regular_optional(root / "herdr-archive" / "archive-index.json",
                    directory_modes=(0o700, 0o700, 0o700), file_mode=0o600, max_bytes=MAX_BYTES) is not None):
            archived_roles = fleet_json.loads(_read(store, root / "herdr-archive" / "role-results.json"))
            archived_backend = fleet_json.loads(_read(store, root / "herdr-archive" / "backend.json"))
            if archived_roles != role_results or archived_backend != backend_state or backend_state.get("mission_id") != mission_id or backend_state.get("compiled_digest") != compiled["compiled_digest"]:
                raise HerdrArchiveError("archive recovery inputs differ")
            archived_freeze = fleet_json.loads(_read(store, root / "herdr-archive" / "candidate-freeze.json"))
            if str(candidate_repo.resolve(strict=False)) != archived_freeze["candidate_repo"]:
                raise HerdrArchiveError("archive recovery candidate path differs")
            return verify(runs_dir, mission_id, require_anchor=False)
    # Reject historical partials before touching the candidate or preparing CAS.
    # A concurrent producer may already own the publication; its selection wins.
    with state.MissionTransaction(runs_dir, mission_id) as transaction:
        selected = transaction.current_state.get("herdr_archive_selection")
        if selected is None:
            _require_unpublished(runs_dir, mission_id)
            _require_archive_inputs(transaction.current_state, role_results, backend_state, compiled)
    if selected is None:
        frozen = freeze(runs_dir, mission_id, candidate_repo)
        with state.MissionTransaction(runs_dir, mission_id) as transaction:
            current = transaction.current_state
            selected = current.get("herdr_archive_selection")
            if selected is None:
                _require_unpublished(runs_dir, mission_id)
                compiled = fleet_mission._load_compiled_for_snapshot(
                    runs_dir, mission_id, current, mode="effect")
                # The strict transaction parser guarantees these canonical bytes
                # are exactly the history from which current_state was derived.
                ledger = fleet_json.canonical_jsonl(transaction.events)
                contents, index = _capture_contents(runs_dir, mission_id, role_results,
                    backend_state, compiled, current, frozen, ledger)
                for content in contents.values():
                    fleet_artifacts.put_bytes(runs_dir, mission_id, content)
                pin = fleet_artifacts.put_bytes(runs_dir, mission_id, _bytes(index))
                transaction.append_event(kind="herdr_archive_selected", actor="CONTROL",
                    idempotency_key="herdr:archive:select", payload={
                        "compiled_digest": compiled["compiled_digest"],
                        "ledger_head": current["head_sha256"], "index_artifact_id": pin["artifact_id"]})
    index_raw, contents = _selected_contents(runs_dir, mission_id)
    selected_backend = fleet_json.loads(contents["backend.json"])
    if (role_results != fleet_json.loads(contents["role-results.json"])
            or any(backend_state.get(key) != selected_backend.get(key)
                   for key in ("mission_id", "compiled_digest", "session", "generation"))):
        raise HerdrArchiveError("archive recovery inputs differ")
    if str(candidate_repo.resolve(strict=False)) != fleet_json.loads(contents["candidate-freeze.json"])["candidate_repo"]:
        raise HerdrArchiveError("archive recovery candidate path differs")
    return _publish_selected(runs_dir, mission_id, index_raw, contents)


def _require_unpublished(runs_dir: Path, mission_id: str) -> None:
    root = Path("missions") / mission_id
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        if ("herdr-archive" in store.list_directory(root, directory_modes=(0o700, 0o700))
                and store.list_directory(root / "herdr-archive", directory_modes=(0o700,) * 3)):
            raise HerdrArchiveError("legacy partial archive has no durable snapshot selection; publication remains pending")


def _require_archive_inputs(current, role_results, backend_state, compiled):
    if current["status"] != "completing" or any(a.get("active") for a in current["admissions"].values()):
        raise HerdrArchiveError("archive requires completing mission with finalized admissions")
    try:
        profile = fleet_herdr_profile.resolve_profile(compiled)
    except fleet_herdr_profile.ProfileError as exc:
        raise HerdrArchiveError(str(exc)) from exc
    if set(role_results) != profile.result_roles:
        raise HerdrArchiveError("archive requires every selected profile role result")
    if set(current.get("risk_categories", [])) & {"credentials", "private_data", "regulated"}:
        raise HerdrArchiveError("sensitive/regulated archive requires separately authorized audit lane")
    if backend_state.get("mission_id") != current["mission_id"] or backend_state.get("compiled_digest") != compiled["compiled_digest"]:
        raise HerdrArchiveError("backend archive binding mismatch")


def _capture_contents(runs_dir, mission_id, role_results, backend_state, compiled, current, frozen, ledger):
    root = Path("missions") / mission_id
    _require_archive_inputs(current, role_results, backend_state, compiled)
    try:
        profile = fleet_herdr_profile.resolve_profile(compiled)
    except fleet_herdr_profile.ProfileError as exc:
        raise HerdrArchiveError(str(exc)) from exc
    contents: dict[str, bytes] = {"ledger.jsonl": ledger}
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        for name in ("compiled-workflow.json", "runtime-options.json", "creation-request.json", "objective.txt", "candidate-freeze.json"):
            contents[name] = _read(store, root / name)
        if profile is fleet_herdr_profile.RESEARCH:
            contents["research-snapshot.json"] = _read(store, root / "research-snapshot.json")
    contents["backend.json"] = _bytes(backend_state)
    contents["role-results.json"] = _bytes(role_results)
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        for artifact_id in store.list_directory(root / "artifacts", directory_modes=(0o700, 0o700, 0o700)):
            if not state.SHA256.fullmatch(artifact_id):
                # Uncommitted CAS preparation can survive a crash. It is not an
                # artifact input; retain it and recognize only the exact CAS intent.
                pending = _read(store, root / "artifacts" / artifact_id)
                if artifact_id == fleet_safe_paths._atomic_pending_name(_sha(pending), pending):
                    continue
                raise HerdrArchiveError("invalid archive CAS entry")
            contents[f"artifacts/{artifact_id}"] = fleet_artifacts.get_bytes(runs_dir, mission_id, artifact_id)
    for role, result in role_results.items():
        if not isinstance(result, dict) or not isinstance(result.get("artifact_id"), str):
            raise HerdrArchiveError(f"{role} result lacks CAS artifact")
        contents[f"results/{role}.txt"] = fleet_artifacts.get_bytes(runs_dir, mission_id, result["artifact_id"])
    contents["writer/final-tree.tar"] = fleet_artifacts.get_bytes(runs_dir, mission_id, frozen["tree_artifact_id"])
    contents["writer/change.patch"] = fleet_artifacts.get_bytes(runs_dir, mission_id, frozen["patch_artifact_id"])
    if profile is fleet_herdr_profile.RESEARCH:
        research = fleet_json.loads(contents["research-snapshot.json"])
        verify_research_snapshot(runs_dir, mission_id, research,
                                 expected_profile_digest=profile.digest)
        contents["research/investigated-tree.tar"] = fleet_artifacts.get_bytes(
            runs_dir, mission_id, research["tree_artifact_id"])
        contents["research/change.patch"] = fleet_artifacts.get_bytes(
            runs_dir, mission_id, research["patch_artifact_id"])
    # Reject invalid role evidence before publishing any archive files.
    for role, result in role_results.items():
        matches = [a for a in current["admissions"].values() if a["recipient_instance"] == role and a["run_id"] == result.get("run_id")]
        if len(matches) != 1:
            raise HerdrArchiveError("archive role lacks exact admission")
        admission = matches[0]
        recorded = admission.get("result")
        if (admission["phase"] != "finalized" or not recorded or admission["terminal"]["status"] != "succeeded"
                or result.get("status") != "PASS" or recorded["artifact_id"] != result.get("result_artifact_id")):
            raise HerdrArchiveError("archive role result/admission mismatch")
        if fleet_json.loads(contents["artifacts/" + recorded["artifact_id"]]) != {k: v for k, v in result.items() if k != "result_artifact_id"}:
            raise HerdrArchiveError("archive role envelope differs from ledger CAS")
    options = fleet_json.loads(contents["runtime-options.json"])
    scope_contract = fleet_herdr_scope.validate_binding(current, options)
    if scope_contract is not None:
        baseline, receipt = fleet_herdr_scope.inspect(runs_dir, current, Path(frozen["candidate_repo"]),
            scope_contract, tree_sha=frozen["tree_sha"], tree=contents["writer/final-tree.tar"])
        contents["scope/baseline.json"] = fleet_json.canonical_bytes(baseline)
        contents["scope/result.json"] = _bytes(receipt)
    try:
        fleet_herdr_inference.require_settled(current)
        fleet_herdr_inference.verify_evidence(current, lambda pin: contents["artifacts/" + pin])
    except (fleet_herdr_inference.InferenceError, KeyError, TypeError, ValueError) as exc:
        raise HerdrArchiveError("invalid inference broker evidence") from exc
    contract = options.get("acceptance_contract")
    acceptance = (fleet_acceptance.evaluate(contract, contents["writer/final-tree.tar"],
        mission_id=mission_id, final_sha=frozen["tree_sha"]) if contract is not None else
        {"status": "not_evaluated", "scope": "no_artifact_contract", "mission_id": mission_id})
    contents["acceptance-result.json"] = _bytes(acceptance)
    functional = fleet_functional.archived_receipt(current, frozen,
        lambda key: contents["artifacts/" + key])
    if functional is not None:
        contents["functional-result.json"] = _bytes(functional)
    try:
        sdd = fleet_herdr_sdd.archived_evidence(runs_dir, current)
    except state.MissionStateError as exc:
        raise HerdrArchiveError(f"archive SDD evidence: {exc}") from exc
    if sdd is not None:
        contents["sdd/plan.json"] = sdd["plan"]
        contents["sdd/binding.json"] = sdd["binding"]
    # Reject missing or incompatible recorded permissions before staging files.
    attest_admissions(contents, current, frozen["candidate_repo"], profile=profile)
    versioned = profile is not fleet_herdr_profile.LEGACY
    schema_version = profile.archive_schema_version if versioned else (5 if sdd is not None else (4 if functional is not None else 3))
    if scope_contract is not None:
        schema_version = fleet_herdr_scope.ARCHIVE_VERSION
    index = {"schema_version": schema_version, "permissions_policy_version": profile.permissions_policy_version if versioned else (2 if options.get("herdr_capsule_manifest") else 1), "backend": "herdr", "mission_id": mission_id,
        "compiled_digest": compiled["compiled_digest"], "ledger_head": current["head_sha256"],
        "base_sha": current["base_sha"], "final_tree_sha": frozen["tree_sha"],
        "entries": {name: {"sha256": _sha(content), "bytes": len(content)} for name, content in sorted(contents.items())}}
    if versioned:
        index.update({"herdr_profile": profile.profile_id,
                      "herdr_profile_sha256": profile.digest})
    if profile is fleet_herdr_profile.RESEARCH:
        index["investigated_tree_sha"] = research["tree_sha"]
    if sum(map(len, contents.values())) > 128 * 1024 * 1024:
        raise HerdrArchiveError("archive exceeds size limit")
    return contents, index


def _selected_contents(runs_dir: Path, mission_id: str) -> tuple[bytes, dict[str, bytes]]:
    current = fleet_mission.load_state(runs_dir, mission_id)
    selected = current.get("herdr_archive_selection")
    if selected is None:
        raise HerdrArchiveError("archive has no durable snapshot selection")
    raw = fleet_artifacts.get_bytes(runs_dir, mission_id, selected["index_artifact_id"])
    index = fleet_json.loads(raw)
    if (not isinstance(index, dict) or raw != _bytes(index)
            or index.get("mission_id") != mission_id or index.get("backend") != "herdr"
            or index.get("compiled_digest") != selected["compiled_digest"]
            or index.get("ledger_head") != selected["ledger_head"]
            or not isinstance(index.get("entries"), dict) or len(index["entries"]) > 1000):
        raise HerdrArchiveError("archive snapshot selection binding mismatch")
    contents = {}
    for name, proof in index["entries"].items():
        fleet_archive_tree.safe_relative(name)
        content = fleet_artifacts.get_bytes(runs_dir, mission_id, proof["sha256"])
        if proof != {"sha256": _sha(content), "bytes": len(content)}:
            raise HerdrArchiveError("archive selected content differs")
        contents[name] = content
    return raw, contents


def _publish_selected(runs_dir, mission_id, index_raw, contents):
    root = Path("missions") / mission_id
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        for name, content in sorted(contents.items()):
            _write(store, root / "herdr-archive" / name, content)
        _write(store, root / "herdr-archive" / "archive-index.json", index_raw)
    return verify(runs_dir, mission_id, require_anchor=False)


def recover(runs_dir: Path, mission_id: str) -> dict[str, Any]:
    """Continue one selected publication using CAS, without live execution inputs."""
    index_raw, contents = _selected_contents(runs_dir, mission_id)
    return _publish_selected(runs_dir, mission_id, index_raw, contents)


def attest_admissions(contents: dict[str, bytes], current: dict[str, Any], cwd: str,
                      *, profile=fleet_herdr_profile.LEGACY) -> dict[str, Any]:
    """Check every selected turn, including planning and synthesis admissions."""
    expected = {f"herdr:{stage}": instance for stage, instance, _ in profile.stages}
    admissions = list(current["admissions"].values())
    if len(admissions) != len(profile.stages) or {a["request_key"] for a in admissions} != set(expected):
        raise HerdrArchiveError("permission attestation requires every profile stage admission")

    def read(digest):
        try:
            raw = contents["artifacts/" + digest]
        except (KeyError, TypeError) as exc:
            raise HerdrArchiveError("permission attestation artifact is missing") from exc
        if _sha(raw) != digest:
            raise HerdrArchiveError("permission attestation CAS mismatch")
        return raw

    options = fleet_json.loads(contents["runtime-options.json"])
    capsule_manifest = options.get("herdr_capsule_manifest")
    proofs = {}
    usage_by_run = {}
    for admission in admissions:
        role = expected[admission["request_key"]]
        recorded = admission.get("result")
        if (admission["recipient_instance"] != role or admission["writer"] is not (role == "worker")
                or admission["phase"] != "finalized" or admission["active"] or not recorded
                or admission["terminal"]["status"] != "succeeded"):
            raise HerdrArchiveError("permission attestation admission is incompatible")
        result = fleet_json.loads(read(recorded["artifact_id"]))
        if (not isinstance(result, dict) or result.get("instance_id") != role
                or result.get("run_id") != admission["run_id"] or result.get("mission_id") != current["mission_id"]
                or result.get("status") != "PASS" or recorded.get("provider") != "openai"
                or recorded.get("model") != dict((m[0], m[2]) for m in profile.members)[role]):
            raise HerdrArchiveError("permission attestation result/admission mismatch")
        try:
            proofs[admission["run_id"]] = fleet_herdr_evidence.verify_result(result,
                read_artifact=read, role=role, cwd=cwd, prompt_sha256=admission["task_sha256"],
                capsule_manifest=capsule_manifest, current=current,
                permission_version=profile.permissions_policy_version)
        except fleet_herdr_evidence.EvidenceError as exc:
            raise HerdrArchiveError(f"{admission['request_key']} permission evidence: {exc}") from exc
        evidence = result["evidence"]
        baseline = None
        baseline_id = evidence.get("usage_baseline_artifact_id")
        if baseline_id is not None:
            try:
                baseline = fleet_json.loads(read(baseline_id))
            except (KeyError, TypeError, fleet_json.FleetJSONError) as exc:
                raise HerdrArchiveError("usage baseline CAS is unavailable") from exc
            if (not isinstance(baseline, dict)
                    or baseline.get("mission_id") != current["mission_id"]
                    or baseline.get("run_id") != admission["run_id"]
                    or baseline.get("prompt_sha256") != admission["task_sha256"]
                    or baseline.get("generation") != evidence.get("generation")
                    or baseline.get("agent_session") not in (None, evidence["agent_session"])):
                raise HerdrArchiveError("usage baseline binding mismatch")
        transcript = fleet_json.load_jsonl(read(evidence["transcript_artifact_id"]))
        usage_by_run[admission["run_id"]] = fleet_herdr_metrics.usage(
            transcript, result["turn_id"], baseline,
            baseline_frontier=evidence.get("usage_baseline_frontier"))
    return {"status": "attested", "policy_version": 2 if capsule_manifest else profile.permissions_policy_version,
            "scope": "external_seatbelt_capsule" if capsule_manifest else "recorded_codex_turn_configuration",
            "runs": len(proofs), "by_run": proofs, "usage_by_run": usage_by_run}


def verify(runs_dir: Path, mission_id: str, *, require_anchor: bool = True,
           attest_permissions: bool = False, for_completion: bool = False) -> dict[str, Any]:
    """Verify offline from durable files, including the live ledger anchor."""
    mission_id = state.normalize_uuid(mission_id, "mission_id")
    archive = Path("missions") / mission_id / "herdr-archive"
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        raw = _read(store, archive / "archive-index.json")
        index = fleet_json.loads(raw)
        if not isinstance(index, dict) or raw != _bytes(index) or (type(index.get("schema_version")) is not int or index["schema_version"] not in READABLE_ARCHIVE_SCHEMAS) or index.get("backend") != "herdr" or index.get("mission_id") != mission_id:
            raise HerdrArchiveError("invalid Herdr archive index")
        if index["schema_version"] >= 3 and (type(index.get("permissions_policy_version")) is not int
                or index["permissions_policy_version"] not in {1, 2, 3, 4}):
            raise HerdrArchiveError("archive permission policy version is invalid")
        contents = {}
        entries = index.get("entries")
        if not isinstance(entries, dict) or len(entries) > 1000:
            raise HerdrArchiveError("invalid archive entry map")
        for name, proof in entries.items():
            fleet_archive_tree.safe_relative(name)
            content = _read(store, archive / name)
            if proof != {"sha256": _sha(content), "bytes": len(content)}:
                raise HerdrArchiveError(f"archive content changed: {name}")
            contents[name] = content
        compiled = fleet_compiled.loads(contents["compiled-workflow.json"], mode="read")
        try:
            profile = fleet_herdr_profile.resolve_profile(compiled)
        except fleet_herdr_profile.ProfileError as exc:
            raise HerdrArchiveError(str(exc)) from exc
        expected_profile = VERSIONED_ARCHIVE_PROFILES.get(index["schema_version"])
        if expected_profile is not None:
            if (profile is not expected_profile
                    or index.get("herdr_profile") != profile.profile_id
                    or index.get("herdr_profile_sha256") != profile.digest
                    or index.get("permissions_policy_version") != profile.permissions_policy_version):
                raise HerdrArchiveError("versioned archive profile binding mismatch")
        elif profile is not fleet_herdr_profile.LEGACY:
            if profile is fleet_herdr_profile.RESEARCH:
                raise HerdrArchiveError("Research profile requires archive schema v6")
            raise HerdrArchiveError("minimal profile requires archive schema v7")
        events = [fleet_json.loads(line) for line in contents["ledger.jsonl"].splitlines()]
        archived_state = state.derive_state(events)
        state.verify_events(events)
        # Required files are driven by durable evidence, not schema version:
        # SDD-only archives (schema 5) need no functional receipt.
        required = {"compiled-workflow.json", "runtime-options.json", "creation-request.json", "objective.txt", "ledger.jsonl", "candidate-freeze.json", "backend.json", "role-results.json", "writer/final-tree.tar", "writer/change.patch", "acceptance-result.json"} | {f"results/{role}.txt" for role in profile.result_roles}
        if archived_state.get("functional_policy") is not None:
            required.add("functional-result.json")
        ledger_sdd = archived_state.get("sdd_plan_sha256")
        scope_required = archived_state.get(fleet_herdr_scope.FIELD) is not None
        if scope_required != (index["schema_version"] == fleet_herdr_scope.ARCHIVE_VERSION):
            raise HerdrArchiveError("scope archive version/creation binding mismatch")
        if scope_required:
            required.update({"scope/baseline.json", "scope/result.json"})
        if index["schema_version"] == 5 and ledger_sdd is None:
            raise HerdrArchiveError("archive schema v5 lacks its ledger SDD plan pin")
        if ledger_sdd is not None:
            required.update({"sdd/plan.json", "sdd/binding.json"})
        if profile is fleet_herdr_profile.RESEARCH:
            required.update({"research-snapshot.json", "research/investigated-tree.tar",
                             "research/change.patch"})
        artifact_names = {name for name in contents if name.startswith("artifacts/")}
        if set(contents) - artifact_names != required:
            raise HerdrArchiveError("archive required files differ")
        for name in artifact_names:
            if name != "artifacts/" + _sha(contents[name]):
                raise HerdrArchiveError("archived CAS identity mismatch")
        try:
            fleet_herdr_inference.require_settled(archived_state)
            inference_evidence = fleet_herdr_inference.verify_evidence(archived_state,
                lambda pin: contents["artifacts/" + pin])
        except (fleet_herdr_inference.InferenceError, KeyError, TypeError, ValueError) as exc:
            raise HerdrArchiveError("invalid archived inference broker evidence") from exc
        if (archived_state["mission_id"] != mission_id or archived_state["head_sha256"] != index["ledger_head"]
                or archived_state["compiled_digest"] != compiled["compiled_digest"] or index["compiled_digest"] != compiled["compiled_digest"]
                or archived_state["base_sha"] != index["base_sha"]):
            raise HerdrArchiveError("archive ledger/compiled binding mismatch")
        live_events = state.read_events(state.ledger_path(runs_dir, mission_id), expected_mission_id=mission_id)
        live_state = state.derive_state(live_events)
        if live_events[:len(events)] != events:
            raise HerdrArchiveError("archive ledger is not a prefix of the mission")
        anchors = [e for e in live_events if e["kind"] == "archive_created"]
        if (len(anchors) > 1 or (require_anchor and len(anchors) != 1)
                or any(e["payload"]["sha256"] != _sha(raw) for e in anchors)):
            raise HerdrArchiveError("archive index differs from ledger anchor")
        # Pre-anchor durable authority: the snapshot selection event pins the
        # index artifact, so a rewritten index cannot pass merely because
        # archive_created is not appended yet. This requirement is scoped to
        # SDD evidence; legacy v2 resealed archives keep their existing rules.
        selection = live_state.get("herdr_archive_selection")
        sdd_scoped = index["schema_version"] >= 5 or ledger_sdd is not None
        if sdd_scoped and selection is not None and (
                selection.get("index_artifact_id") != _sha(raw)
                or selection.get("compiled_digest") != compiled["compiled_digest"]
                or selection.get("ledger_head") != index["ledger_head"]):
            raise HerdrArchiveError("archive index differs from durable snapshot selection")
        if for_completion and index["schema_version"] >= 5 and selection is None:
            raise HerdrArchiveError("SDD archive completion requires its durable snapshot selection")
        if ledger_sdd is not None:
            if not isinstance(ledger_sdd, str):
                raise HerdrArchiveError("SDD archive lacks its ledger plan pin")
            try:
                fleet_herdr_sdd.verify_archived(mission_id, ledger_sdd,
                    contents["sdd/plan.json"], contents["sdd/binding.json"])
            except state.MissionStateError as exc:
                raise HerdrArchiveError(f"archive SDD evidence: {exc}") from exc
        finalization_policy = None
        if for_completion:
            finalization_policy = live_state.get("herdr_finalization_policy")
            expected_policy = fleet_herdr_permissions.finalization_policy(compiled["compiled_digest"],
                capsule=bool(fleet_json.loads(contents["runtime-options.json"]).get("herdr_capsule_manifest")),
                profile=profile)
            if (not finalization_policy or
                    {k: v for k, v in finalization_policy.items() if k != "event_sha256"} != expected_policy):
                raise HerdrArchiveError("archive completion lacks its durable finalization policy")
            if index["schema_version"] < finalization_policy["minimum_archive_schema_version"]:
                raise HerdrArchiveError("historical archive schema cannot authorize new completion")
            if live_state.get("functional_policy") != archived_state.get("functional_policy"):
                raise HerdrArchiveError("archive cannot omit durable functional policy")
            if live_state.get("inference_policies") != archived_state.get("inference_policies"):
                raise HerdrArchiveError("archive cannot omit durable inference requests")
        frozen = fleet_json.loads(contents["candidate-freeze.json"])
        backend = fleet_json.loads(contents["backend.json"])
        if frozen.get("sdd_plan_sha256") != ledger_sdd:
            raise HerdrArchiveError("archive candidate freeze/plan binding mismatch")
        if (frozen.get("mission_id") != mission_id or frozen.get("compiled_digest") != compiled["compiled_digest"]
                or frozen.get("base_sha") != index["base_sha"] or backend.get("mission_id") != mission_id
                or backend.get("compiled_digest") != compiled["compiled_digest"]):
            raise HerdrArchiveError("archive backend/freeze identity mismatch")
        tree = contents["writer/final-tree.tar"]
        object_format = "sha1" if len(index["final_tree_sha"]) == 40 else "sha256"
        if (fleet_archive_tree.tree_hash_from_tar(tree, object_format) != index["final_tree_sha"]
                or frozen["tree_sha"] != index["final_tree_sha"] or frozen["tree_artifact_id"] != _sha(tree)
                or frozen["patch_artifact_id"] != _sha(contents["writer/change.patch"])):
            raise HerdrArchiveError("archive candidate tree binding mismatch")
        research = None
        research_tree = None
        if profile is fleet_herdr_profile.RESEARCH:
            research = fleet_json.loads(contents["research-snapshot.json"])
            expected_research = {"schema_version": 1, "kind": "herdr_investigated_snapshot",
                "mission_id": mission_id, "compiled_digest": compiled["compiled_digest"],
                "herdr_profile": profile.profile_id, "herdr_profile_sha256": profile.digest,
                "base_sha": index["base_sha"]}
            if (set(research) != set(expected_research) | {"candidate_repo", "tree_sha", "tree_artifact_id", "patch_artifact_id"}
                    or any(research.get(key) != value for key, value in expected_research.items())
                    or research.get("tree_sha") != index.get("investigated_tree_sha")
                    or research.get("tree_artifact_id") != _sha(contents["research/investigated-tree.tar"])
                    or research.get("patch_artifact_id") != _sha(contents["research/change.patch"])):
                raise HerdrArchiveError("archived investigated snapshot binding mismatch")
            research_tree = contents["research/investigated-tree.tar"]
            research_format = "sha1" if len(research["tree_sha"]) == 40 else "sha256"
            if fleet_archive_tree.tree_hash_from_tar(research_tree, research_format) != research["tree_sha"]:
                raise HerdrArchiveError("archived investigated tree proof failed")
        roles = fleet_json.loads(contents["role-results.json"])
        if set(roles) != profile.result_roles or any(roles[role].get("artifact_id") != _sha(contents[f"results/{role}.txt"]) for role in profile.result_roles):
            raise HerdrArchiveError("archive role artifact binding mismatch")
        members = {member["instance_id"]: member for member in
                   [compiled["resolved"].get("lead"), *compiled["resolved"]["instances"]]
                   if member is not None}
        for role, result in roles.items():
            matches = [a for a in archived_state["admissions"].values() if a["recipient_instance"] == role and a["run_id"] == result.get("run_id")]
            if len(matches) != 1:
                raise HerdrArchiveError("archive role lacks exact admission")
            admission = matches[0]
            recorded = admission.get("result")
            if (admission["phase"] != "finalized" or admission["active"] or not recorded
                    or admission["terminal"]["status"] != "succeeded" or result.get("status") != "PASS"
                    or admission["writer"] is not (role == "worker")
                    or result.get("mission_id") != mission_id or result.get("instance_id") != role
                    or type(result.get("schema_version")) is not int or result["schema_version"] != 1
                    or recorded["artifact_id"] != result.get("result_artifact_id")
                    or recorded["model"] != members[role]["model"] or recorded["provider"] != "openai"):
                raise HerdrArchiveError("archive role result/admission mismatch")
            envelope = contents.get("artifacts/" + recorded["artifact_id"])
            if envelope is None or fleet_json.loads(envelope) != {k: v for k, v in result.items() if k != "result_artifact_id"}:
                raise HerdrArchiveError("archive role envelope differs from ledger CAS")
            if role == "research":
                try:
                    research_task = fleet_json.loads(contents["artifacts/" + admission["task_sha256"]])
                except (KeyError, fleet_json.FleetJSONError) as exc:
                    raise HerdrArchiveError("Research task CAS is unavailable") from exc
                if (research_task.get("stage") != "research"
                        or research_task.get("investigated_snapshot") != research
                        or research_task.get("frozen_candidate") != research
                        or research_task.get("result_contract", {}).get("candidate_tree_sha") != research["tree_sha"]
                        or result.get("candidate_tree_sha") != research["tree_sha"]):
                    raise HerdrArchiveError("Research task/result does not authorize investigated snapshot")
            authored = {k: v for k, v in result.items() if k not in {"artifact_id", "result_artifact_id", "backend_result_artifact_id", "turn_id", "evidence", "evidence_artifact_ids"}}
            if fleet_json.loads(contents[f"results/{role}.txt"]) != authored:
                raise HerdrArchiveError("archive raw final differs from role result")
            backend_id = result.get("backend_result_artifact_id")
            evidence = result.get("evidence")
            if (not isinstance(backend_id, str) or "artifacts/" + backend_id not in contents
                    or not isinstance(evidence, dict)):
                raise HerdrArchiveError("archive missing durable backend evidence")
            expected_backend = {k: v for k, v in result.items() if k not in {"result_artifact_id", "backend_result_artifact_id", "evidence_artifact_ids"}}
            if fleet_json.loads(contents["artifacts/" + backend_id]) != expected_backend:
                raise HerdrArchiveError("archive backend envelope binding mismatch")
            transcript_id = evidence.get("transcript_artifact_id")
            session = evidence.get("agent_session")
            if (not isinstance(transcript_id, str) or "artifacts/" + transcript_id not in contents
                    or evidence.get("transcript_sha256") != transcript_id
                    or evidence.get("herdr_session") != backend.get("session")
                    or evidence.get("context_artifact_id") != backend.get("context_artifact_id")
                    or evidence.get("prompt_sha256") != admission["task_sha256"]
                    or not isinstance(session, dict) or session.get("kind") != "id"):
                raise HerdrArchiveError("archive transcript evidence binding mismatch")
            try:
                capsule_manifest = fleet_json.loads(contents["runtime-options.json"]).get("herdr_capsule_manifest")
                if capsule_manifest is not None:
                    fleet_herdr_evidence.verify_result(result,
                        read_artifact=lambda pin: contents["artifacts/" + pin], role=role,
                        cwd=frozen["candidate_repo"], prompt_sha256=admission["task_sha256"],
                        capsule_manifest=capsule_manifest, current=archived_state)
                else:
                    fleet_herdr_evidence.verify_transcript(contents["artifacts/" + transcript_id],
                        agent_session=session["value"], model=members[role]["model"], turn_id=result.get("turn_id"),
                        prompt_sha256=admission["task_sha256"], final_bytes=contents[f"results/{role}.txt"])
            except (fleet_herdr_evidence.EvidenceError, KeyError) as exc:
                raise HerdrArchiveError(f"archive transcript proof failed: {exc}") from exc
            artifacts = result.get("artifacts")
            evidence_ids = result.get("evidence_artifact_ids")
            if (not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 100
                    or not isinstance(evidence_ids, list) or len(artifacts) != len(evidence_ids)):
                raise HerdrArchiveError("archive requires nonempty bound role evidence")
            evidence_tree = research_tree if role == "research" else tree
            with tarfile.open(fileobj=io.BytesIO(evidence_tree), mode="r:") as tar:
                for artifact, digest in zip(artifacts, evidence_ids):
                    if (not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}
                            or not isinstance(artifact["path"], str) or not artifact["path"]
                            or PurePosixPath(artifact["path"]).is_absolute()
                            or any(p in {"", ".", "..", ".git"} for p in artifact["path"].split("/"))
                            or artifact["sha256"] != digest):
                        raise HerdrArchiveError("archive role evidence binding mismatch")
                    try:
                        member = tar.getmember(artifact["path"])
                    except KeyError as exc:
                        raise HerdrArchiveError("role evidence missing from final tree") from exc
                    if not member.isfile() or member.size > MAX_BYTES:
                        raise HerdrArchiveError("role evidence is not a bounded regular file")
                    stream = tar.extractfile(member)
                    if stream is None or _sha(stream.read(MAX_BYTES + 1)) != digest:
                        raise HerdrArchiveError("role evidence differs from final tree")
            for digest in [result["artifact_id"], admission["task_sha256"], *result.get("evidence_artifact_ids", [])]:
                if "artifacts/" + digest not in contents:
                    raise HerdrArchiveError("archive missing admission/result evidence")
            expected_tree_sha = research["tree_sha"] if role == "research" else index["final_tree_sha"]
            if role in {"lead", "reviewer", "verifier", "research"} and result.get("candidate_tree_sha") != expected_tree_sha:
                raise HerdrArchiveError("archive reviewer candidate binding mismatch")
        options = fleet_json.loads(contents["runtime-options.json"])
        creation = fleet_json.loads(contents["creation-request.json"])
        if (creation.get("runtime_options") != options or creation.get("mission_id") != mission_id
                or _sha(contents["objective.txt"]) != archived_state["objective_sha256"]
                or creation.get("request", {}).get("sdd_plan_sha256") != ledger_sdd):
            raise HerdrArchiveError("archive runtime/objective binding mismatch")
        scope_receipt = None
        try:
            fleet_herdr_scope.validate_binding(archived_state, options)
            if creation.get("request", {}).get(fleet_herdr_scope.FIELD) != archived_state.get(fleet_herdr_scope.FIELD):
                raise fleet_herdr_scope.ScopeError("scope creation request mismatch")
            if scope_required:
                scope_receipt = fleet_herdr_scope.verify_archived(archived_state, options, frozen,
                    fleet_json.loads(contents["scope/baseline.json"]),
                    fleet_json.loads(contents["scope/result.json"]), tree)
            elif fleet_herdr_scope.FIELD in frozen or "scope_baseline_artifact_id" in frozen:
                raise fleet_herdr_scope.ScopeError("unbound scope freeze")
        except (fleet_herdr_scope.ScopeError, KeyError, TypeError, ValueError) as exc:
            raise HerdrArchiveError("archive physical scope evidence invalid: " + str(exc)) from exc
        try:
            fleet_herdr_profile.validate_profile_binding(compiled, options, archived_state)
            fleet_herdr_profile.validate_creation_binding(
                compiled, creation.get("request"))
        except fleet_herdr_profile.ProfileError as exc:
            raise HerdrArchiveError("archive profile binding mismatch") from exc
        contract = options.get("acceptance_contract")
        fleet_acceptance.check_binding(creation["idempotency_key"], contract)
        acceptance = fleet_json.loads(contents["acceptance-result.json"])
        expected = (fleet_acceptance.evaluate(contract, tree, mission_id=mission_id, final_sha=index["final_tree_sha"])
            if contract is not None else {"status": "not_evaluated", "scope": "no_artifact_contract", "mission_id": mission_id})
        if acceptance != expected:
            raise HerdrArchiveError("archive acceptance receipt mismatch")
        functional = None
        functional_spec = options.get("functional_contract")
        if bool(functional_spec) != bool(archived_state.get("functional_policy")):
            raise HerdrArchiveError("archive functional options/policy mismatch")
        if functional_spec is not None:
            if (index["schema_version"] not in FUNCTIONAL_ARCHIVE_SCHEMAS or fleet_functional.digest(fleet_functional.validate(functional_spec))
                    != archived_state["functional_policy"]["spec_artifact_id"]):
                raise HerdrArchiveError("archive functional contract/schema mismatch")
            try:
                functional = fleet_functional.archived_receipt(archived_state, frozen,
                    lambda key: contents["artifacts/" + key])
            except (fleet_functional.FunctionalError, KeyError) as exc:
                raise HerdrArchiveError(f"archive functional evidence: {exc}") from exc
            if fleet_json.loads(contents["functional-result.json"]) != functional:
                raise HerdrArchiveError("archive functional receipt mismatch")
        elif index["schema_version"] == 4:
            raise HerdrArchiveError("functional archive lacks its policy")
        permission_proof = {"status": "not_attested", "reason": "historical_archive_v2"}
        reassessment = None
        if index["schema_version"] >= 3 or attest_permissions or for_completion:
            checked = attest_admissions(contents, archived_state, frozen["candidate_repo"], profile=profile)
            if index["schema_version"] >= 3:
                permission_proof = checked
            else:
                reassessment = checked
        if index["schema_version"] >= 3 and permission_proof.get("policy_version") != index["permissions_policy_version"]:
            raise HerdrArchiveError("archive permission version differs from creation-bound evidence")
        if for_completion and (permission_proof.get("status") != "attested"
                or permission_proof.get("policy_version") != finalization_policy["permissions_policy_version"]
                or permission_proof.get("runs") != finalization_policy["required_turns"]):
            raise HerdrArchiveError("archive completion permission attestation is incompatible")
        store.assert_root_binding()
    return {"archive_schema_version": index["schema_version"], "permissions": permission_proof,
        **({"physical_scope": scope_receipt} if scope_receipt is not None else {}),
        **({"inference_broker": inference_evidence} if archived_state.get("inference_policies") else {}),
        **({"functional": functional, "functional_verification_scope": "offline_integrity_and_controller_provenance_not_reexecution"} if functional is not None else {}),
        **({"finalization_policy_event_sha256": finalization_policy["event_sha256"]} if for_completion else {}),
        **({"permissions_reassessment": reassessment} if reassessment is not None else {}), "valid": bool(anchors), "staged_valid": True, "anchored": bool(anchors), "mission_id": mission_id, "index_sha256": _sha(raw),
        "path": str(runs_dir / archive / "archive-index.json"), "final_tree_sha": index["final_tree_sha"],
        "acceptance": acceptance, "audit_scope": "local_hash_chain_and_cas"}


def main() -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True, type=Path)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--attest-permissions", action="store_true", help="Reassess historical recorded permissions without rewriting the archive")
    args = parser.parse_args()
    try:
        result = verify(args.runs_dir.resolve(strict=True), args.mission_id, attest_permissions=args.attest_permissions)
    except (HerdrArchiveError, fleet_safe_paths.SafePathError, state.MissionStateError,
            fleet_json.FleetJSONError, fleet_compiled.CompiledError,
            fleet_acceptance.AcceptanceError, fleet_archive_tree.ArchiveError, OSError) as exc:
        print(f"herdr-archive: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
