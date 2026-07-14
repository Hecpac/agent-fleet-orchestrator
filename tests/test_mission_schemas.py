from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION = ROOT / "orchestration"
FIXTURES = ROOT / "tests" / "fixtures" / "mission_control"


def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate key: {key}")
        value[key] = item
    return value


class MissionSchemaContractTests(unittest.TestCase):
    def test_contract_schemas_are_strict_and_loadable(self) -> None:
        expected = {
            "workflow.schema.json",
            "mission-event.schema.json",
            "delegation.schema.json",
            "archive-index.schema.json",
        }
        for name in expected:
            with self.subTest(name=name):
                schema = json.loads((ORCHESTRATION / name).read_text(encoding="utf-8"))
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(schema["type"], "object")
                self.assertTrue(schema["required"])

    def test_workflow_schema_has_no_runtime_or_authority_escape_hatches(self) -> None:
        schema = json.loads((ORCHESTRATION / "workflow.schema.json").read_text(encoding="utf-8"))
        encoded = json.dumps(schema, sort_keys=True)
        root_properties = set(schema["properties"])
        self.assertNotIn("command", root_properties)
        self.assertNotIn("shell", root_properties)
        self.assertNotIn("authority", encoded)
        self.assertNotIn("tool_access", encoded)
        self.assertNotIn("run_id", encoded)
        self.assertNotIn("surface", encoded)
        self.assertTrue(all(
            value.get("additionalProperties") is False
            for value in schema["properties"].values()
            if isinstance(value, dict) and value.get("type") == "object"
        ))

    def test_valid_fixture_is_json_compatible_yaml(self) -> None:
        workflow = json.loads(
            (FIXTURES / "valid-workflow.json").read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )
        self.assertEqual(workflow["schema_version"], 1)
        self.assertEqual(workflow["autonomy"]["owner"], "lead")
        self.assertEqual(workflow["archive"]["mode"], "incremental")

    def test_duplicate_fixture_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            json.loads(
                (FIXTURES / "invalid-duplicate-workflow.json").read_text(encoding="utf-8"),
                object_pairs_hook=reject_duplicates,
            )

    def test_invalid_fixtures_expose_forbidden_unknown_keys(self) -> None:
        schema = json.loads((ORCHESTRATION / "workflow.schema.json").read_text(encoding="utf-8"))
        root_allowed = set(schema["properties"])
        shell = json.loads((FIXTURES / "invalid-shell-workflow.json").read_text(encoding="utf-8"))
        authority = json.loads((FIXTURES / "invalid-authority-workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(set(shell) - root_allowed, {"command"})
        capabilities_allowed = set(schema["properties"]["capabilities"]["properties"])
        self.assertEqual(set(authority["capabilities"]) - capabilities_allowed, {"authority"})


if __name__ == "__main__":
    unittest.main()
