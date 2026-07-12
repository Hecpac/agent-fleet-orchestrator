from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "orchestration" / "router.yaml"
SKILL_COPIES = (
    ROOT / ".agents" / "skills" / "cmux" / "SKILL.md",
    ROOT / ".claude" / "skills" / "cmux" / "SKILL.md",
)


class SpecCoherenceTests(unittest.TestCase):
    """The router is the single source of truth; prose must not drift from it."""

    def test_skill_copies_are_identical(self) -> None:
        agents_copy, claude_copy = (path.read_text(encoding="utf-8") for path in SKILL_COPIES)
        self.assertEqual(
            agents_copy,
            claude_copy,
            ".agents and .claude copies of the cmux SKILL have drifted apart",
        )

    def test_skill_schema_version_matches_router(self) -> None:
        router_version = json.loads(ROUTER.read_text(encoding="utf-8"))["schema_version"]
        for path in SKILL_COPIES:
            versions = re.findall(r"^schema_version=(\d+)$", path.read_text(encoding="utf-8"), re.M)
            self.assertTrue(versions, f"{path} shows no manifest schema_version example")
            for version in versions:
                self.assertEqual(
                    int(version),
                    router_version,
                    f"{path} shows schema_version={version}; router.yaml is {router_version}",
                )


if __name__ == "__main__":
    unittest.main()
