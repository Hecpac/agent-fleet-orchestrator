"""Trusted exec wrapper on Herdr's normal agent.start path.

Records launch inputs and optionally bootstraps the native observer. Missing
effective inputs and external effects keep INTEGRATION_BINDING NOT_VERIFIED.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

import fleet_artifacts
import fleet_herdr_binding as identity
import fleet_herdr_binding_v2 as binding
import fleet_herdr_permissions as permissions
import fleet_json
import fleet_mission_state as state
import fleet_safe_paths


class LaunchError(RuntimeError):
    """Launch inputs do not match CONTROL's durable one-use intent."""


def digest(value):
    return hashlib.sha256(fleet_json.canonical_bytes(value)).hexdigest()


def validate_manifest(value):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] not in {1, 2}:
        raise LaunchError("invalid pinned launch manifest")
    fields = {"version", "codex", "herdr"} | ({"native_observer"} if value["version"] == 2 else set())
    if set(value) != fields or value["version"] == 2 and value["native_observer"] != "worker-v1":
        raise LaunchError("unsupported native observer manifest")
    for name, version in (("codex", "codex-cli 0.153.0"), ("herdr", "herdr 0.8.2")):
        item = value[name]
        if not isinstance(item, dict) or set(item) != {"image", "version"} or item["version"] != version:
            raise LaunchError(f"unsupported {name} version")
        if identity.path_identity(item["image"]["realpath"], directory=False) != item["image"]:
            raise LaunchError(f"{name} image changed")
        if not os.access(item["image"]["realpath"], os.X_OK):
            raise LaunchError(f"{name} image is not executable")
    return value


def launcher_bin(runs, mid):
    return Path(runs) / "missions" / mid / "herdr-launch" / "bin"


def install_wrapper(runs, mid):
    # -I rejects PYTHONPATH/user site; the source path is trusted CONTROL code.
    interpreter = str(Path(sys.executable).resolve(strict=True))
    if any(c in interpreter for c in " \n\r"):
        raise LaunchError("launcher interpreter path is not shebang-safe")
    source = Path(__file__).resolve()
    content = (f"#!{interpreter} -I\nimport sys\nsys.path.insert(0, {str(source.parent)!r})\n"
               "from fleet_herdr_launch import main\nraise SystemExit(main())\n").encode()
    with fleet_safe_paths.RootedFS(runs) as fs:
        fs.atomic_write(Path("missions") / mid / "herdr-launch/bin/codex", content,
                        directory_modes=(0o700,) * 4, file_mode=0o700)
    return launcher_bin(runs, mid)


