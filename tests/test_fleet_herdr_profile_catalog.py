"""The Herdr profile catalog is closed: identities are pinned and every copy agrees."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_herdr_mission  # noqa: E402
import fleet_herdr_permissions as permissions  # noqa: E402
import fleet_herdr_profile as profiles  # noqa: E402
import fleet_herdr_rejection as rejection  # noqa: E402
import fleet_mission_state as state  # noqa: E402
import fleet_workflow_contract as workflow_contract  # noqa: E402

# Ledgers bind these digests as herdr_profile_sha256. Changing an existing profile
# must be a new profile id, never an edit that silently re-reads history.
PINNED_DIGESTS = {
    "astra_sol_v1": "643b70ab6cbc7e99096f5aebe676f42c03d3c3aa12acab62ec0d40beb041c439",
    "astra_sol_research_v1": "a9b645de910ecdecef696152c6efbe51cd46baf6a17647d5150e1fc587c9e3f1",
    "sol_minimal_v1": "5994452d852b1368dacf8b447af486e867398070c90d0e539cdd9060c20c53ef",
}


def role_models(profile: profiles.HerdrProfile) -> dict[str, str]:
    return {instance: model for instance, _, model, _, _ in profile.members}


class ProfileCatalogIdentityTests(unittest.TestCase):
    def test_existing_profile_identities_are_pinned(self) -> None:
        catalog = {p.profile_id: p.digest for p in (profiles.LEGACY, profiles.RESEARCH, profiles.MINIMAL)}
        self.assertEqual(catalog, PINNED_DIGESTS)

    def test_permission_policy_copies_match_the_catalog(self) -> None:
        self.assertEqual(permissions.VERSION, profiles.LEGACY.permissions_policy_version)
        self.assertEqual(permissions.RESEARCH_VERSION, profiles.RESEARCH.permissions_policy_version)
        self.assertEqual(permissions.MINIMAL_VERSION, profiles.MINIMAL.permissions_policy_version)
        self.assertEqual(permissions.REQUIRED_TURNS, len(profiles.LEGACY.stages))
        self.assertEqual(permissions.MINIMUM_ARCHIVE_SCHEMA_VERSION,
                         profiles.LEGACY.minimum_archive_schema_version)
        self.assertEqual(permissions.MODELS, role_models(profiles.LEGACY))
        self.assertEqual(permissions.RESEARCH_MODELS, role_models(profiles.RESEARCH))
        self.assertEqual(permissions.MINIMAL_MODELS, role_models(profiles.MINIMAL))

    def test_rejection_reads_the_profile_permission_version(self) -> None:
        cases = {None: profiles.LEGACY, "astra_sol_v1": profiles.LEGACY,
                 "astra_sol_research_v1": profiles.RESEARCH, "sol_minimal_v1": profiles.MINIMAL}
        for binding, profile in cases.items():
            with self.subTest(binding=binding):
                backend = {} if binding is None else {"herdr_profile": binding}
                self.assertEqual(rejection._permission_version(backend), profile.permissions_policy_version)
        # Historical behaviour: an unvalidated unknown binding keeps version 1.
        self.assertEqual(rejection._permission_version({"herdr_profile": "unknown"}), 1)

    def test_legacy_roster_copy_matches_the_catalog(self) -> None:
        self.assertEqual(fleet_herdr_mission.PROFILE, profiles.LEGACY.members)


class LedgerContractPinTests(unittest.TestCase):
    @staticmethod
    def finalization(profile: profiles.HerdrProfile, **changes) -> dict:
        payload = {
            "compiled_digest": "a" * 64,
            "minimum_archive_schema_version": profile.minimum_archive_schema_version,
            "permissions_policy_version": profile.permissions_policy_version,
            "required_turns": len(profile.stages),
            "herdr_profile": profile.profile_id,
            "herdr_profile_sha256": profile.digest,
        }
        payload.update(changes)
        return payload

    def test_versioned_finalization_contracts_follow_the_catalog(self) -> None:
        for profile in (profiles.RESEARCH, profiles.MINIMAL):
            with self.subTest(profile=profile.profile_id):
                state._validate_payload("herdr_finalization_policy_frozen", self.finalization(profile))
                for field in ("minimum_archive_schema_version", "permissions_policy_version", "required_turns"):
                    wrong = self.finalization(profile, **{field: self.finalization(profile)[field] + 1})
                    with self.assertRaises(state.MissionStateError):
                        state._validate_payload("herdr_finalization_policy_frozen", wrong)

    def test_legacy_finalization_contract_follows_the_catalog(self) -> None:
        legacy = self.finalization(profiles.LEGACY)
        for key in ("herdr_profile", "herdr_profile_sha256"):
            legacy.pop(key)
        state._validate_payload("herdr_finalization_policy_frozen", legacy)
        with self.assertRaises(state.MissionStateError):
            state._validate_payload("herdr_finalization_policy_frozen",
                                    {**legacy, "required_turns": len(profiles.LEGACY.stages) + 1})


class CatalogPinTests(unittest.TestCase):
    """Facts outside HerdrProfile.contract() are pinned here, since no digest covers them."""

    def test_catalog_accessors(self) -> None:
        self.assertEqual(profiles.BY_PROFILE_ID, {p.profile_id: p for p in
                                                  (profiles.LEGACY, profiles.RESEARCH, profiles.MINIMAL)})
        self.assertEqual(profiles.VERSIONED, (profiles.RESEARCH, profiles.MINIMAL))
        self.assertIs(profiles.PHYSICAL_SCOPE_PROFILE, profiles.MINIMAL)

    def test_ledger_validator_constants_equal_the_catalog(self) -> None:
        self.assertEqual(state.VERSIONED_HERDR_PROFILE_IDS, {p.profile_id for p in profiles.VERSIONED})
        self.assertEqual(state.PHYSICAL_SCOPE_PROFILE_ID, profiles.PHYSICAL_SCOPE_PROFILE.profile_id)
        self.assertEqual(state.VERSIONED_FINALIZATION_CONTRACTS, {
            p.profile_id: (p.minimum_archive_schema_version, p.permissions_policy_version, len(p.stages))
            for p in profiles.VERSIONED})
        legacy = profiles.LEGACY
        self.assertEqual(state.LEGACY_FINALIZATION_CONTRACT, {
            "minimum_archive_schema_version": {legacy.minimum_archive_schema_version},
            # 2 is the legacy capsule execution policy, outside the profile contract.
            "permissions_policy_version": {legacy.permissions_policy_version, 2},
            "required_turns": {len(legacy.stages)},
        })

    def test_archive_reader_residual_literals_match_the_catalog(self) -> None:
        # fleet_herdr_archive.verify still selects the profile by archive schema
        # (6 -> Research, else Minimal) and allows permission versions {1, 2, 3, 4}.
        # Deriving them belongs to the archive reader work; this pin is the tripwire.
        self.assertEqual({p.archive_schema_version: p for p in profiles.VERSIONED},
                         {6: profiles.RESEARCH, 7: profiles.MINIMAL})
        self.assertEqual({1, 2, 3, 4}, {profiles.LEGACY.permissions_policy_version, 2,
                                        profiles.RESEARCH.permissions_policy_version,
                                        profiles.MINIMAL.permissions_policy_version})

    def test_work_owner_is_the_first_stage_instance(self) -> None:
        for profile in (profiles.LEGACY, profiles.RESEARCH, profiles.MINIMAL):
            with self.subTest(profile=profile.profile_id):
                owner = workflow_contract.WORK_OWNER_BY_PRESET.get(
                    profile.preset, workflow_contract.DEFAULT_WORK_OWNER)
                self.assertEqual(owner, profile.stages[0][1])


if __name__ == "__main__":
    unittest.main()
