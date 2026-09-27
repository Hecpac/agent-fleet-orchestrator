from __future__ import annotations

import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_archive_tree as tree


class ArchiveTreeTests(unittest.TestCase):
    def test_verifier_imports_without_execution_or_mission_modules(self) -> None:
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
class RejectExecutionImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"fleet_archive", "fleet_mission", "fleet_mission_state",
                        "fleet_herdr", "fleet_providers", "subprocess", "socket"}:
            raise AssertionError("verification imported execution: " + fullname)
sys.meta_path.insert(0, RejectExecutionImports())
import fleet_archive_tree
assert fleet_archive_tree.tree_hash_from_tar(b"\\0" * 10240) == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
'''
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(ROOT / "scripts")],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_raw_regular_file_reproduces_both_git_object_formats(self) -> None:
        payload = b"candidate content\n"
        for algorithm in ("sha1", "sha256"):
            with self.subTest(algorithm=algorithm):
                blob = hashlib.new(algorithm, b"blob " + str(len(payload)).encode() + b"\0" + payload).digest()
                body = b"100644 candidate.txt\0" + blob
                expected = hashlib.new(algorithm, b"tree " + str(len(body)).encode() + b"\0" + body).hexdigest()
                output = io.BytesIO()
                with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    member = tarfile.TarInfo("candidate.txt")
                    member.size = len(payload)
                    member.mode = 0o644
                    member.pax_headers = {tree.GIT_MODE_PAX: "100644", tree.GIT_OID_PAX: blob.hex()}
                    archive.addfile(member, io.BytesIO(payload))
                self.assertEqual(tree.tree_hash_from_tar(output.getvalue(), algorithm), expected)

    def test_tampered_raw_blob_binding_is_rejected(self) -> None:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo("candidate.txt")
            member.size = 1
            member.mode = 0o644
            member.pax_headers = {tree.GIT_MODE_PAX: "100644", tree.GIT_OID_PAX: "a" * 40}
            archive.addfile(member, io.BytesIO(b"x"))
        with self.assertRaisesRegex(tree.ArchiveError, "oid does not match tar content"):
            tree.tree_hash_from_tar(output.getvalue())

    def test_traversal_and_invalid_tar_remain_rejected(self) -> None:
        for path in ("", "/absolute", "../escape", "nested/../escape"):
            with self.subTest(path=path), self.assertRaises(tree.ArchiveError):
                tree.safe_relative(path)
        with self.assertRaisesRegex(tree.ArchiveError, "invalid"):
            tree.tree_hash_from_tar(b"not a tar")


if __name__ == "__main__":
    unittest.main()
