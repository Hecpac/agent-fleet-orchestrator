"""Herdr archives v2 (historical), v3 (permissions), v4 (functional receipt).

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
import fleet_archive
import fleet_artifacts
import fleet_compiled
import fleet_json
import fleet_herdr_evidence
import fleet_herdr_permissions
import fleet_functional
import fleet_mission
import fleet_mission_state as state
import fleet_safe_paths

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
        env=environment or fleet_archive._git_environment(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, input=input_bytes, timeout=60, check=False)
    if result.returncode:
        raise HerdrArchiveError(result.stderr.decode(errors="replace")[:2000])
    if len(result.stdout) > MAX_BYTES:
        raise HerdrArchiveError("Git snapshot exceeds size limit")
    return result.stdout


def snapshot(candidate_repo: Path, *, expected_base: str | None = None) -> tuple[str, bytes, bytes]:
    """Use a private index; never stage into the candidate's real index."""
    repo = candidate_repo.resolve(strict=True)
    if Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve() != repo:
        raise HerdrArchiveError("candidate must be the exact Git root")
    base = _git(repo, "rev-parse", "HEAD").decode().strip()
    if expected_base is not None and base != expected_base:
        raise HerdrArchiveError("candidate HEAD differs from mission baseline")
    with tempfile.TemporaryDirectory(prefix="fleet-herdr-index-") as temporary:
        environment = {**fleet_archive._git_environment(), "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(repo, "read-tree", base, environment=environment)
        _git(repo, "add", "--all", "--", ".", environment=environment)
        tree_sha = _git(repo, "write-tree", environment=environment).decode().strip()
        patch = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", base, tree_sha)
        _git(repo, "read-tree", base, environment=environment)
        if patch:
            _git(repo, "apply", "--cached", "--binary", "--whitespace=nowarn", "-", environment=environment, input_bytes=patch)
        if _git(repo, "write-tree", environment=environment).decode().strip() != tree_sha:
            raise HerdrArchiveError("patch does not reproduce frozen tree from baseline")
    object_format = _git(repo, "rev-parse", "--show-object-format").decode().strip()
    tree = fleet_archive._raw_tree_tar(repo, tree_sha, object_format)
    if fleet_archive._tree_hash_from_tar(tree, object_format) != tree_sha:
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
    tree_sha, tree, patch = snapshot(candidate_repo, expected_base=current["base_sha"])
    frozen = {
        "schema_version": 1, "mission_id": mission_id,
        "compiled_digest": compiled["compiled_digest"], "base_sha": current["base_sha"],
        "candidate_repo": str(candidate_repo.resolve(strict=True)), "tree_sha": tree_sha,
        "tree_artifact_id": fleet_artifacts.put_bytes(runs_dir, mission_id, tree)["artifact_id"],
        "patch_artifact_id": fleet_artifacts.put_bytes(runs_dir, mission_id, patch)["artifact_id"],
    }
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        _write(store, root / "candidate-freeze.json", _bytes(frozen))
    return frozen


def create(runs_dir: Path, mission_id: str, candidate_repo: Path,
           role_results: dict[str, Any], backend_state: dict[str, Any]) -> dict[str, Any]:
    compiled, current = fleet_mission.load_mission_compiled(runs_dir, mission_id, mode="effect")
    root = Path("missions") / mission_id
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        if ("herdr-archive" in store.list_directory(root, directory_modes=(0o700, 0o700))
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
    if current["status"] != "completing" or any(a.get("active") for a in current["admissions"].values()):
        raise HerdrArchiveError("archive requires completing mission with finalized admissions")
    if set(role_results) != ROLES:
        raise HerdrArchiveError("archive requires results from all four roles")
    if set(current.get("risk_categories", [])) & {"credentials", "private_data", "regulated"}:
        raise HerdrArchiveError("sensitive/regulated archive requires separately authorized audit lane")
    if backend_state.get("mission_id") != mission_id or backend_state.get("compiled_digest") != compiled["compiled_digest"]:
        raise HerdrArchiveError("backend archive binding mismatch")
    frozen = freeze(runs_dir, mission_id, candidate_repo)
    contents: dict[str, bytes] = {}
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        for name in ("compiled-workflow.json", "runtime-options.json", "creation-request.json", "objective.txt", "ledger.jsonl", "candidate-freeze.json"):
            contents[name] = _read(store, root / ("mission.jsonl" if name == "ledger.jsonl" else name))
    contents["backend.json"] = _bytes(backend_state)
    contents["role-results.json"] = _bytes(role_results)
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        for artifact_id in store.list_directory(root / "artifacts", directory_modes=(0o700, 0o700, 0o700)):
            contents[f"artifacts/{artifact_id}"] = fleet_artifacts.get_bytes(runs_dir, mission_id, artifact_id)
    for role, result in role_results.items():
        if not isinstance(result, dict) or not isinstance(result.get("artifact_id"), str):
            raise HerdrArchiveError(f"{role} result lacks CAS artifact")
        contents[f"results/{role}.txt"] = fleet_artifacts.get_bytes(runs_dir, mission_id, result["artifact_id"])
    contents["writer/final-tree.tar"] = fleet_artifacts.get_bytes(runs_dir, mission_id, frozen["tree_artifact_id"])
    contents["writer/change.patch"] = fleet_artifacts.get_bytes(runs_dir, mission_id, frozen["patch_artifact_id"])
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
    contract = options.get("acceptance_contract")
    acceptance = (fleet_acceptance.evaluate(contract, contents["writer/final-tree.tar"],
        mission_id=mission_id, final_sha=frozen["tree_sha"]) if contract is not None else
        {"status": "not_evaluated", "scope": "no_artifact_contract", "mission_id": mission_id})
    contents["acceptance-result.json"] = _bytes(acceptance)
    functional = fleet_functional.archived_receipt(current, frozen,
        lambda key: contents["artifacts/" + key])
    if functional is not None:
        contents["functional-result.json"] = _bytes(functional)
    # Reject missing or incompatible recorded permissions before staging files.
    attest_admissions(contents, current, frozen["candidate_repo"])
    index = {"schema_version": 4 if functional is not None else 3, "permissions_policy_version": 1, "backend": "herdr", "mission_id": mission_id,
        "compiled_digest": compiled["compiled_digest"], "ledger_head": current["head_sha256"],
        "base_sha": current["base_sha"], "final_tree_sha": frozen["tree_sha"],
        "entries": {name: {"sha256": _sha(content), "bytes": len(content)} for name, content in sorted(contents.items())}}
    if sum(map(len, contents.values())) > 128 * 1024 * 1024:
        raise HerdrArchiveError("archive exceeds size limit")
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        for name, content in sorted(contents.items()):
            _write(store, root / "herdr-archive" / name, content)
        _write(store, root / "herdr-archive" / "archive-index.json", _bytes(index))
    return verify(runs_dir, mission_id, require_anchor=False)


def attest_admissions(contents: dict[str, bytes], current: dict[str, Any], cwd: str) -> dict[str, Any]:
    """Check all five turn results, including the initial planning admission."""
    expected = {"herdr:plan": "lead", "herdr:build": "worker", "herdr:review": "reviewer",
                "herdr:verify": "verifier", "herdr:synthesis": "lead"}
    admissions = list(current["admissions"].values())
    if len(admissions) != 5 or {a["request_key"] for a in admissions} != set(expected):
        raise HerdrArchiveError("permission attestation requires all five stage admissions")

    def read(digest):
        try:
            raw = contents["artifacts/" + digest]
        except (KeyError, TypeError) as exc:
            raise HerdrArchiveError("permission attestation artifact is missing") from exc
        if _sha(raw) != digest:
            raise HerdrArchiveError("permission attestation CAS mismatch")
        return raw

    proofs = {}
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
                or recorded.get("model") != fleet_herdr_permissions.MODELS[role]):
            raise HerdrArchiveError("permission attestation result/admission mismatch")
        try:
            proofs[admission["run_id"]] = fleet_herdr_evidence.verify_result(result,
                read_artifact=read, role=role, cwd=cwd, prompt_sha256=admission["task_sha256"])
        except fleet_herdr_evidence.EvidenceError as exc:
            raise HerdrArchiveError(f"{admission['request_key']} permission evidence: {exc}") from exc
    return {"status": "attested", "policy_version": fleet_herdr_permissions.VERSION,
            "scope": "recorded_codex_turn_configuration", "runs": len(proofs), "by_run": proofs}


def verify(runs_dir: Path, mission_id: str, *, require_anchor: bool = True,
           attest_permissions: bool = False, for_completion: bool = False) -> dict[str, Any]:
    """Verify offline from durable files, including the live ledger anchor."""
    mission_id = state.normalize_uuid(mission_id, "mission_id")
    archive = Path("missions") / mission_id / "herdr-archive"
    with fleet_safe_paths.RootedFS(runs_dir) as store:
        raw = _read(store, archive / "archive-index.json")
        index = fleet_json.loads(raw)
        if not isinstance(index, dict) or raw != _bytes(index) or (type(index.get("schema_version")) is not int or index["schema_version"] not in {2, 3, 4}) or index.get("backend") != "herdr" or index.get("mission_id") != mission_id:
            raise HerdrArchiveError("invalid Herdr archive index")
        if index["schema_version"] >= 3 and (type(index.get("permissions_policy_version")) is not int
                or index["permissions_policy_version"] != fleet_herdr_permissions.VERSION):
            raise HerdrArchiveError("archive permission policy version is invalid")
        contents = {}
        entries = index.get("entries")
        if not isinstance(entries, dict) or len(entries) > 1000:
            raise HerdrArchiveError("invalid archive entry map")
        for name, proof in entries.items():
            fleet_archive._safe_relative(name)
            content = _read(store, archive / name)
            if proof != {"sha256": _sha(content), "bytes": len(content)}:
                raise HerdrArchiveError(f"archive content changed: {name}")
            contents[name] = content
        required = {"compiled-workflow.json", "runtime-options.json", "creation-request.json", "objective.txt", "ledger.jsonl", "candidate-freeze.json", "backend.json", "role-results.json", "writer/final-tree.tar", "writer/change.patch", "acceptance-result.json"} | {f"results/{role}.txt" for role in ROLES}
        if index["schema_version"] == 4:
            required.add("functional-result.json")
        artifact_names = {name for name in contents if name.startswith("artifacts/")}
        if set(contents) - artifact_names != required:
            raise HerdrArchiveError("archive required files differ")
        for name in artifact_names:
            if name != "artifacts/" + _sha(contents[name]):
                raise HerdrArchiveError("archived CAS identity mismatch")
        compiled = fleet_compiled.loads(contents["compiled-workflow.json"], mode="read")
        events = [fleet_json.loads(line) for line in contents["ledger.jsonl"].splitlines()]
        archived_state = state.derive_state(events)
        state.verify_events(events)
        if (archived_state["mission_id"] != mission_id or archived_state["head_sha256"] != index["ledger_head"]
                or archived_state["compiled_digest"] != compiled["compiled_digest"] or index["compiled_digest"] != compiled["compiled_digest"]
                or archived_state["base_sha"] != index["base_sha"]):
            raise HerdrArchiveError("archive ledger/compiled binding mismatch")
        live_events = state.read_events(state.ledger_path(runs_dir, mission_id), expected_mission_id=mission_id)
        finalization_policy = None
        if for_completion:
            finalization_policy = state.derive_state(live_events).get("herdr_finalization_policy")
            expected_policy = fleet_herdr_permissions.finalization_policy(compiled["compiled_digest"])
            if (not finalization_policy or
                    {k: v for k, v in finalization_policy.items() if k != "event_sha256"} != expected_policy):
                raise HerdrArchiveError("archive completion lacks its durable finalization policy")
            if index["schema_version"] < finalization_policy["minimum_archive_schema_version"]:
                raise HerdrArchiveError("historical archive schema cannot authorize new completion")
            if state.derive_state(live_events).get("functional_policy") != archived_state.get("functional_policy"):
                raise HerdrArchiveError("archive cannot omit durable functional policy")
        if live_events[:len(events)] != events:
            raise HerdrArchiveError("archive ledger is not a prefix of the mission")
        anchors = [e for e in live_events if e["kind"] == "archive_created"]
        if (len(anchors) > 1 or (require_anchor and len(anchors) != 1)
                or any(e["payload"]["sha256"] != _sha(raw) for e in anchors)):
            raise HerdrArchiveError("archive index differs from ledger anchor")
        frozen = fleet_json.loads(contents["candidate-freeze.json"])
        backend = fleet_json.loads(contents["backend.json"])
        if (frozen.get("mission_id") != mission_id or frozen.get("compiled_digest") != compiled["compiled_digest"]
                or frozen.get("base_sha") != index["base_sha"] or backend.get("mission_id") != mission_id
                or backend.get("compiled_digest") != compiled["compiled_digest"]):
            raise HerdrArchiveError("archive backend/freeze identity mismatch")
        tree = contents["writer/final-tree.tar"]
        object_format = "sha1" if len(index["final_tree_sha"]) == 40 else "sha256"
        if (fleet_archive._tree_hash_from_tar(tree, object_format) != index["final_tree_sha"]
                or frozen["tree_sha"] != index["final_tree_sha"] or frozen["tree_artifact_id"] != _sha(tree)
                or frozen["patch_artifact_id"] != _sha(contents["writer/change.patch"])):
            raise HerdrArchiveError("archive candidate tree binding mismatch")
        roles = fleet_json.loads(contents["role-results.json"])
        if set(roles) != ROLES or any(roles[role].get("artifact_id") != _sha(contents[f"results/{role}.txt"]) for role in ROLES):
            raise HerdrArchiveError("archive role artifact binding mismatch")
        members = {member["instance_id"]: member for member in [compiled["resolved"]["lead"], *compiled["resolved"]["instances"]]}
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
                    or evidence.get("prompt_sha256") != admission["task_sha256"]
                    or not isinstance(session, dict) or session.get("kind") != "id"):
                raise HerdrArchiveError("archive transcript evidence binding mismatch")
            try:
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
            with tarfile.open(fileobj=io.BytesIO(tree), mode="r:") as tar:
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
            if role in {"lead", "reviewer", "verifier"} and result.get("candidate_tree_sha") != index["final_tree_sha"]:
                raise HerdrArchiveError("archive reviewer candidate binding mismatch")
        options = fleet_json.loads(contents["runtime-options.json"])
        creation = fleet_json.loads(contents["creation-request.json"])
        if (creation.get("runtime_options") != options or creation.get("mission_id") != mission_id
                or _sha(contents["objective.txt"]) != archived_state["objective_sha256"]):
            raise HerdrArchiveError("archive runtime/objective binding mismatch")
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
            if (index["schema_version"] != 4 or fleet_functional.digest(fleet_functional.validate(functional_spec))
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
            checked = attest_admissions(contents, archived_state, frozen["candidate_repo"])
            if index["schema_version"] >= 3:
                permission_proof = checked
            else:
                reassessment = checked
        if for_completion and (permission_proof.get("status") != "attested"
                or permission_proof.get("policy_version") != finalization_policy["permissions_policy_version"]
                or permission_proof.get("runs") != finalization_policy["required_turns"]):
            raise HerdrArchiveError("archive completion permission attestation is incompatible")
        store.assert_root_binding()
    return {"archive_schema_version": index["schema_version"], "permissions": permission_proof,
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
            fleet_acceptance.AcceptanceError, fleet_archive.ArchiveError, OSError) as exc:
        print(f"herdr-archive: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
