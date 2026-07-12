from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_identity  # noqa: E402


class FleetIdentityProbeTests(unittest.TestCase):
    def test_exists_reserves_exit_one_for_confirmed_absence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "fleet-test.manifest"
            manifest.write_text(
                "workspace=workspace:1\n"
                "workspace_uuid=00000000-0000-0000-0000-000000000001\n",
                encoding="utf-8",
            )
            failures = (
                OSError("cmux missing"),
                subprocess.TimeoutExpired(["cmux", "tree"], 10),
            )
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    with mock.patch.object(fleet_identity.subprocess, "run", side_effect=failure):
                        with mock.patch.object(
                            sys, "argv", ["fleet_identity.py", "exists", str(manifest)]
                        ):
                            self.assertEqual(fleet_identity.main(), 2)


if __name__ == "__main__":
    unittest.main()