def role_environment(candidate, role, attempt_id):
    base = Path(candidate).parent / "roles" / role / attempt_id
    # This is generated data, not inherited environment values or credentials.
    return {"HOME": str(base / "home"), "CODEX_HOME": str(base / "codex-home"),
            "TMPDIR": str(base / "tmp"), "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_CACHE_HOME": str(base / "cache"), "XDG_STATE_HOME": str(base / "state"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "TERM": "xterm-256color"}


def prepare(runs, mid, candidate, compiled, member, manifest, argv):
    """Called after Fleet persists this exact starting attempt, before Herdr send."""
    validate_manifest(manifest)
    generation = state.normalize_uuid(member["generation"], "generation")
    aid = state.normalize_uuid(member["start_attempts"][-1]["attempt_id"], "attempt_id")
    role = member["instance_id"]
    if role not in {"lead", "worker", "reviewer", "verifier"}:
        raise LaunchError("unknown launch role")
    candidate = str(candidate)
    if Path(candidate).is_relative_to(Path(runs)) or Path(runs).is_relative_to(Path(candidate)):
        raise LaunchError("launch observation requires separated runtime layout")
    env = role_environment(candidate, role, aid)
    for name in ("HOME", "CODEX_HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        # Descriptor-anchored creation; marker is runtime scaffolding, not evidence.
        relative = Path(env[name]).relative_to(Path(candidate).parent)
        with fleet_safe_paths.RootedFS(Path(candidate).parent, root_mode=0o700) as fs:
            fs.atomic_write(relative / ".fleet-owned", b"runtime scaffolding\n",
                            directory_modes=(0o700,) * len(relative.parts), file_mode=0o600)
    current = state.derive_state(state.read_events(state.ledger_path(runs, mid), expected_mission_id=mid))
    attempt = {"mission_id": mid, "generation": generation, "role": role, "attempt_id": aid}
    # Establish the protected CAS using its normal writer before freezing paths.
    fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes({"kind": "launch-attempt", "attempt": attempt}))
    frozen_pin = None
    if role == "worker":
        root = state.mission_root(runs, mid)
        frozen = binding.freeze_attempt(attempt=attempt, challenge=os.urandom(32).hex(),
            candidate=candidate, codex=manifest["codex"],
            temporary_roots={"slash_tmp": str(Path("/tmp").resolve(strict=True)), "tmpdir": env["TMPDIR"]},
            protected_roots={"runs": str(runs), "control": str(root), "ledger": str(root),
                             "cas": str(fleet_artifacts.store_path(runs, mid))},
            router_sha256=compiled["router_digest"], workflow_sha256=compiled["workflow_digest"])
        frozen_pin = fleet_artifacts.put_bytes(runs, mid, frozen)["artifact_id"]
    capsule = {"schema_version": 1, "kind": "herdr-launch-intent", "attempt": attempt,
        "candidate": identity.path_identity(candidate, directory=True), "codex": manifest["codex"],
        "argv": argv, "environment": env, "requested_policy": permissions.policy(role, candidate),
        "frozen_policy_sha256": frozen_pin, "ledger_predecessor_sha256": current["head_sha256"],
        "runs": str(runs)}
    if manifest["version"] == 2 and role == "worker":
        capsule.update(schema_version=2, native_observer="worker-v1")
    raw = fleet_json.canonical_bytes(capsule)
    pin = fleet_artifacts.put_bytes(runs, mid, raw)["artifact_id"]
    relative = Path("missions") / mid / "herdr-launch" / aid / "intent.json"
    with fleet_safe_paths.RootedFS(runs) as fs:
        fs.atomic_write(relative, raw, directory_modes=(0o700,) * 4, file_mode=0o600, require_absent=True)
    return {"path": str(Path(runs) / relative), "sha256": pin}


def consume(path, pin, observed_argv, *, native=None):
    """Validate the actual argv/cwd; consume durably before exec with no replay."""
    path = Path(path)
    if path.resolve(strict=True) != path or path.is_symlink():
        raise LaunchError("launch intent alias")
    # The fixed layout determines the read root; never trust a root inside JSON.
    runs = path.parents[4]
    relative = path.relative_to(runs)
    if len(relative.parts) != 5 or relative.parts[0] != "missions" or relative.parts[2] != "herdr-launch" or relative.parts[-1] != "intent.json":
        raise LaunchError("invalid launch intent location")
    mid, aid = relative.parts[1], relative.parts[3]
    state.normalize_uuid(mid, "mission_id")
    state.normalize_uuid(aid, "attempt_id")
    with fleet_safe_paths.RootedFS(runs) as fs:
        raw = fs.read_regular(relative, directory_modes=(0o700,) * 4, file_mode=0o600, max_bytes=1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != pin:
            raise LaunchError("launch intent digest changed")
        capsule = fleet_json.loads(raw)
        version = capsule.get("schema_version")
        extra = {"native_observer"} if version == 2 else set()
        if (set(capsule) != {"schema_version", "kind", "attempt", "candidate", "codex", "argv", "environment",
                "requested_policy", "frozen_policy_sha256", "ledger_predecessor_sha256", "runs"} | extra
                or type(version) is not int or version not in {1, 2}
                or capsule["kind"] != "herdr-launch-intent"):
            raise LaunchError("unsupported launch intent")
        if version == 2 and (capsule["native_observer"] != "worker-v1" or capsule["attempt"]["role"] != "worker" or native is None):
            raise LaunchError("native launch requires its trusted adapter")
        if capsule["runs"] != str(runs) or capsule["attempt"]["mission_id"] != mid or capsule["attempt"]["attempt_id"] != aid:
            raise LaunchError("launch intent identity mismatch")
        backend = fleet_json.loads(fs.read_regular(Path("missions") / mid / "herdr-backend.json",
            directory_modes=(0o700, 0o700), file_mode=0o600, max_bytes=4 * 1024 * 1024))
        members = [m for m in backend["members"] if m["instance_id"] == capsule["attempt"]["role"]]
        member = members[0] if len(members) == 1 else None
        if (backend["mission_id"] != mid or backend["generation"] != capsule["attempt"]["generation"]
                or not member or member["start_phase"] != "starting"
                or member["generation"] != capsule["attempt"]["generation"]
                or member["start_attempts"][-1]["attempt_id"] != aid
                or member["start_attempts"][-1].get("launch_intent") != {"path": str(path), "sha256": pin}):
            raise LaunchError("inactive or replaced launch attempt")
        if observed_argv != capsule["argv"] or os.getcwd() != capsule["candidate"]["realpath"]:
            raise LaunchError("actual launch argv or cwd differs from intent")
        if identity.path_identity(os.getcwd(), directory=True) != capsule["candidate"]:
            raise LaunchError("candidate identity changed")
        role = capsule["attempt"]["role"]
        if capsule["requested_policy"] != permissions.policy(role, os.getcwd()):
            raise LaunchError("requested policy differs from the role contract")
        if role == "worker" and capsule["frozen_policy_sha256"] is None:
            raise LaunchError("Worker launch requires a frozen policy")
        codex = capsule["codex"]
        if identity.path_identity(codex["image"]["realpath"], directory=False) != codex["image"]:
            raise LaunchError("Codex image changed")
        env = role_environment(os.getcwd(), capsule["attempt"]["role"], aid)
        if env != capsule["environment"]:
            raise LaunchError("effective environment differs from intent")
        for name in ("HOME", "CODEX_HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            if str(Path(env[name]).resolve(strict=True)) != env[name]:
                raise LaunchError("runtime environment path alias")
        if capsule["frozen_policy_sha256"] is not None:
            frozen = fleet_artifacts.get_bytes(runs, mid, capsule["frozen_policy_sha256"])
            validated = binding.validate_frozen(frozen, pinned_sha256=capsule["frozen_policy_sha256"], active_attempt=capsule["attempt"])
            if (validated["requested_policy"]["policy"] != capsule["requested_policy"]
                    or validated["codex"] != codex or validated["candidate"] != capsule["candidate"]
                    or validated["temporary_roots"]["tmpdir"] != env["TMPDIR"]):
                raise LaunchError("launch requested policy differs from frozen policy")
        events = state.read_events(state.ledger_path(runs, mid), expected_mission_id=mid)
        if not any(e["event_sha256"] == capsule["ledger_predecessor_sha256"] for e in events):
            raise LaunchError("launch ledger predecessor missing")
        current = state.derive_state(events)
        if current["status"] != "booting" or current.get("herdr_control", {}).get("desired", "running") != "running":
            raise LaunchError("Mission authority no longer permits launch")
        if datetime.now(timezone.utc) >= state.parse_timestamp(current["admission_policy"]["deadline_at"], "launch deadline"):
            raise LaunchError("Mission launch deadline expired")
        argv = [codex["image"]["realpath"], *observed_argv[1:]]
        if version == 2:
            # The FD is created here, before recording the actual argv. It is a
            # capability transferred by the launcher, never an agent argument.
            argv.insert(1, native.open(capsule, path, pin))
        record = {"schema_version": 1, "kind": "launcher-observation", "attempt": capsule["attempt"],
            "run_id": None, "intent_sha256": pin, "frozen_policy_sha256": capsule["frozen_policy_sha256"],
            "candidate": capsule["candidate"], "codex": codex, "requested_policy": capsule["requested_policy"],
            "codex_version_source": "pinned_manifest",
            "argv_sha256": digest(argv), "argv_redacted": [a if a in identity.SAFE_FLAGS else "<redacted>" for a in argv],
            "inherited_environment_names": sorted(os.environ), "environment_names": sorted(env),
            "sanitized_environment_sha256": digest(env), "launcher_pid": os.getpid(),
            "ledger_predecessor_sha256": capsule["ledger_predecessor_sha256"],
            "authority": "none", "INTEGRATION_BINDING": "NOT_VERIFIED",
            "missing": ["codex_effective_policy", "authenticated_pre_exec", "process_birth_image", "external_effects"]}
        if version == 2:
            record["native_observer"] = "worker-v1"
        receipt = fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(record))
        fs.atomic_write(relative.with_name("consumed.json"), fleet_json.canonical_bytes({"artifact_id": receipt["artifact_id"]}),
                        directory_modes=(0o700,) * 4, file_mode=0o600, require_absent=True)
        if version == 2:
            native.bind(receipt["artifact_id"])
    return argv, env


def link_run(runs, mid, member, run_id, prompt_sha256):
    """Bind a later turn to its launch observation without granting authority."""
    state.normalize_uuid(run_id, "run_id")
    attempt = member["start_attempts"][-1]
    observation = attempt.get("launch_observation_artifact_id")
    if not observation:
        raise LaunchError("turn has no launch observation")
    record = fleet_json.loads(fleet_artifacts.get_bytes(runs, mid, observation))
    expected = {"mission_id": mid, "generation": member["generation"],
                "role": member["instance_id"], "attempt_id": attempt["attempt_id"]}
    if record.get("attempt") != expected or record.get("intent_sha256") != attempt["launch_intent"]["sha256"]:
        raise LaunchError("turn launch observation belongs to another attempt")
    link = {"schema_version": 1, "kind": "launch-run-link", "attempt": expected,
            "run_id": run_id, "prompt_sha256": prompt_sha256, "launch_observation_sha256": observation,
            "authority": "none", "INTEGRATION_BINDING": "NOT_VERIFIED"}
    return fleet_artifacts.put_bytes(runs, mid, fleet_json.canonical_bytes(link))["artifact_id"]


def main():
    from fleet_herdr_native import NativeLaunch
    native = NativeLaunch()
    try:
        if len(sys.argv) < 4 or sys.argv[1] != "--fleet-launch-intent":
            raise LaunchError("missing CONTROL launch intent; no ambient Codex fallback")
        path, pin = sys.argv[2:4]
        argv, env = consume(path, pin, ["codex", *sys.argv[4:]], native=native)
        native.start(env)
        os.execve(argv[0], argv, env)
    except Exception as exc:
        # Never log argv, environment values or arbitrary OS exception payloads.
        print(f"fleet launcher refused: {type(exc).__name__}", file=sys.stderr)
        return 126
    finally:
        native.close()


if __name__ == "__main__":
    raise SystemExit(main())
