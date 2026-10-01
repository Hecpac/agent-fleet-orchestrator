"""Managed CLI updates with fake commands and downloads; no network or installs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_cli_update as updates  # noqa: E402

import os

import fleet_json  # noqa: E402

WIRE_15 = b"protocol 1.5"
WIRE_16 = b"protocol 1.6"
CAPTURED = ROOT / "tests" / "fixtures" / "kimi" / "wire-1.5-kimi-code-2.1.1.jsonl"


def probe_records(binary: Path) -> list[dict]:
    """Stand-in for the real loopback turn: the captured 2.1.1 Wire, relabelled."""
    protocol = binary.read_bytes().rsplit(b" ", 1)[-1].decode()
    rows = [fleet_json.loads(line) for line in CAPTURED.read_bytes().splitlines()]
    rows[0]["protocol_version"] = protocol
    return rows


class FakeWorld:
    def __init__(self, root: Path):
        self.root = root
        self.versions = {"codex": "0.159.3", "claude": "2.1.286", "opencode": "1.18.30",
                         "ollama": "0.35.0", "herdr": "0.9.0"}
        self.latest = {"@openai/codex": "0.159.3", "@anthropic-ai/claude-code": "2.1.286",
                       "opencode": "1.18.30", "ollama": "0.35.0"}
        self.kimi_latest = "2.1.1"
        self.kimi_payload = b"kimi 2.1.1\n" + WIRE_15
        self.calls: list[list[str]] = []
        self.kimi = root / "kimi-bin" / "kimi"
        self.kimi.parent.mkdir()
        self.kimi.write_bytes(b"kimi 2.1.1\n" + WIRE_15)
        self.npm_fail = False

    def kimi_version(self) -> str:
        return self.kimi.read_bytes().split(b"\n", 1)[0].split()[-1].decode()

    def run(self, argv, *, timeout=0, env=None):
        self.calls.append(list(argv))
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")
        if argv[-1] == "--version":
            name = Path(argv[0]).name
            if argv[0] == str(self.kimi):
                return ok(self.kimi_version() + "\n")
            if name == "codex" and "/versions/" in argv[0]:
                return ok("codex-cli " + argv[0].split("/versions/")[1].split("/")[0] + "\n")
            return ok(f"{name} {self.versions[name]}\n")
        if argv[:2] == ["npm", "view"]:
            return ok(self.latest[argv[2]] + "\n")
        if argv[:3] == ["npm", "install", "-g"]:
            if self.npm_fail:
                return subprocess.CompletedProcess(argv, 1, "", "registry error")
            package, version = argv[3].rsplit("@", 1)
            self.versions["claude" if "claude" in package else "codex"] = version
            return ok()
        if argv[:3] == ["npm", "install", "--prefix"]:
            binary = Path(argv[3]) / "node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"Ask Codex to do anything Open restricted")
            return ok()
        if argv[:3] == ["brew", "info", "--json=v2"]:
            return ok(json.dumps({"formulae": [{"versions": {"stable": self.latest[argv[3]]}}]}))
        if argv[:2] == ["brew", "upgrade"]:
            self.versions[argv[2]] = self.latest[argv[2]]
            return ok()
        if argv[:2] == ["brew", "update"]:
            return ok()
        if argv[1:3] == ["debug", "prompt-input"]:
            return subprocess.CompletedProcess(argv, 1, "", "offline fixture")
        raise AssertionError(f"unexpected command {argv}")

    def fetch(self, url: str) -> bytes:
        if url.endswith("/latest"):
            return self.kimi_latest.encode()
        if url.endswith("/manifest.json"):
            return json.dumps({"version": self.kimi_latest, "platforms": {
                target: {"filename": "kimi-code-" + target, "checksum": hashlib.sha256(self.kimi_payload).hexdigest()}
                for target in ("darwin-arm64", "darwin-x64")}}).encode()
        if "/binaries/" in url:
            return self.kimi_payload
        raise AssertionError(url)


class CliUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.world = FakeWorld(root)
        self.env = updates.Env(run=self.world.run, fetch=self.world.fetch, state=root / "state",
                               kimi_binary=self.world.kimi, kimi_probe=probe_records)

    def results(self):
        return {row["cli"]: row for row in updates.run(self.env)["results"]}

    def test_nothing_newer_changes_nothing_and_writes_a_report(self):
        rows = self.results()
        self.assertTrue(all(row["action"] == "none" for row in rows.values()))
        self.assertEqual(len(list((self.env.state / "runs").glob("*.json"))), 1)
        self.assertFalse(any(c[:3] == ["npm", "install", "-g"] for c in self.world.calls))

    def test_auto_policies_update_to_the_exact_latest_version(self):
        self.world.latest.update({"@anthropic-ai/claude-code": "2.1.290", "ollama": "0.36.0"})
        rows = self.results()
        self.assertEqual((rows["claude"]["action"], rows["claude"]["installed_after"]), ("updated", "2.1.290"))
        self.assertIn(["npm", "install", "-g", "@anthropic-ai/claude-code@2.1.290"], self.world.calls)
        self.assertEqual(rows["ollama"]["installed_after"], "0.36.0")

    def codex_registry(self, verdict="PASS"):
        import fleet_codex_registry as registry
        store = registry.Registry(self.env.state.parent / "fleet-codex")
        certified = []

        def certify(link):
            certified.append(str(link))
            version = link.parent.parent.name
            return {"schema_version": registry.RECORD_SCHEMA, "status": verdict, "codex_version": version,
                    "binary_sha256": registry.file_sha256(link.resolve()), "bin_dir": str(link.parent),
                    "startup_guard": "codex-0.159-v1", "herdr_version": "0.9.0", "run_dir": "/tmp/run",
                    "finished_at": "2026-10-02T00:00:00+00:00",
                    **({} if verdict == "PASS" else {"failure": {"type": "TimeoutError", "message": "new dialog"}})}
        self.env.codex_registry, self.env.certify = store, certify
        return store, certified

    def test_global_codex_updates_freely_without_a_registry(self):
        self.world.latest["@openai/codex"] = "0.160.0"
        row = self.results()["codex"]
        self.assertEqual((row["action"], row["installed_after"]), ("updated", "0.160.0"))
        self.assertIn("disabled", row["certification"])

    def test_new_codex_is_certified_side_by_side_and_registered(self):
        store, certified = self.codex_registry()
        self.world.latest["@openai/codex"] = "0.160.0"
        row = self.results()["codex"]
        self.assertEqual((row["action"], row["certification"], row["certified"]), ("updated", "PASS", "0.160.0"))
        self.assertEqual(certified, [str(store.bin_dir("0.160.0") / "codex")])
        self.assertEqual(store.latest_certified()["codex_version"], "0.160.0")
        self.assertEqual(self.results()["codex"]["certified"], "0.160.0")
        self.assertEqual(len(certified), 1)

    def test_failed_certification_keeps_missions_on_the_last_certified(self):
        store, certified = self.codex_registry("FAIL")
        self.world.latest["@openai/codex"] = "0.160.0"
        row = self.results()["codex"]
        self.assertEqual((row["certification"], row["certified"]), ("FAIL", None))
        self.assertEqual(self.world.versions["codex"], "0.160.0")
        self.assertIsNone(store.latest_certified())
        again = self.results()["codex"]
        self.assertEqual(again["certification"], "failed_before")
        self.assertEqual(len(certified), 1)

    def test_herdr_is_report_only(self):
        row = self.results()["herdr"]
        self.assertEqual((row["action"], row["pinned"]), ("none", "0.9.0"))

    def test_kimi_installs_natively_with_checksum_and_protocol_check(self):
        self.world.kimi_latest = "2.2.0"
        self.world.kimi_payload = b"kimi 2.2.0\n" + WIRE_15
        row = self.results()["kimi"]
        self.assertEqual((row["action"], row["installed_after"]), ("updated", "2.2.0"))
        self.assertEqual(row["checks"], {"wire_protocol": "1.5", "bridge_supported": True,
                                         "hook_events": ["agent.hook.UserPromptSubmit", "agent.hook.Stop"]})
        self.assertTrue(list((self.env.state / "backups").glob("*/kimi")))

    def test_kimi_with_an_unsupported_wire_protocol_is_never_installed(self):
        self.world.kimi_latest = "2.3.0"
        self.world.kimi_payload = b"kimi 2.3.0\n" + WIRE_16
        row = self.results()["kimi"]
        self.assertEqual(row["action"], "rejected")
        self.assertEqual(row["checks"]["wire_protocol"], "1.6")
        self.assertEqual(self.world.kimi_version(), "2.1.1")

    def test_kimi_checksum_mismatch_installs_nothing(self):
        self.world.kimi_latest = "2.2.0"
        original = self.world.fetch
        self.world.fetch = lambda url: b"tampered" if "/binaries/" in url and not url.endswith(".json") else original(url)
        self.env.fetch = self.world.fetch
        row = self.results()["kimi"]
        self.assertEqual(row["action"], "error")
        self.assertIn("checksum", row["error"])
        self.assertEqual(self.world.kimi_version(), "2.1.1")

    def test_failed_npm_update_reports_without_claiming_success(self):
        self.world.latest["@anthropic-ai/claude-code"] = "2.1.290"
        self.world.npm_fail = True
        row = self.results()["claude"]
        self.assertEqual(row["action"], "error")
        self.assertEqual(self.world.versions["claude"], "2.1.286")

    @unittest.skipUnless(os.environ.get("FLEET_LOCAL_KIMI_PROBE") == "1",
                         "opt-in: runs the installed kimi-code against a loopback fixture")
    def test_installed_kimi_turn_is_accepted_by_bridge_and_frontier(self):
        verdict = updates.judge_kimi_records(updates.kimi_turn_records(Path.home() / ".kimi-code/bin/kimi"))
        self.assertTrue(verdict["bridge_supported"], verdict)

    def test_launch_agent_runs_the_managed_updater_daily(self):
        import plistlib
        value = plistlib.loads(updates.agent_plist(hour=9, minute=30, python="/usr/bin/python3"))
        self.assertEqual(value["Label"], updates.AGENT_LABEL)
        self.assertEqual(value["ProgramArguments"][-1], "run")
        self.assertEqual(value["StartCalendarInterval"], {"Hour": 9, "Minute": 30})
        self.assertEqual(value["EnvironmentVariables"]["KIMI_CODE_NO_AUTO_UPDATE"], "1")


if __name__ == "__main__":
    unittest.main()
