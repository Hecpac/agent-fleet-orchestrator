"""No candidate is imported here. Docker lane must be explicitly enabled."""
import copy
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleet_harness_acceptance as checker
import fleet_json
import fleet_harness_sandbox as sandbox
from fleet_safe_paths import SafePathError

FIXTURES = Path(__file__).parent / "fixtures" / "harness_v1"


class AcceptanceTests(unittest.TestCase):
    def test_oracle_has_all_required_families_and_nonempty_controls(self):
        for task in ("D1", "D2"):
            value = checker.suite(task)
            self.assertGreater(len(value["public"]), 30)
            broken = copy.deepcopy(value); broken["public"].pop()
            with self.assertRaises(ValueError): checker.validate_suite(broken)
            with self.assertRaises(ValueError): checker.replay(value, [])

    def test_private_cases_not_in_projection_and_typed_equality(self):
        private = {"id": "private-canary", "family": "reserved", "visibility": "reserved",
            "request": {"op": "usage", "records": [], "mission": "SECRET-HOLDOUT", "admitted": []},
            "expected": {"value": checker.summary(runs=0, observed=0, groups=[]), "errors": None}}
        projected = checker.public_projection(checker.suite("D2", reserved=[private]))
        self.assertNotIn(b"SECRET-HOLDOUT", fleet_json.canonical_bytes(projected))
        case = checker.public_cases("D2")[0]
        value = copy.deepcopy(case["expected"]["value"]); value["total_runs"] = True
        observed = {"value": value, "error": None, "input_after": case["request"]["records"]}
        self.assertIn("value_or_type", checker.assess(case, observed, {}, {}))

    def test_source_admission_exact_pin_required(self):
        with self.assertRaises(ValueError): checker.validate_source_admission(None, {"report.py": "a"})
        review = {"version": checker.SOURCE_POLICY, "manifest": {"report.py": "a"}, "reviewer": "reader",
                  "scope": "bridge-integrity-no-reflection-no-stdout-or-process-interference", "accepted": True}
        checker.validate_source_admission(review, {"report.py": "a"})
        with self.assertRaises(ValueError): checker.validate_source_admission(review, {"report.py": "b"})

    def test_failed_preflight_is_retained_and_replays_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(sandbox.legacy, "Docker", side_effect=sandbox.legacy.RunnerBlocked("offline")):
                result = checker.run(FIXTURES / "d2", checker.suite("D2"), temporary, binding={"revision": "r"})
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(checker.verify(temporary, expected_binding={"revision": "r"}), result)


