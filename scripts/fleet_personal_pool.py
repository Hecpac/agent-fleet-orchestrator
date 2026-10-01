#!/usr/bin/env python3
"""Prepare an idle personal fleet; task execution remains Mission Control's job.

No prompts are sent here. Prepared roles are read-only and have no Mission
authority. Assignment closes the exact idle pool before creating a new Mission.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import fleet_acceptance
import fleet_herdr as herdr
import fleet_herdr_permissions as permissions
import hashlib
import time

import fleet_herdr_startup as startup
from fleet_herdr_startup import codex_startup_blocker
from fleet_herdr_versions import CURRENT_CONTRACT
import fleet_json
import fleet_personal_preflight as preflight
import fleet_safe_paths


class PoolError(ValueError):
    pass


class Pool:
    def __init__(self, root: Path, *, run=None, probe=None):
        self.root = root.absolute()
        if self.root.resolve() != self.root:
            raise PoolError("pool root must be a physical path")
        self.run = run or self._run
        self.probe = probe or preflight.inspect

    @staticmethod
    def _run(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=45, check=False)

    def call(self, saved, *args):
        return self.run([saved["herdr_binary"], "--session", saved["session"], *args])

    def result(self, saved, *args):
        return herdr._result_payload(self.call(saved, *args), "personal " + args[0])

    @staticmethod
    def save(fs, saved):
        existing = fs.read_regular_optional(Path("pool.json"), file_mode=0o600,
                                            max_bytes=1024 * 1024, directory_modes=())
        write = fs.replace_regular if existing is not None else fs.atomic_write
        write(Path("pool.json"), fleet_json.canonical_bytes(saved) + b"\n",
              file_mode=0o600, directory_modes=())

    @staticmethod
    def read(fs):
        raw = fs.read_regular_optional(Path("pool.json"), file_mode=0o600, max_bytes=1024 * 1024, directory_modes=())
        if raw is None:
            return None
        saved = fleet_json.loads(raw)
        if (not isinstance(saved, dict) or saved.get("schema_version") != 1 or saved.get("kind") != "personal-idle-pool"
                or not herdr.SESSION_NAME.fullmatch(saved.get("session", ""))):
            raise PoolError("unsupported personal pool")
        return saved

    def workspace(self, saved):
        if not saved.get("workspace"):
            raise PoolError("workspace creation is not reconciled; no duplicate allocation")
        result = self.call(saved, "workspace", "get", saved["workspace"]["workspace_id"])
        if result.returncode and herdr._error_code(result) == "workspace_not_found":
            return None
        value = herdr._result_payload(result, "personal workspace get")["workspace"]
        if (value.get("workspace_id") != saved["workspace"]["workspace_id"]
                or value.get("label") != saved["label"]):
            raise PoolError("pool workspace ownership differs")
        return value

    def observe_member(self, saved, member):
        value = self.result(saved, "agent", "get", member["name"])
        observed = herdr._agent_receipt(value, allow_missing_session=True)
        for field in ("workspace_id", "tab_id", "pane_id", "terminal_id"):
            if observed[field] != member[field]:
                raise PoolError("prepared agent identity differs: " + field)
        if observed["agent_name"] != member["name"]:
            raise PoolError("prepared agent name differs")
        screen = self.call(saved, "agent", "read", member["name"], "--source", "visible")
        if screen.returncode or len(screen.stdout) > 128 * 1024:
            raise PoolError("prepared Codex surface is unavailable")
        reason = codex_startup_blocker(screen.stdout, saved.get("startup_guard", CURRENT_CONTRACT["startup_guard"]))
        return {"role": member["role"], "name": member["name"], "pane_id": member["pane_id"],
                "status": observed["agent_status"], "startup_blocker": reason,
                "ready": observed["agent_status"] in {"idle", "done"} and reason is None,
                "requested_model": member["model"], "observed_model": None,
                "model_execution": "NOT_VERIFIED"}

    def prepare(self, target: Path, session: str):
        target = target.resolve(strict=True)
        if not herdr.SESSION_NAME.fullmatch(session):
            raise PoolError("an explicit safe Herdr session is required")
        diagnostic = self.probe(target)
        if diagnostic["runtime_preflight"] != "PASS":
            raise PoolError("personal preflight blocked: " + ", ".join(diagnostic["blockers"]))
        self.root.mkdir(mode=0o700, exist_ok=True)
        with fleet_safe_paths.RootedFS(self.root) as fs:
            with fs.exclusive_lock(Path("pool.lock"), directory_modes=()):
                saved = self.read(fs)
                if saved is None:
                    pool_id = str(uuid.uuid4())
                    saved = {"schema_version": 1, "kind": "personal-idle-pool", "pool_id": pool_id,
                             "target": str(target), "session": session, "phase": "allocating",
                             "label": "Agent Fleet · Lista · " + pool_id[:8], "workspace": None,
                             "herdr_binary": diagnostic["binaries"]["herdr"]["path"], "members": [],
                             "controller_prompts_sent": 0, "mission_id": None, "pending": "workspace.create",
                             "startup_guard": diagnostic.get("runtime_contract", CURRENT_CONTRACT)["startup_guard"],
                             "codex_pin": diagnostic.get("codex_pin")}
                    self.save(fs, saved)
                    path = os.environ.get("PATH", "")
                    if saved["codex_pin"]:
                        # Same pin as the Missions: the certified install leads PATH.
                        path = saved["codex_pin"]["bin_dir"] + ":" + path
                    env = ["--env", "PATH=" + path]
                    for name in ("CODEX_HOME", "HOME"):
                        if os.environ.get(name):
                            env += ["--env", name + "=" + os.environ[name]]
                    created = self.result(saved, "workspace", "create", "--cwd", str(target),
                                          "--label", saved["label"], *env, "--no-focus")
                    saved["workspace"] = herdr.HerdrBackend._workspace_binding(created)
                    first = created["root_pane"]
                    saved["members"].append(self.member(saved, "lead", first))
                    saved["pending"] = None
                    self.save(fs, saved)
                if saved["target"] != str(target) or saved["session"] != session or saved["phase"] == "closed":
                    raise PoolError("pool identity differs or pool is closed")
                if saved.get("pending"):
                    raise PoolError("previous pool operation is indeterminate; refusing to repeat it")
                if self.workspace(saved) is None:
                    raise PoolError("owned workspace is absent")
                for role, source_role, direction in (("worker", "lead", "right"),
                                                      ("reviewer", "lead", "down"),
                                                      ("verifier", "worker", "down")):
                    if any(m["role"] == role for m in saved["members"]):
                        continue
                    source = next(m for m in saved["members"] if m["role"] == source_role)
                    saved["pending"] = "pane.split:" + role
                    self.save(fs, saved)
                    pane = self.result(saved, "pane", "split", source["pane_id"], "--direction", direction,
                                       "--cwd", str(target), "--no-focus")["pane"]
                    saved["members"].append(self.member(saved, role, pane))
                    saved["pending"] = None
                    self.save(fs, saved)
                for member in saved["members"]:
                    if member["phase"] != "not_started":
                        continue
                    member["phase"] = "starting"
                    self.save(fs, saved)
                    # Until a Mission exists even the future Worker is read-only.
                    result = self.call(saved, "agent", "start", member["name"], "--kind", "codex",
                                       "--pane", member["pane_id"], "--timeout", "30000", "--",
                                       "--model", member["model"], "-c", 'model_reasoning_effort="high"',
                                       "--sandbox", "read-only", "--ask-for-approval", "never", "-c",
                                       'projects={' + json.dumps(str(target)) + '={trust_level="untrusted"}}')
                    if result.returncode:
                        saved["phase"] = "blocked"
                        self.save(fs, saved)
                        herdr._result_payload(result, "personal agent start")
                    receipt = herdr._agent_receipt(herdr._result_payload(result, "personal start"),
                                                  allow_missing_session=True)
                    if any(receipt[k] != member[k] for k in ("workspace_id", "tab_id", "pane_id", "terminal_id")):
                        raise PoolError("agent start identity differs")
                    member["phase"] = "started"
                    self.save(fs, saved)
                    self.acknowledge_folder_access(fs, saved, member, target)
                observations = [self.observe_member(saved, m) for m in saved["members"]]
                saved["phase"] = "ready" if all(o["ready"] for o in observations) else "blocked"
                self.save(fs, saved)
                return {**saved, "observations": observations}

    def acknowledge_folder_access(self, fs, saved, member, target):
        """Answer only the exact 0.159 Folder access notice, once, and record it.

        Same rule as the Mission backend: "Open restricted" keeps restrictions
        and changes no saved trust; any other dialog stays visible and blocks.
        """
        guard = saved.get("startup_guard", CURRENT_CONTRACT["startup_guard"])
        screen = self.call(saved, "agent", "read", member["name"], "--source", "visible")
        if screen.returncode or not startup.folder_access_notice(
                screen.stdout, guard=guard, cwd=str(target), home=os.environ.get("HOME")):
            return
        if member.get("startup_acknowledgments"):
            raise PoolError("Codex folder access notice reappeared after its acknowledgment")
        before = hashlib.sha256(screen.stdout.encode()).hexdigest()
        if self.call(saved, "agent", "send-keys", member["name"], "enter").returncode:
            raise PoolError("Codex folder access acknowledgment was not delivered")
        member["startup_acknowledgments"] = [{"guard": guard, "notice": "folder-access-open-restricted",
                                              "key": "enter", "screen_before_sha256": before}]
        self.save(fs, saved)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            screen = self.call(saved, "agent", "read", member["name"], "--source", "visible")
            if screen.returncode == 0 and codex_startup_blocker(screen.stdout, guard) is None:
                break
            time.sleep(0.25)

    @staticmethod
    def member(saved, role, pane):
        fields = {k: pane[k] for k in ("workspace_id", "tab_id", "pane_id", "terminal_id")}
        if fields["workspace_id"] != saved["workspace"]["workspace_id"]:
            raise PoolError("allocated pane belongs to another workspace")
        return {**fields, "role": role, "model": permissions.MODELS[role], "phase": "not_started",
                "name": "ready_" + saved["pool_id"][:8] + "_" + role}

    def show(self):
        with fleet_safe_paths.RootedFS(self.root) as fs:
            saved = self.read(fs)
        if saved is None:
            raise PoolError("pool does not exist")
        if saved["phase"] == "closed":
            return saved
        if self.workspace(saved) is None:
            return {**saved, "live_phase": "absent"}
        observed = [self.observe_member(saved, m) for m in saved["members"] if m["phase"] != "not_started"]
        return {**saved, "live_phase": "ready" if len(observed) == 4 and all(o["ready"] for o in observed)
                else "blocked", "observations": observed}

    def close(self):
        with fleet_safe_paths.RootedFS(self.root) as fs:
            with fs.exclusive_lock(Path("pool.lock"), directory_modes=()):
                saved = self.read(fs)
                if not saved:
                    raise PoolError("pool does not exist")
                if saved["phase"] == "closed":
                    return saved
                if self.workspace(saved) is not None:
                    for member in saved["members"]:
                        if member["phase"] == "started":
                            observed = self.observe_member(saved, member)
                            if observed["status"] not in {"idle", "done", "blocked"}:
                                raise PoolError("prepared agent is active; workspace was not closed")
                    saved["phase"] = "closing"
                    self.save(fs, saved)
                    self.result(saved, "workspace", "close", saved["workspace"]["workspace_id"])
                    if self.workspace(saved) is not None:
                        raise PoolError("workspace closure is unconfirmed")
                saved["phase"] = "closed"
                self.save(fs, saved)
                return saved

    def assign(self, request: dict, execute):
        """Freeze assignment before retiring preparation; retries use the same request."""
        with fleet_safe_paths.RootedFS(self.root) as fs:
            with fs.exclusive_lock(Path("pool.lock"), directory_modes=()):
                saved = self.read(fs)
                if saved is None:
                    raise PoolError("pool does not exist")
                previous = saved.get("assignment")
                if previous is not None and previous != request:
                    raise PoolError("pool already has a different immutable assignment")
                if previous is None:
                    if saved["phase"] != "ready" or self.workspace(saved) is None:
                        raise PoolError("assignment requires a ready pool")
                    if not all(self.observe_member(saved, m)["ready"] for m in saved["members"]):
                        raise PoolError("assignment requires all four agents idle and ready")
                    saved["assignment"] = request
                    self.save(fs, saved)
                if saved.get("mission_id"):
                    # The executor must resume the exact Mission, never derive a new one.
                    return execute(saved, saved["mission_id"])
        self.close()
        result = execute(saved, None)
        with fleet_safe_paths.RootedFS(self.root) as fs:
            with fs.exclusive_lock(Path("pool.lock"), directory_modes=()):
                current = self.read(fs)
                if current.get("assignment") != request:
                    raise PoolError("assignment changed during execution")
                current["mission_id"] = result["mission_id"]
                self.save(fs, current)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--target-repo", type=Path, required=True)
    prepare.add_argument("--session", required=True)
    sub.add_parser("show")
    sub.add_parser("close")
    assign = sub.add_parser("assign")
    assign.add_argument("feature")
    assign.add_argument("objective")
    assign.add_argument("--acceptance-contract", type=Path, required=True)
    assign.add_argument("--runs-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    pool = Pool(args.state_dir)
    try:
        if args.command == "prepare":
            result = pool.prepare(args.target_repo, args.session)
        elif args.command == "show":
            result = pool.show()
        elif args.command == "close":
            result = pool.close()
        else:
            contract = fleet_acceptance.load(args.acceptance_contract)
            spec = importlib.util.spec_from_file_location("personal_mission_run", Path(__file__).with_name("mission-run.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not module.FEATURE.fullmatch(args.feature) or not args.objective.strip():
                raise PoolError("assignment requires a valid feature and nonempty objective")
            compiled = module.workflow_config.compile_path(module.workflow_path("herdr-implementation"))
            request = {"feature": args.feature, "objective": args.objective,
                       "runs_dir": str(args.runs_dir.resolve()), "acceptance_contract": contract,
                       "compiled_digest": compiled["compiled_digest"]}
            def execute(current, mission_id):
                if mission_id:
                    return module.drive_mission(Path(request["runs_dir"]), mission_id)
                return module.create_and_drive(Path(request["runs_dir"]), feature=args.feature, objective=args.objective,
                    workflow_name="herdr-implementation", target_repo=Path(current["target"]), risk_override="auto",
                    timeout_seconds=7200, allow_dirty_baseline=False, teardown=False,
                    herdr_session=current["session"], acceptance_contract=contract)
            result = pool.assign(request, execute)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "assign":
            return module.response_exit_code(result)
        return 0 if result.get("live_phase", result.get("phase")) in {"ready", "closed"} else 1
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"personal-pool: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
