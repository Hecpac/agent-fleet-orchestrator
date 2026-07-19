from __future__ import annotations

import math
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fleet_json  # noqa: E402


class FleetJSONTests(unittest.TestCase):
    def test_loads_accepts_one_utf8_value(self) -> None:
        self.assertEqual(
            fleet_json.loads(b'{"name":"flota","n":1}'),
            {"name": "flota", "n": 1},
        )
        self.assertEqual(fleet_json.loads(bytearray(b"[1,2]")), [1, 2])
        self.assertEqual(fleet_json.loads(memoryview(b'"ok"')), "ok")
        self.assertEqual(fleet_json.loads(rb'"\ud83d\ude00"'), "😀")
        self.assertEqual(fleet_json.loads(b"{}\n\t "), {})

    def test_loads_rejects_ambiguous_or_non_utf8_values(self) -> None:
        invalid = (
            b'{"a":1,"a":2}',
            rb'{"a":1,"\u0061":2}',
            b'{"outer":{"a":1,"a":2}}',
            b'{"n":NaN}',
            b'{"n":Infinity}',
            b'{"n":-Infinity}',
            b'{"n":1e999}',
            b'{"n":-1e999}',
            b"{}{}",
            b"\xef\xbb\xbf{}",
            b'{"x":"\xff"}',
            rb'"\ud800"',
            rb'"\udfff"',
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(fleet_json.FleetJSONError):
                fleet_json.loads(raw)

    def test_canonical_bytes_is_stable_and_rejects_non_json_types(self) -> None:
        self.assertEqual(
            fleet_json.canonical_bytes({"z": 1, "a": "ñ"}),
            b'{"a":"\xc3\xb1","z":1}',
        )
        invalid = (
            {"n": math.nan},
            {"n": math.inf},
            {1: "integer key"},
            {"nested": (1, 2)},
            {"nested": {"items": {1, 2}}},
            b"bytes are not a JSON value",
            "\ud800",
            {"\udfff": "invalid key"},
        )
        for value in invalid:
            with (
                self.subTest(value=repr(value)),
                self.assertRaises(fleet_json.FleetJSONError),
            ):
                fleet_json.canonical_bytes(value)

    def test_recursion_is_normalized_to_fleet_json_error(self) -> None:
        deeply_nested = b"[" * 5_000 + b"0" + b"]" * 5_000
        with self.assertRaises(fleet_json.FleetJSONError):
            fleet_json.loads(deeply_nested)

        circular: list[object] = []
        circular.append(circular)
        with self.assertRaises(fleet_json.FleetJSONError):
            fleet_json.canonical_bytes(circular)

        nested: object = None
        for _ in range(5_000):
            nested = [nested]
        with self.assertRaises(fleet_json.FleetJSONError):
            fleet_json.canonical_bytes(nested)

    def test_jsonl_requires_records_and_canonical_boundaries(self) -> None:
        self.assertEqual(
            fleet_json.load_jsonl(b'{"a":1}\n{"b":2}\n'),
            [{"a": 1}, {"b": 2}],
        )
        self.assertEqual(
            fleet_json.load_jsonl(b'{"x":"a\xc2\x85b\xe2\x80\xa8c"}\n'),
            [{"x": "a\u0085b\u2028c"}],
        )
        self.assertEqual(
            fleet_json.load_jsonl(b'{"a":1}', require_final_newline=False),
            [{"a": 1}],
        )
        invalid = (
            b"",
            b'{"a":1}',
            b'{"a":1}\n\n',
            b'{"a":1}\n{"a":1,"a":2}\n',
            b"{}\r\n",
            b"{}\r{}\n",
            b"{}\xc2\x85{}\n",
            b"{}\xe2\x80\xa8{}\n",
            b"{}\xe2\x80\xa9{}\n",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(fleet_json.FleetJSONError):
                fleet_json.load_jsonl(raw, require_nonempty=True)

        for options in (
            {"require_nonempty": 1},
            {"require_final_newline": 1},
        ):
            with (
                self.subTest(options=options),
                self.assertRaises(fleet_json.FleetJSONError),
            ):
                fleet_json.load_jsonl(b"{}\n", **options)

    def test_canonical_jsonl_rejects_ambiguous_iterables(self) -> None:
        self.assertEqual(
            fleet_json.canonical_jsonl(({"b": 2}, {"a": 1})),
            b'{"b":2}\n{"a":1}\n',
        )
        for values in (None, "text", b"bytes", {"one": 1}):
            with (
                self.subTest(values=values),
                self.assertRaises(fleet_json.FleetJSONError),
            ):
                fleet_json.canonical_jsonl(values)  # type: ignore[arg-type]

    def test_load_wraps_io_errors_and_parses_strictly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "valid.json"
            valid.write_bytes(b'{"ok":true}')
            self.assertEqual(fleet_json.load(valid), {"ok": True})
            with self.assertRaises(fleet_json.FleetJSONError):
                fleet_json.load(root / "missing.json")
            with self.assertRaises(fleet_json.FleetJSONError):
                fleet_json.load("bad\x00path")


if __name__ == "__main__":
    unittest.main()
