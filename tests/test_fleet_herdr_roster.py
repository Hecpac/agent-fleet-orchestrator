"""Focal contract tests for the Herdr six-role roster validator (FLEET-01).

Provider-free, offline and stdlib-only. The module under test never launches a
CLI, Herdr session, provider or agent; the CLI smoke tests invoke only this
repository's own Python entry point. ``workspace_access`` is treated as a
contract declaration, not as OS or sandbox attestation.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _candidate in (HERE, HERE.parent / "scripts"):
    if (_candidate / "fleet_herdr_roster.py").is_file():
        sys.path.insert(0, str(_candidate))
        break

import fleet_herdr_roster as roster


def manifest_path() -> Path:
    for candidate in (
        HERE / "herdr-six-role-v1.json",
        HERE.parent / "orchestration" / "fleet" / "herdr-six-role-v1.json",
    ):
        if candidate.is_file():
            return candidate
    raise AssertionError("herdr-six-role-v1.json not found next to the tests or in orchestration/fleet")


class RosterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = manifest_path()
        cls.script = Path(roster.__file__).resolve()

    def valid(self) -> dict:
        return roster.loads_strict(self.manifest.read_bytes())

    def assert_invalid(self, mutate, message: str = "") -> None:
        value = self.valid()
        if mutate is not None:
            mutate(value)
        with self.assertRaises(ValueError, msg=message):
            roster.validate_roster(value)

    def test_manifest_is_valid_and_declares_exact_posts(self):
        source = self.valid()
        normalized = roster.validate_roster(source)
        self.assertEqual(normalized, source)
        self.assertIsNot(normalized, source)
        for normalized_role, source_role in zip(normalized["roles"], source["roles"]):
            self.assertIsNot(normalized_role, source_role)
        self.assertEqual(normalized["schema_version"], "herdr.fleet.roster.v1")
        self.assertEqual(normalized["mode"], "supervised-maintenance")
        self.assertIs(normalized["mission_enabled"], False)
        self.assertEqual(normalized["canonical_writer"], "worker_sol")
        self.assertEqual(normalized["input_policy"], "independent-v1")
        self.assertEqual(normalized["closure_authority"], "controller")
        self.assertEqual(normalized["max_canonical_writers"], 1)
        self.assertEqual(
            [row["id"] for row in normalized["roles"]],
            ["lead", "research", "worker_sol", "worker_deepseek", "reviewer", "verifier"],
        )
        self.assertEqual(
            [row["workspace_access"] for row in normalized["roles"]],
            [
                "read-only",
                "read-only",
                "canonical-candidate",
                "isolated-contribution",
                "read-only",
                "read-only",
            ],
        )
        self.assertEqual(
            [(row["cli"], row["provider"], row["model"]) for row in normalized["roles"]],
            [
                ("codex", "openai", "gpt-6-astra"),
                ("codex", "openai", "gpt-6-astra"),
                ("codex", "openai", "gpt-5.6-sol"),
                ("opencode", "deepseek", "deepseek-flash"),
                ("codex", "openai", "gpt-5.6-sol"),
                ("codex", "openai", "gpt-5.6-sol"),
            ],
        )

    def test_second_canonical_writer_is_rejected(self):
        def promote_deepseek(value):
            value["roles"][3]["workspace_access"] = "canonical-candidate"

        self.assert_invalid(promote_deepseek)
        self.assert_invalid(lambda v: v.update(max_canonical_writers=2))
        self.assert_invalid(lambda v: v.update(canonical_writer="lead"))

    def test_research_and_readers_cannot_write(self):
        for index in (0, 1, 4, 5):

            def mutate(value, index=index):
                value["roles"][index]["workspace_access"] = "canonical-candidate"

            self.assert_invalid(mutate, message=f"role index {index}")

    def test_worker_read_only_is_rejected(self):
        self.assert_invalid(lambda v: v["roles"][2].update(workspace_access="read-only"))

    def test_missing_or_duplicate_role_is_rejected(self):
        self.assert_invalid(lambda v: v["roles"].pop())
        self.assert_invalid(lambda v: v["roles"].append(copy.deepcopy(v["roles"][0])))
        self.assert_invalid(lambda v: v["roles"][5].update(id="reviewer"))

    def test_wrong_identity_is_rejected(self):
        self.assert_invalid(lambda v: v["roles"][0].update(provider="anthropic"))
        self.assert_invalid(lambda v: v["roles"][3].update(provider="openai"))
        self.assert_invalid(lambda v: v["roles"][0].update(cli="opencode"))
        self.assert_invalid(lambda v: v["roles"][3].update(cli="codex"))
        self.assert_invalid(lambda v: v["roles"][0].update(model="gpt-5.6-sol"))
        self.assert_invalid(lambda v: v["roles"][4].update(model="deepseek-flash"))
        self.assert_invalid(lambda v: v["roles"][3].update(reasoning_requested="high"))
        self.assert_invalid(lambda v: v["roles"][0].update(reasoning_requested="thinking"))
        self.assert_invalid(lambda v: v["roles"][0].update(deliverable=""))
        self.assert_invalid(lambda v: v["roles"][4].update(deliverable="plan-and-synthesis"))

    def test_boolean_or_number_confusion_is_rejected(self):
        for confused in (0, 1, "false", None):
            self.assert_invalid(lambda v, c=confused: v.update(mission_enabled=c))
        self.assert_invalid(lambda v: v.update(mission_enabled=True))
        for confused in (True, False, 1.0, "1", None):
            self.assert_invalid(lambda v, c=confused: v.update(max_canonical_writers=c))
        self.assert_invalid(lambda v: v.update(schema_version=1))
        self.assert_invalid(lambda v: v["roles"][0].update(reasoning_requested=True))
        self.assert_invalid(lambda v: v["roles"][0].update(id=1))

    def test_mission_must_stay_disabled(self):
        self.assert_invalid(lambda v: v.update(mission_enabled=True))

    def test_non_independent_policy_is_rejected(self):
        self.assert_invalid(lambda v: v.update(input_policy="shared-v1"))
        self.assert_invalid(lambda v: v.update(closure_authority="lead"))

    def test_unknown_fields_are_rejected(self):
        self.assert_invalid(lambda v: v.update(extra=True))
        self.assert_invalid(lambda v: v.pop("input_policy"))
        self.assert_invalid(lambda v: v["roles"][0].update(notes="nope"))
        self.assert_invalid(lambda v: v["roles"][0].pop("deliverable"))

    def test_role_field_types_are_strict(self):
        self.assert_invalid(lambda v: v["roles"][0].update(role="admin"))
        self.assert_invalid(lambda v: v["roles"][0].update(cli="Codex"))
        self.assert_invalid(lambda v: v["roles"][0].update(workspace_access="write"))

    def test_duplicate_json_keys_are_rejected(self):
        raw = b'{"schema_version": "herdr.fleet.roster.v1", "schema_version": "x"}'
        with self.assertRaises(ValueError):
            roster.loads_strict(raw)

    def test_non_finite_json_constants_are_rejected(self):
        for literal in (b'{"mission_enabled": NaN}', b'{"mission_enabled": Infinity}',
                        b'{"mission_enabled": -Infinity}'):
            with self.subTest(literal=literal):
                with self.assertRaises(ValueError):
                    roster.loads_strict(literal)

    def test_load_roster_rejects_missing_file(self):
        with self.assertRaises(ValueError):
            roster.load_roster(self.manifest.parent / "does-not-exist.json")

    def test_cli_valid_manifest_returns_protocol_and_exit_zero(self):
        completed = subprocess.run(
            [sys.executable, "-B", str(self.script), str(self.manifest)],
            capture_output=True,
            text=True,
            cwd=str(HERE),
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            set(payload),
            {
                "valid",
                "mission_enabled",
                "schema_version",
                "mode",
                "canonical_writer",
                "max_canonical_writers",
                "input_policy",
                "closure_authority",
                "role_count",
                "role_ids",
            },
        )
        self.assertIs(payload["valid"], True)
        self.assertIs(payload["mission_enabled"], False)
        self.assertEqual(payload["canonical_writer"], "worker_sol")
        self.assertEqual(payload["max_canonical_writers"], 1)
        self.assertEqual(payload["role_count"], 6)
        self.assertEqual(
            payload["role_ids"],
            ["lead", "research", "worker_sol", "worker_deepseek", "reviewer", "verifier"],
        )
        self.assertNotIn("error", payload)

    def test_cli_invalid_manifest_returns_error_and_exit_one(self):
        value = self.valid()
        value["mission_enabled"] = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.json"
            path.write_text(json.dumps(value))
            completed = subprocess.run(
                [sys.executable, "-B", str(self.script), str(path)],
                capture_output=True,
                text=True,
                cwd=str(HERE),
                timeout=30,
            )
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(set(payload), {"valid", "error"})
        self.assertIs(payload["valid"], False)
        self.assertTrue(payload["error"])

    def test_cli_usage_error_is_json_and_exit_one(self):
        completed = subprocess.run(
            [sys.executable, "-B", str(self.script)],
            capture_output=True,
            text=True,
            cwd=str(HERE),
            timeout=30,
        )
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(set(payload), {"valid", "error"})
        self.assertIs(payload["valid"], False)


if __name__ == "__main__":
    unittest.main()
