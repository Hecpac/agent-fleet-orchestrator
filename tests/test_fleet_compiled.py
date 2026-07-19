from __future__ import annotations

import copy
import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
fleet_compiled = importlib.import_module("fleet_compiled")
fleet_json = importlib.import_module("fleet_json")
workflow_config = importlib.import_module("workflow_config")


class FleetCompiledTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "compiled-workflow.json"
        workflow = workflow_config.load_workflow(
            ROOT / "workflows" / "implementation.yaml"
        )
        router = workflow_config.router_config.load_router()
        self.v2 = workflow_config.compile_workflow(workflow, router=router)

    @staticmethod
    def _seal(value: dict) -> dict:
        value["workflow_digest"] = fleet_json.sha256(value["workflow"])
        unsigned = {
            key: item for key, item in value.items() if key != "compiled_digest"
        }
        value["compiled_digest"] = fleet_json.sha256(unsigned)
        return value

    def _legacy_v1(self) -> dict:
        legacy = copy.deepcopy(self.v2)
        legacy["schema_version"] = 1
        legacy.pop("router_snapshot")
        legacy["workflow"]["audit"].pop("trust_scope")
        for field in (
            "identity_groups",
            "launch_digest",
            "assurance_mode",
            "assurance_launch_digest",
            "assurance_identity_groups",
            "assurance_lead",
            "assurance_instances",
        ):
            legacy["resolved"].pop(field)
        members = [legacy["resolved"]["lead"]] + legacy["resolved"]["instances"]
        for member in members:
            member.pop("runner")
        return self._seal(legacy)

    def _write(self, value: dict) -> Path:
        self.path.write_bytes(fleet_json.canonical_bytes(value) + b"\n")
        return self.path

    def test_v2_is_accepted_for_read_and_effect_with_canonical_digests(self) -> None:
        path = self._write(self.v2)
        self.assertEqual(fleet_compiled.load(path, mode="read"), self.v2)
        self.assertEqual(fleet_compiled.load(path, mode="effect"), self.v2)
        self.assertEqual(
            self.v2["router_digest"], fleet_json.sha256(self.v2["router_snapshot"])
        )
        unsigned = {
            key: item for key, item in self.v2.items() if key != "compiled_digest"
        }
        self.assertEqual(
            self.v2["workflow_digest"], fleet_json.sha256(self.v2["workflow"])
        )
        self.assertEqual(self.v2["compiled_digest"], fleet_json.sha256(unsigned))

    def test_historical_v1_is_readable_but_never_effectful(self) -> None:
        legacy = self._legacy_v1()
        path = self._write(legacy)
        self.assertEqual(fleet_compiled.load(path, mode="read"), legacy)

        intermediate = copy.deepcopy(self.v2)
        intermediate["schema_version"] = 1
        intermediate.pop("router_snapshot")
        for field in (
            "launch_digest",
            "assurance_mode",
            "assurance_launch_digest",
            "assurance_lead",
            "assurance_instances",
        ):
            intermediate["resolved"].pop(field)
        for member in [intermediate["resolved"]["lead"]] + intermediate["resolved"][
            "instances"
        ]:
            member.pop("runner")
        self.assertEqual(
            fleet_compiled.validate(self._seal(intermediate), mode="read"),
            intermediate,
        )

        effect = mock.Mock()

        def consume() -> None:
            compiled = fleet_compiled.load(path, mode="effect")
            effect(compiled)

        with self.assertRaisesRegex(
            fleet_compiled.CompiledError, "historical read-only.*require v2"
        ):
            consume()
        effect.assert_not_called()

    def test_strict_json_rejects_duplicates_nonfinite_trailing_and_invalid_utf8(
        self,
    ) -> None:
        canonical = fleet_json.canonical_bytes(self.v2)
        cases = {
            "duplicate": canonical.replace(b"{", b'{"schema_version":2,', 1),
            "nonfinite": canonical.replace(
                b'"schema_version":2', b'"schema_version":NaN', 1
            ),
            "trailing": canonical + b"{}",
            "invalid-utf8": canonical + b"\xff",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                self.path.write_bytes(raw)
                with self.assertRaises(fleet_compiled.CompiledError):
                    fleet_compiled.load(self.path, mode="read")

    def test_digest_drift_is_rejected_before_an_effect(self) -> None:
        drifted = copy.deepcopy(self.v2)
        drifted["resolved"]["instances"][0]["model"] = "unbound-model"
        path = self._write(drifted)
        effect = mock.Mock()

        def consume() -> None:
            compiled = fleet_compiled.load(path, mode="effect")
            effect(compiled)

        with self.assertRaisesRegex(
            fleet_compiled.CompiledError, "compiled_digest mismatch"
        ):
            consume()
        effect.assert_not_called()

        workflow_drift = copy.deepcopy(self.v2)
        workflow_drift["workflow"]["description"] = "drifted policy"
        with self.assertRaisesRegex(
            fleet_compiled.CompiledError, "workflow_digest mismatch"
        ):
            fleet_compiled.validate(workflow_drift, mode="read")

    def test_v2_requires_an_exact_content_addressed_router_snapshot(self) -> None:
        missing = copy.deepcopy(self.v2)
        missing.pop("router_snapshot")
        with self.assertRaisesRegex(fleet_compiled.CompiledError, "missing fields"):
            fleet_compiled.validate(self._seal(missing), mode="effect")

        digest_drift = copy.deepcopy(self.v2)
        digest_drift["router_digest"] = "f" * 64
        with self.assertRaisesRegex(
            fleet_compiled.CompiledError, "router_snapshot digest mismatch"
        ):
            fleet_compiled.validate(self._seal(digest_drift), mode="effect")

        resealed_plan_drift = copy.deepcopy(self.v2)
        resealed_plan_drift["router_snapshot"]["roles"]["codex"][
            "ready_pattern"
        ] = "a-valid-but-unbound-ready-pattern"
        resealed_plan_drift["router_digest"] = fleet_json.sha256(
            resealed_plan_drift["router_snapshot"]
        )
        self._seal(resealed_plan_drift)
        # Historical/static inspection remains portable, but effect admission
        # proves that both frozen plans came from this exact snapshot.
        self.assertEqual(
            fleet_compiled.validate(resealed_plan_drift, mode="read"),
            resealed_plan_drift,
        )
        with self.assertRaisesRegex(
            fleet_compiled.CompiledError, "snapshot plan binding.*launch drifted"
        ):
            fleet_compiled.validate(resealed_plan_drift, mode="effect")

    def test_resealed_hard_budget_is_rejected_at_effect_admission(self) -> None:
        hard = copy.deepcopy(self.v2)
        hard["workflow"]["limits"].update(
            {"budget_mode": "hard", "token_budget": 1}
        )
        self._seal(hard)
        self.assertEqual(fleet_compiled.validate(hard, mode="read"), hard)
        with self.assertRaisesRegex(
            fleet_compiled.CompiledError, "hard_total.*unsupported providers"
        ):
            fleet_compiled.validate(hard, mode="effect")

    def test_closed_v2_schema_and_exact_bindings_reject_resealed_drift(self) -> None:
        cases: list[tuple[str, dict, str]] = []

        unknown_top = copy.deepcopy(self.v2)
        unknown_top["unbound"] = True
        cases.append(("unknown-top", self._seal(unknown_top), "unknown fields"))

        unknown_resolved = copy.deepcopy(self.v2)
        unknown_resolved["resolved"]["unbound"] = True
        cases.append(
            ("unknown-resolved", self._seal(unknown_resolved), "unknown fields")
        )

        unknown_member = copy.deepcopy(self.v2)
        unknown_member["resolved"]["instances"][0]["command"] = ["unsafe"]
        cases.append(("unknown-member", self._seal(unknown_member), "unknown fields"))

        preset_drift = copy.deepcopy(self.v2)
        preset_drift["resolved"]["preset"] = "research"
        cases.append(("preset-binding", self._seal(preset_drift), "preset binding"))

        adapter_drift = copy.deepcopy(self.v2)
        adapter_drift["resolved"]["lead"]["provider_adapter"] = "claude"
        cases.append(
            (
                "adapter-binding",
                self._seal(adapter_drift),
                "provider_adapter binding",
            )
        )

        writer_drift = copy.deepcopy(self.v2)
        writer_drift["resolved"]["writer_instance"] = "scout"
        cases.append(
            ("writer-binding", self._seal(writer_drift), "writer_instance binding")
        )

        group_drift = copy.deepcopy(self.v2)
        group_drift["resolved"]["identity_groups"][0].append("unknown-instance")
        cases.append(("group-binding", self._seal(group_drift), "unknown instances"))

        boolean_workflow_version = copy.deepcopy(self.v2)
        boolean_workflow_version["workflow"]["schema_version"] = True
        cases.append(
            (
                "boolean-workflow-version",
                self._seal(boolean_workflow_version),
                "workflow.schema_version",
            )
        )

        for name, value, pattern in cases:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(fleet_compiled.CompiledError, pattern),
            ):
                fleet_compiled.validate(value, mode="effect")

    def test_load_mode_is_explicit_and_checked_before_file_access(self) -> None:
        missing = self.path.with_name("does-not-exist.json")
        with self.assertRaisesRegex(fleet_compiled.CompiledError, "load mode"):
            fleet_compiled.load(missing, mode="execute")


if __name__ == "__main__":
    unittest.main()
