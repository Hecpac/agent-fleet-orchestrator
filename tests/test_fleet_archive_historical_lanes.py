"""Explicit historical archive read lanes stay exact, labelled and read-only."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_archive  # noqa: E402
import fleet_compiled  # noqa: E402
import fleet_json  # noqa: E402
import router_config  # noqa: E402
import workflow_config  # noqa: E402

MID = "2734160e-1241-5f06-adab-ffef2af33e1f"
BASE = "60b5b6fdf24627ff6340d54030bc3570ef76e023"


def pre_provider_router() -> dict:
    router = copy.deepcopy(router_config.load_router())
    router.pop("providers")
    for role in router["roles"].values():
        role.pop("transport", None)
        role.pop("num_predict", None)
    return router


class RouterPreProviderContractTests(unittest.TestCase):
    def test_pre_provider_shape_validates_only_on_the_historical_lane(self) -> None:
        router = pre_provider_router()
        router_config.validate_router(router, provider_contract=False)
        with self.assertRaisesRegex(router_config.RouterError, "missing fields: providers"):
            router_config.validate_router(router)

    def test_historical_lane_rejects_every_provider_contract_field(self) -> None:
        with self.assertRaisesRegex(router_config.RouterError, "unknown fields: providers"):
            router_config.validate_router(router_config.load_router(), provider_contract=False)
        router = pre_provider_router()
        interactive = next(r for r in router["roles"].values() if r["runner"] == "interactive")
        interactive["transport"] = "pointer"
        with self.assertRaisesRegex(router_config.RouterError, "unknown fields: transport"):
            router_config.validate_router(router, provider_contract=False)

    def test_compilation_never_accepts_a_pre_provider_router(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "router.json"
            path.write_bytes(fleet_json.canonical_bytes(pre_provider_router()))
            with self.assertRaisesRegex(workflow_config.WorkflowError, "providers"):
                workflow_config.compile_path(ROOT / "workflows/implementation.yaml", router_path=path)

    def test_effect_mode_refuses_pre_provider_snapshots(self) -> None:
        with self.assertRaisesRegex(fleet_compiled.CompiledError, "historical read-only"):
            fleet_compiled.validate({}, mode="effect", provider_contract=False)


def manifest_v2(**changes: str | None) -> dict[str, str]:
    value = {
        "manifest_contract_version": "2",
        "feature": "mc-s10-writer-20260714",
        "mission_id": MID,
        "target_repo": "/private/tmp/target",
        "builder.authority": "write",
        "builder.branch": "fleet/mc-s10-writer-20260714/builder",
        "builder.worktree": "/tmp/fleet_workspaces/builder",
        "builder.base_sha": BASE,
        "builder.final_sha": BASE,
    }
    for key, item in changes.items():
        key = key.replace("__", ".")
        if item is None:
            value.pop(key, None)
        else:
            value[key] = item
    return value


STATE = {"feature": "mc-s10-writer-20260714", "mission_id": MID,
         "target_repo": "/private/tmp/target", "base_sha": BASE}


class ManifestContractV2LaneTests(unittest.TestCase):
    def test_binds_the_base_through_its_single_writer(self) -> None:
        manifest = manifest_v2()
        self.assertTrue(fleet_archive._is_manifest_contract_v2(manifest))
        self.assertEqual(fleet_archive._archive_manifest_binding_v2(manifest, STATE),
                         ("builder", "fleet/mc-s10-writer-20260714/builder"))

    def test_contract_v3_publication_fields_are_not_mixed_in(self) -> None:
        for key in ("workspace__quiesced", "builder__git_isolation",
                    "builder__publication_state", "builder__published_sha"):
            with self.subTest(key=key), self.assertRaisesRegex(
                    fleet_archive.ArchiveError, "contract v3 publication fields"):
                fleet_archive._archive_manifest_binding_v2(manifest_v2(**{key: "x"}), STATE)

    def test_writer_binding_is_exact(self) -> None:
        other = "4b00c1d3796a92c4cc50d53b4551a68addda6f0b"
        cases = {
            "writer base_sha does not match": {"builder__base_sha": other},
            "boot-time base": {"builder__final_sha": other},
            "no branch or worktree": {"builder__branch": None},
            "no branch or worktree ": {"builder__worktree": None},
            "only through one writer": {"builder__authority": "advisory"},
            "only through one writer ": {"checker__authority": "write"},
            "feature does not match": {"feature": "other"},
            "mission_id does not match": {"mission_id": "other"},
            "target repository does not match": {"target_repo": "/other"},
        }
        for message, changes in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(
                    fleet_archive.ArchiveError, message.strip()):
                fleet_archive._archive_manifest_binding_v2(manifest_v2(**changes), STATE)

    def test_dispatch_keeps_the_current_contract_strict(self) -> None:
        v3_without_base = manifest_v2(manifest_contract_version="3")
        self.assertFalse(fleet_archive._is_manifest_contract_v2(v3_without_base))
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "fleet manifest base_sha"):
            fleet_archive._archive_manifest_binding(v3_without_base, STATE)
        self.assertFalse(fleet_archive._is_manifest_contract_v2(manifest_v2(base_sha=BASE)))


class ProviderShapeAgreementTests(unittest.TestCase):
    @staticmethod
    def compiled(router: dict, *, version: int = 2) -> bytes:
        return fleet_json.canonical_bytes({"schema_version": version, "router_snapshot": router})

    def test_archived_router_and_manifest_must_agree(self) -> None:
        pre = {"schema_version": 3}
        self.assertFalse(fleet_archive._router_provider_contract(self.compiled(pre), {}))
        self.assertTrue(fleet_archive._router_provider_contract(
            self.compiled({**pre, "providers": {}}), {}))
        self.assertTrue(fleet_archive._router_provider_contract(self.compiled(pre, version=1), {}))
        with self.assertRaisesRegex(fleet_archive.ArchiveError, "mix provider contracts"):
            fleet_archive._router_provider_contract(
                self.compiled(pre), {"provider.claude.submit.repress_safe": "true"})


class AcceptanceRefusesHistoricalLanesTests(unittest.TestCase):
    def test_historical_lane_never_grants_acceptance(self) -> None:
        @contextmanager
        def view(_path):
            yield mock.Mock(snapshot=mock.Mock(return_value={}))

        verified = {"valid": True, "historical_lanes": [fleet_archive.MANIFEST_V2_LANE]}
        with mock.patch.object(fleet_archive, "_ArchiveView", view), \
                mock.patch.object(fleet_archive, "_verify_archive_snapshot", return_value=verified):
            with self.assertRaisesRegex(fleet_archive.ArchiveError, "historical lane"):
                fleet_archive.verify_acceptance(Path("/unused"), {"schema_version": 1})


if __name__ == "__main__":
    unittest.main()
