from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import uuid

from tests.mission_control_test_support import create_running_mission

import fleet_delegation


class FleetDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.runs, self.mission_id, self.lead_run_id = create_running_mission(
            self.tmp, feature="delegation"
        )

    def issue(self):
        delegation_id = str(uuid.uuid4())
        token = fleet_delegation.issue_token(
            self.runs,
            self.mission_id,
            delegation_id=delegation_id,
            parent_run_id=self.lead_run_id,
            can_delegate=True,
            allowed_capabilities=["challenge"],
            current_depth=1,
            max_depth=3,
            remaining_budget=1,
            writer_instance="builder",
            idempotency_key="token:one",
        )
        child_run = str(uuid.uuid4())
        fleet_delegation.bind_token(
            self.runs,
            self.mission_id,
            token_id=token["token_id"],
            delegation_id=delegation_id,
            run_id=child_run,
            idempotency_key="token:bind",
        )
        return token, child_run

    def test_authorized_token_is_ledger_bound_and_scoped(self) -> None:
        token, child_run = self.issue()
        verified = fleet_delegation.validate_for_subdelegation(
            self.runs,
            self.mission_id,
            token_id=token["token_id"],
            parent_run_id=child_run,
            capability="challenge",
        )
        self.assertEqual(verified["writer_instance"], "builder")
        with self.assertRaisesRegex(fleet_delegation.DelegationError, "outside delegated scope"):
            fleet_delegation.validate_for_subdelegation(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                parent_run_id=child_run,
                capability="build",
            )

    def test_token_tampering_is_rejected_even_if_file_remains_valid_json(self) -> None:
        token, child_run = self.issue()
        path = Path(token["path"])
        value = json.loads(path.read_text())
        value["allowed_capabilities"].append("build")
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(fleet_delegation.DelegationError, "hash mismatch"):
            fleet_delegation.validate_for_subdelegation(
                self.runs,
                self.mission_id,
                token_id=token["token_id"],
                parent_run_id=child_run,
                capability="build",
            )


if __name__ == "__main__":
    unittest.main()
