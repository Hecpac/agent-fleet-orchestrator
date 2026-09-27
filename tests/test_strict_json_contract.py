from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

# Components whose bytes are part of a pinned execution contract and cannot
# import fleet_json. Guests are copied alone into their container and run with
# `python3 -I -S`; the Mini consumer is the reviewed reader recorded as
# `consumer_sha256`. Historical verification compares these exact bytes, so a
# stricter decoder needs a versioned runtime, not an in-place edit. The count
# is the exact number of decoding calls tolerated in each file.
PINNED_DECODERS = {
    "scripts/fleet_harness_executor_guest.py": 1,
    "scripts/fleet_harness_guest.py": 1,
    "scripts/fleet_harness_mini_guest.py": 1,
    "scripts/fleet_stats_guest.py": 1,
    "scripts/fleet_harness_mini_terminal.py": 2,
}


def _decoder_calls(tree: ast.AST) -> list[int]:
    imported_decoders: set[str] = set()
    lines: list[int] = []
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
            lines.append(node.lineno)
    return lines


def _repository_imports(tree: ast.AST, modules: set[str]) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names if a.name.split(".")[0] in modules)
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module and node.module.split(".")[0] in modules | {"scripts"}):
                found.add(node.module or ".")
    return found


class StrictJSONContractTests(unittest.TestCase):
    def test_python_runtime_has_one_json_decoder(self) -> None:
        """All runtime JSON decoding must pass through fleet_json."""

        violations: list[str] = []
        pinned: Counter[str] = Counter()
        sources = [
            *sorted((ROOT / "scripts").glob("*.py")),
            *sorted((ROOT / "orchestration" / "agents").glob("*.py")),
        ]
        for path in sources:
            if path.name == "fleet_json.py":
                continue
            relative = str(path.relative_to(ROOT))
            tree = ast.parse(path.read_bytes(), filename=str(path))
            for line in _decoder_calls(tree):
                if relative in PINNED_DECODERS:
                    pinned[relative] += 1
                else:
                    violations.append(f"{relative}:{line}")
        self.assertEqual(
            violations,
            [],
            "runtime JSON decoding bypasses scripts/fleet_json.py",
        )
        self.assertEqual(
            dict(pinned),
            PINNED_DECODERS,
            "pinned decoder exemptions must match their exact call count; "
            "remove stale entries and never add a decoder to a pinned file",
        )

    def test_pinned_decoders_stay_isolated_from_repository_modules(self) -> None:
        """An exemption only covers code that cannot import fleet_json."""

        modules = {path.stem for path in (ROOT / "scripts").glob("*.py")}
        for relative in PINNED_DECODERS:
            with self.subTest(path=relative):
                path = ROOT / relative
                tree = ast.parse(path.read_bytes(), filename=str(path))
                self.assertEqual(_repository_imports(tree, modules), set())


if __name__ == "__main__":
    unittest.main()