@unittest.skipUnless(os.environ.get("FLEET_HARNESS_LOCAL_TESTS") == "1", "explicit owned local Docker lane required")
class IsolatedAcceptanceTests(unittest.TestCase):
    def run_candidate(self, task, *, mutation=None, reviewed=False, inspect=None):
        with tempfile.TemporaryDirectory(prefix="fleet-check-v1-") as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            shutil.copytree(FIXTURES / task.lower(), candidate)
            if mutation:
                mutation(candidate)
            review = fleet_json.loads((FIXTURES / "source-admissions.json").read_bytes())[task] if reviewed else None
            result = checker.run(candidate, checker.suite(task), root / "check",
                binding={"test": self.id()}, source_admission=review)
            self.assertTrue(result["cleanup"], result)
            self.assertEqual(checker.verify(root / "check", expected_binding={"test": self.id()}), result)
            with self.assertRaises(ValueError):
                checker.verify(root / "check", expected_binding={"test": "wrong revision"})
            if inspect:
                inspect(root / "check")
            return result

    def test_positive_controls_behavior_without_self_admission(self):
        for task in ("D1", "D2"):
            with self.subTest(task=task):
                result = self.run_candidate(task)
                self.assertEqual(result["status"], "blocked", result)
                self.assertTrue(result["results"], result)
                self.assertTrue(all(r["passed"] for r in result["results"]), [r for r in result["results"] if not r["passed"]])
                self.assertEqual(result["pending"], ["independent_source_admission_required"])

    def test_independently_reviewed_positive_controls_pass(self):
        for task in ("D1", "D2"):
            with self.subTest(task=task):
                self.assertEqual(self.run_candidate(task, reviewed=True)["status"], "passed")

    def test_extra_shadow_modules_and_bytecode_never_enter_sandbox(self):
        def shadow(candidate):
            (candidate / "tempfile.py").write_text('raise RuntimeError("shadow executed")\n')
            (candidate / "__pycache__").mkdir()
            (candidate / "__pycache__" / "ledger.cpython-312.pyc").write_bytes(b"unreviewed")
        self.assertEqual(self.run_candidate("D1", mutation=shadow, reviewed=True)["status"], "passed")

    def test_negative_controls_per_contract_family(self):
        mutations = [
            ("D1", "exact-types", "ledger.py", "type(generation) is not int", "not isinstance(generation, int)"),
            ("D1", "immutability", "ledger.py", "@dataclass(frozen=True)", "@dataclass(frozen=False)"),
            ("D1", "paths", "paths.py", 'if any(part in {"", ".", "..", ".git"} for part in parts):', 'if False:'),
            ("D1", "symlinks", "paths.py", "if target.is_symlink():", "if False:"),
            ("D1", "generations", "ledger.py", "if generation < old.generation:", "if False:"),
            ("D1", "generations", "ledger.py", "                if path != old.path or sha != old.sha256:", "                if path == old.path:\n                    return old\n                if path != old.path or sha != old.sha256:"),
            ("D1", "generations", "ledger.py", "                if path != old.path or sha != old.sha256:", "                if sha == old.sha256:\n                    return old\n                if path != old.path or sha != old.sha256:"),
            ("D1", "ownership", "ledger.py", 'if path in self._owners and self._owners[path] != run_id:', 'if False:'),
            ("D1", "replay", "ledger.py", 'if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != sha:', 'if False:'),
            ("D1", "idempotence", "ledger.py", '                return old', '                target.write_bytes(data)\n                return old'),
            ("D1", "failed-publication", "ledger.py", '        missing = []', '        self._owners[path] = run_id\n        missing = []'),
            ("D1", "atomicity", "ledger.py", '            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, delete=False) as stream:\n                temporary = stream.name\n                stream.write(data)', '            target.write_bytes(data)\n            temporary = str(target)'),
            ("D2", "selection", "report.py", 'record.get("mission_id") != mission_id', 'False'),
            ("D2", "global-maximum", "report.py", '        identities[run] = identity', '        if any(old["sequence"] == sequence and old != record for old in selected.get(run, [])):\n            raise ValueError("premature tie")\n        identities[run] = identity'),
            ("D2", "latest-types", "report.py", 'for record in latest:', 'for record in latest[:1]:'),
            ("D2", "identity", "report.py", 'if run in identities and identities[run] != identity:', 'if False:'),
            ("D2", "identity", "report.py", '            raise ValueError("invalid identity")', '            pass'),
            ("D2", "mapping-equality", "report.py", 'if any(record != latest[0] for record in latest[1:]):', 'if False:'),
            ("D2", "unknown-propagation", "report.py", 'if complete else None', 'if True else None'),
            ("D2", "sequence", "report.py", 'type(sequence) is not int', 'not isinstance(sequence, int)'),
            ("D2", "aggregation", "report.py", 'sum(record["prompt_tokens"] for record in records)', '2 * sum(record["prompt_tokens"] for record in records)'),
            ("D2", "aggregation", "report.py", 'sum(record["prompt_tokens"] for record in records)', 'records[0]["prompt_tokens"]'),
            ("D2", "structure", "report.py", '"groups": result}', '"groups": list(reversed(result))}'),
            ("D2", "latest-types", "report.py", '                    raise ValueError("invalid counter")', '                    record["cached_input_tokens"] = 999\n                    raise ValueError("invalid counter")'),
        ]
        for task, family, filename, old, new in mutations:
            with self.subTest(task=task, family=family, old=old):
                def mutate(candidate):
                    path = candidate / filename
                    content = path.read_text(); self.assertIn(old, content)
                    path.write_text(content.replace(old, new))
                result = self.run_candidate(task, mutation=mutate)
                self.assertEqual(result["status"], "failed", result)
                self.assertTrue(any(r["family"] == family and not r["passed"] for r in result["results"]), result)

    def test_forged_checker_verdict_is_rejected(self):
        def forge(candidate):
            (candidate / "report.py").write_text('import os\nprint(\'{"passed":true}\', flush=True)\nos._exit(0)\n')
        result = self.run_candidate("D2", mutation=forge)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["results"])

    def test_mutable_returned_copy_rejected_by_new_explicit_public_policy(self):
        def mutable_copy(candidate):
            path=candidate/"ledger.py"
            source=path.read_text().replace("@dataclass(frozen=True)","@dataclass(frozen=False)")
            source=source.replace("return old","return __import__('copy').copy(old)").replace("return result","return __import__('copy').copy(result)")
            path.write_text(source)
        result=self.run_candidate("D1",mutation=mutable_copy)
        self.assertTrue(any(r["family"]=="immutability" and not r["passed"] for r in result["results"]))

    def test_pass_requires_original_sources_runtime_streams_and_typed_wire(self):
        def corrupt(store):
            def verify():
                checker.verify(store, expected_binding={"test": self.id()})
            for name in ("sources/report.py", "resource/created-inspect.json", "resource/terminal-streams.json"):
                path = store / name; raw = path.read_bytes(); mode = path.stat().st_mode & 0o777
                path.unlink()
                with self.assertRaises((ValueError, OSError, SafePathError)): verify()
                path.write_bytes(raw); path.chmod(mode)
                verify()
            for name, edit in (
                ("resource/created-inspect.json", lambda value: value["HostConfig"].update(NetworkMode="host")),
                ("resource/terminal-streams.json", lambda value: value.update(owner="another-resource")),
                ("resource/cleanup.json", lambda value: value["resource"].update(cid="another-container")),
                ("result.json", lambda value: value.update(results=[])),
            ):
                path = store / name; raw = path.read_bytes(); value = fleet_json.loads(raw); edit(value)
                path.write_bytes(fleet_json.canonical_bytes(value))
                with self.assertRaises(ValueError): verify()
                path.write_bytes(raw)
            with self.assertRaises(ValueError):
                checker.run(FIXTURES / "d2", checker.suite("D2"), store, binding={"test": self.id()})
            verify()
        self.assertEqual(self.run_candidate("D2", reviewed=True, inspect=corrupt)["status"], "passed")


if __name__ == "__main__":
    unittest.main()
