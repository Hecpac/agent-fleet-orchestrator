from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StrictJSONContractTests(unittest.TestCase):
    def test_python_runtime_has_one_json_decoder(self) -> None:
        """All runtime JSON decoding must pass through fleet_json."""

        violations: list[str] = []
        sources = [
            *sorted((ROOT / "scripts").glob("*.py")),
            *sorted((ROOT / "orchestration" / "agents").glob("*.py")),
        ]
        for path in sources:
            if path.name == "fleet_json.py":
                continue
            tree = ast.parse(path.read_bytes(), filename=str(path))
            imported_decoders: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "json":
                    imported_decoders.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name in {"load", "loads"}
                    )
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                direct_json = (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "json"
                    and function.attr in {"load", "loads"}
                )
                imported_json = (
                    isinstance(function, ast.Name)
                    and function.id in imported_decoders
                )
                if direct_json or imported_json:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(
            violations,
            [],
            "runtime JSON decoding bypasses scripts/fleet_json.py",
        )


if __name__ == "__main__":
    unittest.main()
