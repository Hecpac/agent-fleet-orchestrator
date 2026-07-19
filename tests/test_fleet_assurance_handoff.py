from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HANDOFF_CLI = ROOT / "scripts" / "fleet_assurance_handoff.py"
FLEET_UP = ROOT / "scripts" / "fleet-up.sh"
FLEET_DOWN = ROOT / "scripts" / "fleet-down.sh"
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_admission  # noqa: E402
import fleet_artifacts  # noqa: E402
import fleet_assurance_handoff as handoff  # noqa: E402
import fleet_control_service  # noqa: E402
import fleet_json  # noqa: E402
import fleet_leases  # noqa: E402
import fleet_mission  # noqa: E402
import fleet_mission_state as mission_state  # noqa: E402
import fleet_state  # noqa: E402
import workflow_config  # noqa: E402
from tests.mission_control_test_support import create_running_mission  # noqa: E402
from tests import test_fleet_up as fleet_up_tests  # noqa: E402


OPTIONAL_RUNTIME_SUFFIXES = (
    "dialogue.jsonl",
    "dialogue-control.jsonl",
    "assurance-control.jsonl",
    "verification-receipt.json",
    "assurance-receipt.json",
)


@dataclass(frozen=True)
class RuntimeFixture:
    runs: Path
    mission_id: str
    feature: str
    manifest: Path
    state: Path
    ledger: Path
    mission_ledger: Path

    @property
    def mission_root(self) -> Path:
        return self.runs / "missions" / self.mission_id

    @property
    def handoff_root(self) -> Path:
        return self.mission_root / "assurance-handoff"


def _tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    if not root.exists() and not root.is_symlink():
        return snapshot
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            snapshot[relative] = ("symlink", mode, info.st_nlink, os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            snapshot[relative] = ("file", mode, info.st_nlink, path.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            snapshot[relative] = ("directory", mode, info.st_nlink)
        else:
            snapshot[relative] = ("special", mode, info.st_nlink, info.st_rdev)
    return snapshot


def _assert_canonical_json(test: unittest.TestCase, path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    value = fleet_json.loads(raw)
    test.assertIs(type(value), dict)
    test.assertEqual(raw, fleet_json.canonical_bytes(value) + b"\n")
    return value


def _write_phase_state(fixture: RuntimeFixture) -> None:
    manifest = fleet_state.manifest_values(fixture.manifest)
    fixture.state.write_bytes(
        fleet_state._state_bytes(fleet_state._new_state(manifest))
    )
    fixture.state.chmod(0o600)


def _set_workspace_handoff(
    fixture: RuntimeFixture, *, state: str, quiesced: bool
) -> None:
    rows = fixture.manifest.read_text(encoding="utf-8").splitlines()
    replacements = {
        "workspace.handoff_state": state,
        "workspace.quiesced": "1" if quiesced else "0",
    }
    present: set[str] = set()
    updated: list[str] = []
    for row in rows:
        key = row.split("=", 1)[0]
        if key in replacements:
            updated.append(f"{key}={replacements[key]}")
            present.add(key)
        else:
            updated.append(row)
    updated.extend(
        f"{key}={value}" for key, value in replacements.items() if key not in present
    )
    fixture.manifest.write_text("\n".join(updated) + "\n", encoding="utf-8")
    fixture.manifest.chmod(0o600)


def _prepare_intent(
    test: unittest.TestCase, fixture: RuntimeFixture
) -> tuple[dict[str, object], tuple[int, int]]:
    process = subprocess.Popen(
        [
            sys.executable,
            str(HANDOFF_CLI),
            "hold",
            "--runs-dir",
            str(fixture.runs),
            "--feature",
            fixture.feature,
            "--mission-id",
            fixture.mission_id,
        ],
        cwd=ROOT,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    test.assertEqual(process.stdout.readline().strip(), "READY")
    stdout, stderr = process.communicate("ABORT\n", timeout=20)
    test.assertEqual(process.returncode, 75, stderr + stdout)
    intent_path = fixture.handoff_root / "intent.json"
    intent = _assert_canonical_json(test, intent_path)
    test.assertEqual(intent["status"], "prepared")
    info = intent_path.stat()
    return intent, (info.st_dev, info.st_ino)


def _approve_mission(
    runs: Path,
    mission_id: str,
    *,
    expires_in: timedelta,
) -> None:
    compiled, current = fleet_mission.load_mission_compiled(
        runs, mission_id, mode="effect"
    )
    if current["risk"] != "high":
        mission_state.append_event(
            runs,
            mission_id,
            kind="risk_escalated",
            actor="CONTROL",
            idempotency_key="handoff:risk",
            payload={
                "from": current["risk"],
                "to": "high",
                "categories": ["handoff"],
                "reason": "exercise assurance handoff",
            },
        )
    current = fleet_mission.load_state(runs, mission_id)
    request, _ = mission_state.append_event(
        runs,
        mission_id,
        kind="assurance_requested",
        actor="CONTROL",
        idempotency_key="handoff:request",
        payload={
            "risk": "high",
            "categories": ["handoff"],
            "scope": current["target_repo"],
            "workflow_digest": compiled["workflow_digest"],
        },
    )
    mission_state.append_event(
        runs,
        mission_id,
        kind="assurance_approved",
        actor="HUMAN",
        idempotency_key="handoff:approval",
        payload={
            "approval_id": str(
                uuid.uuid5(uuid.UUID(mission_id), "handoff-test-approval")
            ),
            "request_event_sha256": request["event_sha256"],
            "workflow_digest": compiled["workflow_digest"],
            "scope": current["target_repo"],
            "risk": "high",
            "expires_at": (datetime.now(timezone.utc) + expires_in).isoformat(),
            "expires_in_seconds": max(60, int(expires_in.total_seconds()) + 1),
            "approved_by_sha256": hashlib.sha256(b"test-operator").hexdigest(),
            "decision": "approved",
        },
    )


def _renew_mission_approval(fixture: RuntimeFixture) -> dict[str, object]:
    current = fleet_mission.load_state(fixture.runs, fixture.mission_id)
    prior = current["approval"]
    key = "handoff:approval-renewal"
    event, _ = mission_state.append_event(
        fixture.runs,
        fixture.mission_id,
        kind="assurance_approval_renewed",
        actor="HUMAN",
        idempotency_key=key,
        payload={
            "approval_id": str(
                uuid.uuid5(
                    uuid.UUID(fixture.mission_id),
                    f"approval-renewal:{prior['event_sha256']}:{key}",
                )
            ),
            "prior_approval_event_sha256": prior["event_sha256"],
            "request_event_sha256": prior["request_event_sha256"],
            "workflow_digest": prior["workflow_digest"],
            "scope": prior["scope"],
            "risk": prior["risk"],
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
            "expires_in_seconds": 600,
            "approved_by_sha256": hashlib.sha256(b"test-renewal-operator").hexdigest(),
            "decision": "approved",
        },
    )
    return event


def _finalize_lead(runs: Path, mission_id: str, lead_run_id: str) -> None:
    compiled, current = fleet_mission.load_mission_compiled(
        runs, mission_id, mode="effect"
    )
    lead = compiled["resolved"]["lead"]
    artifact = fleet_artifacts.put_bytes(
        runs, mission_id, b"main Lead produced a handoff-ready result\n"
    )
    mission_state.append_event(
        runs,
        mission_id,
        kind="lead_result_recorded",
        actor="CONTROL",
        idempotency_key="handoff:lead-result",
        payload={
            "run_id": lead_run_id,
            "artifact_id": artifact["artifact_id"],
            "result_file": artifact["path"],
            "provider": lead["provider"],
            "model": lead["model"],
            "variant": lead.get("variant"),
        },
    )
    admission_id = current["lead_admission_id"]
    admission = current["admissions"][admission_id]
    fleet_admission.finalize(
        runs,
        mission_id,
        admission_id=admission_id,
        recipient_instance=admission["recipient_instance"],
        writer=bool(admission["writer"]),
        terminal_evidence={
            "schema_version": 1,
            "source_event_sha256": mission_state.sha256(
                {"run_id": lead_run_id, "status": "succeeded"}
            ),
            "run_id": lead_run_id,
            "task_sha256": admission["task_sha256"],
            "status": "succeeded",
        },
        reason="main Lead finalized before assurance handoff",
        idempotency_key="handoff:lead-finalized",
    )


def _make_runtime_fixture(
    root: Path,
    *,
    feature: str,
    approved: bool = True,
    approval_lifetime: timedelta = timedelta(minutes=10),
    finalize_lead: bool = True,
) -> RuntimeFixture:
    runs, mission_id, lead_run_id = create_running_mission(root, feature=feature)
    manifest = runs / f"fleet-{feature}.manifest"
    fixture = RuntimeFixture(
        runs=runs,
        mission_id=mission_id,
        feature=feature,
        manifest=manifest,
        state=runs / f"fleet-{feature}.state.json",
        ledger=runs / f"fleet-{feature}.ledger.jsonl",
        mission_ledger=runs / "missions" / mission_id / "mission.jsonl",
    )
    _set_workspace_handoff(fixture, state="live", quiesced=False)
    if finalize_lead:
        _finalize_lead(runs, mission_id, lead_run_id)
    if approved:
        _approve_mission(runs, mission_id, expires_in=approval_lifetime)
    _write_phase_state(fixture)
    fixture.ledger.write_bytes(b"")
    fixture.ledger.chmod(0o600)
    return fixture


def _write_optional_runtime_files(fixture: RuntimeFixture) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for suffix in OPTIONAL_RUNTIME_SUFFIXES:
        path = fixture.runs / f"fleet-{fixture.feature}.{suffix}"
        if suffix.endswith(".jsonl"):
            content = fleet_json.canonical_jsonl(
                [{"schema_version": 1, "source": suffix}]
            )
        else:
            content = (
                fleet_json.canonical_bytes({"schema_version": 1, "source": suffix})
                + b"\n"
            )
        path.write_bytes(content)
        path.chmod(0o600)
        contents[path.name] = content
    return contents


class FleetAssuranceHandoffContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)

    def fixture(self, name: str, **kwargs: object) -> RuntimeFixture:
        root = self.tmp / name
        root.mkdir()
        return _make_runtime_fixture(root, feature=name, **kwargs)

    def test_preflight_accepts_only_exact_approved_quiescent_main_contract(
        self,
    ) -> None:
        fixture = self.fixture("preflight-ready")
        before = _tree_snapshot(fixture.runs)

        result = handoff.preflight(
            fixture.runs, fixture.feature, mission_id=fixture.mission_id
        )

        self.assertEqual(result["mission_id"], fixture.mission_id)
        self.assertEqual(result["feature"], fixture.feature)
        self.assertIn("mode", result)
        self.assertNotIn("result", result)
        self.assertEqual(_tree_snapshot(fixture.runs), before)
        current = fleet_mission.load_state(fixture.runs, fixture.mission_id)
        self.assertEqual(current["status"], "assurance_approved")
        self.assertIsNotNone(current["lead_result"])
        self.assertTrue(current["admissions"])
        self.assertTrue(
            all(not item["active"] for item in current["admissions"].values())
        )
        self.assertEqual(current["active_recipients"], {})
        self.assertIsNone(current["active_writer"])

    def test_preflight_rejections_have_zero_durable_or_external_effects(self) -> None:
        scenarios = (
            (
                "not-approved",
                {"approved": False},
                None,
                "assurance_approved|approval",
            ),
            (
                "expired",
                {"approval_lifetime": timedelta(milliseconds=500)},
                None,
                "expired|approval",
            ),
            ("missing-ledger", {}, "missing-ledger", "ledger|runtime"),
            ("mission-drift", {}, "mission-drift", "[Mm]ission"),
            ("preset-drift", {}, "preset-drift", "preset|compiled|main"),
            ("mode-drift", {}, "mode-drift", "autonomous|mode|phase"),
        )
        for label, fixture_options, mutation, expected in scenarios:
            with self.subTest(case=label):
                fixture = self.fixture(label, **fixture_options)
                if label == "expired":
                    time.sleep(0.6)
                if mutation == "missing-ledger":
                    fixture.ledger.unlink()
                elif mutation == "mission-drift":
                    replacement = str(uuid.uuid4())
                    fixture.manifest.write_text(
                        fixture.manifest.read_text(encoding="utf-8").replace(
                            f"mission_id={fixture.mission_id}\n",
                            f"mission_id={replacement}\n",
                        ),
                        encoding="utf-8",
                    )
                    fixture.manifest.chmod(0o600)
                    _write_phase_state(fixture)
                elif mutation == "preset-drift":
                    fixture.manifest.write_text(
                        fixture.manifest.read_text(encoding="utf-8").replace(
                            "preset=dan\n", "preset=fleet_dialogue\n"
                        ),
                        encoding="utf-8",
                    )
                    fixture.manifest.chmod(0o600)
                    _write_phase_state(fixture)
                elif mutation == "mode-drift":
                    fixture.manifest.write_text(
                        fixture.manifest.read_text(encoding="utf-8").replace(
                            "mode=autonomous\n", "mode=guided\n"
                        ),
                        encoding="utf-8",
                    )
                    fixture.manifest.chmod(0o600)
                    fixture.state.write_bytes(b"{}\n")
                    fixture.state.chmod(0o600)
                before = _tree_snapshot(fixture.runs)

                with self.assertRaisesRegex(handoff.HandoffError, expected):
                    handoff.preflight(
                        fixture.runs,
                        fixture.feature,
                        mission_id=fixture.mission_id,
                    )

                self.assertEqual(_tree_snapshot(fixture.runs), before)
                self.assertFalse(fixture.handoff_root.exists())

    def test_shell_preflight_failure_never_admits_lifecycle_effects(self) -> None:
        fixture = self.fixture("shell-preflight-reject", approved=False)
        fake_bin = self.tmp / "preflight-fake-bin"
        fake_bin.mkdir()
        cmux_marker = self.tmp / "cmux-was-called"
        fake_cmux = fake_bin / "cmux"
        fake_cmux.write_text(
            '#!/bin/sh\nprintf called > "$HANDOFF_CMUX_MARKER"\nexit 99\n',
            encoding="utf-8",
        )
        fake_cmux.chmod(0o700)
        before = _tree_snapshot(fixture.runs)
        env = {
            **os.environ,
            "FLEET_RUNS_DIR": str(fixture.runs),
            "FLEET_MISSION_ID": fixture.mission_id,
            "HANDOFF_CMUX_MARKER": str(cmux_marker),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }

        rejected = subprocess.run(
            ["bash", str(FLEET_DOWN), fixture.feature, "--handoff-assurance"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(rejected.returncode, 2, rejected.stderr)
        self.assertEqual(_tree_snapshot(fixture.runs), before)
        self.assertFalse(cmux_marker.exists())
        self.assertFalse(fixture.handoff_root.exists())

    def test_preflight_defense_rejects_active_admission_projection(self) -> None:
        fixture = self.fixture("active-projection")
        base = fleet_mission.load_state(fixture.runs, fixture.mission_id)
        lead_admission_id = base["lead_admission_id"]
        cases: dict[str, dict[str, object]] = {}

        lead_active = copy.deepcopy(base)
        lead_active["admissions"][lead_admission_id]["active"] = True
        lead_active["admissions"][lead_admission_id]["phase"] = "started"
        cases["active-lead"] = lead_active

        active_writer = copy.deepcopy(base)
        active_writer["active_writer"] = lead_admission_id
        cases["active-writer"] = active_writer

        active_recipient = copy.deepcopy(base)
        active_recipient["active_recipients"] = {"lead": lead_admission_id}
        cases["active-recipient"] = active_recipient

        active_claim = copy.deepcopy(base)
        active_claim["run_claims"] = {
            base["lead_run_id"]: {
                "admission_id": lead_admission_id,
                "recipient_instance": "lead",
            }
        }
        cases["active-run-claim"] = active_claim

        for label, projected in cases.items():
            with self.subTest(case=label):
                before = _tree_snapshot(fixture.runs)
                with mock.patch.object(
                    handoff, "_load_mission_readonly", return_value=projected
                ):
                    with self.assertRaisesRegex(
                        handoff.HandoffError,
                        "admission|writer|recipient|claim|inactive|Lead",
                    ):
                        handoff.preflight(
                            fixture.runs,
                            fixture.feature,
                            mission_id=fixture.mission_id,
                        )
                self.assertEqual(_tree_snapshot(fixture.runs), before)
                self.assertFalse(fixture.handoff_root.exists())

    def test_preflight_rejects_ambiguous_state_json_without_any_effect(self) -> None:
        fixture = self.fixture("ambiguous-state")
        valid = fixture.state.read_bytes()
        cases = {
            "duplicate": b'{"schema_version":2,"schema_version":2}\n',
            "nan": b'{"value":NaN}\n',
            "infinity": b'{"value":Infinity}\n',
            "overflow": b'{"value":1e400}\n',
            "bom": b"\xef\xbb\xbf" + valid,
            "invalid-utf8": b'{"value":"\xff"}\n',
            "surrogate": b'{"value":"\\ud800"}\n',
            "trailing": valid.rstrip(b"\n") + b" trailing\n",
            "crlf": valid.replace(b"\n", b"\r\n"),
            "no-final-lf": valid.rstrip(b"\n"),
            "noncanonical": json.dumps(json.loads(valid), sort_keys=True).encode(
                "utf-8"
            )
            + b"\n",
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                fixture.state.write_bytes(content)
                before = _tree_snapshot(fixture.runs)
                with self.assertRaises(handoff.HandoffError):
                    handoff.preflight(
                        fixture.runs,
                        fixture.feature,
                        mission_id=fixture.mission_id,
                    )
                self.assertEqual(_tree_snapshot(fixture.runs), before)
        fixture.state.write_bytes(valid)

    def test_source_symlink_or_hardlink_is_rejected_before_journal_or_move(
        self,
    ) -> None:
        for attack in ("symlink", "hardlink"):
            for suffix in ("manifest", "state.json", "ledger.jsonl"):
                with self.subTest(attack=attack, suffix=suffix):
                    feature = f"path-{attack}-{suffix.split('.')[0]}"
                    fixture = self.fixture(feature)
                    source = fixture.runs / f"fleet-{feature}.{suffix}"
                    outside = fixture.runs / f".{source.name}.outside"
                    if attack == "symlink":
                        source.rename(outside)
                        source.symlink_to(outside.name)
                    else:
                        os.link(source, outside)
                    before = _tree_snapshot(fixture.runs)

                    with self.assertRaises(handoff.HandoffError):
                        handoff.preflight(
                            fixture.runs,
                            feature,
                            mission_id=fixture.mission_id,
                        )

                    self.assertEqual(_tree_snapshot(fixture.runs), before)
                    self.assertFalse(fixture.handoff_root.exists())

    def test_destination_and_journal_attacks_fail_before_source_mutation(self) -> None:
        attacks = (
            "handoff-symlink",
            "runtime-symlink",
            "intent-hardlink",
            "receipt-symlink",
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                fixture = self.fixture(attack)
                outside = fixture.mission_root / f".{attack}-outside"
                if attack == "handoff-symlink":
                    outside.mkdir(mode=0o700)
                    fixture.handoff_root.symlink_to(
                        outside.name, target_is_directory=True
                    )
                else:
                    fixture.handoff_root.mkdir(mode=0o700)
                    if attack == "runtime-symlink":
                        outside.mkdir(mode=0o700)
                        (fixture.handoff_root / "main-runtime").symlink_to(
                            f"../{outside.name}", target_is_directory=True
                        )
                    elif attack == "intent-hardlink":
                        outside.write_bytes(b"{}\n")
                        outside.chmod(0o600)
                        os.link(outside, fixture.handoff_root / "intent.json")
                    else:
                        outside.write_bytes(b"{}\n")
                        outside.chmod(0o600)
                        (fixture.handoff_root / "receipt.json").symlink_to(
                            f"../{outside.name}"
                        )
                before = _tree_snapshot(fixture.runs)

                with self.assertRaises(handoff.HandoffError):
                    handoff.preflight(
                        fixture.runs,
                        fixture.feature,
                        mission_id=fixture.mission_id,
                    )

                self.assertEqual(_tree_snapshot(fixture.runs), before)
                self.assertTrue(fixture.manifest.exists())
                self.assertTrue(fixture.state.exists())
                self.assertTrue(fixture.ledger.exists())

    def test_commit_moves_exact_top_level_names_and_is_idempotent(self) -> None:
        fixture = self.fixture("commit-ready")
        optional = _write_optional_runtime_files(fixture)
        _prepare_intent(self, fixture)
        _set_workspace_handoff(fixture, state="quiesced", quiesced=True)
        sources = {
            fixture.manifest.name: fixture.manifest.read_bytes(),
            fixture.state.name: fixture.state.read_bytes(),
            fixture.ledger.name: fixture.ledger.read_bytes(),
            **optional,
        }
        mission_ledger_before = fixture.mission_ledger.read_bytes()

        result = handoff.commit(fixture.runs, fixture.feature, fixture.mission_id)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["mission_id"], fixture.mission_id)
        self.assertEqual(result["feature"], fixture.feature)
        runtime = fixture.handoff_root / "main-runtime"
        self.assertEqual(
            sorted(path.name for path in runtime.iterdir()), sorted(sources)
        )
        for name, content in sources.items():
            self.assertFalse((fixture.runs / name).exists())
            self.assertEqual((runtime / name).read_bytes(), content)
            info = (runtime / name).stat()
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(info.st_nlink, 1)
        intent = fixture.handoff_root / "intent.json"
        receipt = fixture.handoff_root / "receipt.json"
        intent_value = _assert_canonical_json(self, intent)
        receipt_value = _assert_canonical_json(self, receipt)
        for value in (intent_value, receipt_value):
            self.assertEqual(value["mission_id"], fixture.mission_id)
            self.assertEqual(value["feature"], fixture.feature)
        self.assertEqual(intent_value["status"], "committing")
        self.assertEqual(receipt_value["status"], "complete")
        self.assertEqual(fixture.mission_ledger.read_bytes(), mission_ledger_before)
        self.assertEqual(
            fleet_mission.load_state(fixture.runs, fixture.mission_id)["status"],
            "assurance_approved",
        )
        self.assertFalse((fixture.mission_root / "archive").exists())
        self.assertFalse((fixture.runs / "archive").exists())
        receipt_before = receipt.read_bytes()
        receipt_identity = (receipt.stat().st_dev, receipt.stat().st_ino)

        replay = handoff.commit(fixture.runs, fixture.feature, fixture.mission_id)
        complete = handoff.preflight(
            fixture.runs, fixture.feature, mission_id=fixture.mission_id
        )

        self.assertEqual(replay, result)
        self.assertEqual(complete["mode"], "complete")
        self.assertEqual(complete["result"], result)
        self.assertEqual(receipt.read_bytes(), receipt_before)
        self.assertEqual(
            (receipt.stat().st_dev, receipt.stat().st_ino), receipt_identity
        )
        self.assertEqual(fixture.mission_ledger.read_bytes(), mission_ledger_before)

    def test_hold_abort_keeps_one_prepared_intent_and_moves_nothing(self) -> None:
        fixture = self.fixture("hold-abort")
        sources_before = {
            path.name: path.read_bytes()
            for path in (fixture.manifest, fixture.state, fixture.ledger)
        }

        first, identity = _prepare_intent(self, fixture)
        first_bytes = (fixture.handoff_root / "intent.json").read_bytes()
        second, replay_identity = _prepare_intent(self, fixture)

        self.assertEqual(second, first)
        self.assertEqual(replay_identity, identity)
        self.assertEqual(
            (fixture.handoff_root / "intent.json").read_bytes(), first_bytes
        )
        self.assertFalse((fixture.handoff_root / "main-runtime").exists())
        self.assertFalse((fixture.handoff_root / "receipt.json").exists())
        for name, content in sources_before.items():
            self.assertEqual((fixture.runs / name).read_bytes(), content)

    def test_completed_receipt_survives_only_approval_renewal_lineage(self) -> None:
        fixture = self.fixture(
            "receipt-renewal", approval_lifetime=timedelta(seconds=5)
        )
        _prepare_intent(self, fixture)
        _set_workspace_handoff(fixture, state="quiesced", quiesced=True)
        result = handoff.commit(fixture.runs, fixture.feature, fixture.mission_id)
        receipt = fixture.handoff_root / "receipt.json"
        receipt_before = receipt.read_bytes()
        receipt_identity = (receipt.stat().st_dev, receipt.stat().st_ino)

        approval = fleet_mission.load_state(fixture.runs, fixture.mission_id)[
            "approval"
        ]
        expires_at = datetime.fromisoformat(
            str(approval["expires_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        delay = (expires_at - datetime.now(timezone.utc)).total_seconds() + 0.05
        if delay > 0:
            time.sleep(delay)
        renewed = _renew_mission_approval(fixture)

        complete = handoff.preflight(
            fixture.runs, fixture.feature, mission_id=fixture.mission_id
        )
        replay = handoff.commit(fixture.runs, fixture.feature, fixture.mission_id)

        self.assertEqual(complete["mode"], "complete")
        self.assertEqual(complete["result"], result)
        self.assertEqual(replay, result)
        self.assertEqual(receipt.read_bytes(), receipt_before)
        self.assertEqual(
            (receipt.stat().st_dev, receipt.stat().st_ino), receipt_identity
        )

        mission_state.append_event(
            fixture.runs,
            fixture.mission_id,
            kind="assurance_boot_started",
            actor="CONTROL",
            idempotency_key="handoff:post-receipt-non-renewal",
            payload={
                "preset": "assurance",
                "approval_event_sha256": renewed["event_sha256"],
            },
        )
        for operation in (
            lambda: handoff.preflight(
                fixture.runs,
                fixture.feature,
                mission_id=fixture.mission_id,
            ),
            lambda: handoff.commit(fixture.runs, fixture.feature, fixture.mission_id),
        ):
            with self.assertRaises(handoff.HandoffError):
                operation()
        self.assertEqual(receipt.read_bytes(), receipt_before)
        self.assertEqual(
            (receipt.stat().st_dev, receipt.stat().st_ino), receipt_identity
        )

    def test_sigkill_checkpoints_recover_to_one_identical_receipt(self) -> None:
        checkpoints = (
            ("after_committing_intent", None),
            ("after_move", "fleet-crash-after-move.state.json"),
            ("after_receipt", None),
        )
        for checkpoint, selected_leaf in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                name = checkpoint.replace("after_", "crash-").replace("_", "-")
                if checkpoint == "after_move":
                    name = "crash-after-move"
                    selected_leaf = f"fleet-{name}.state.json"
                fixture = self.fixture(name)
                _write_optional_runtime_files(fixture)
                mission_ledger_before = fixture.mission_ledger.read_bytes()
                env = os.environ.copy()
                env["FLEET_TEST_HANDOFF_CRASH_AT"] = checkpoint
                if selected_leaf is not None:
                    env["FLEET_TEST_HANDOFF_CRASH_FILE"] = selected_leaf
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(HANDOFF_CLI),
                        "hold",
                        "--runs-dir",
                        str(fixture.runs),
                        "--feature",
                        fixture.feature,
                        "--mission-id",
                        fixture.mission_id,
                    ],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "READY")
                _set_workspace_handoff(fixture, state="quiesced", quiesced=True)
                stdout, stderr = process.communicate("COMMIT\n", timeout=20)
                self.assertEqual(process.returncode, -signal.SIGKILL, stderr + stdout)
                self.assertEqual(
                    fixture.mission_ledger.read_bytes(), mission_ledger_before
                )

                recovered = handoff.commit(
                    fixture.runs, fixture.feature, fixture.mission_id
                )
                replay = handoff.commit(
                    fixture.runs, fixture.feature, fixture.mission_id
                )

                self.assertEqual(recovered, replay)
                self.assertEqual(recovered["status"], "ready")
                _assert_canonical_json(self, fixture.handoff_root / "intent.json")
                _assert_canonical_json(self, fixture.handoff_root / "receipt.json")
                self.assertEqual(
                    fixture.mission_ledger.read_bytes(), mission_ledger_before
                )


class FleetAssuranceHandoffShellTests(unittest.TestCase):
    make_executable = fleet_up_tests.FleetUpTests.make_executable
    calls = fleet_up_tests.FleetUpTests.calls
    make_target_repo = fleet_up_tests.FleetUpTests.make_target_repo
    writer_worktree = fleet_up_tests.FleetUpTests.writer_worktree

    def setUp(self) -> None:
        fleet_up_tests.FleetUpTests.setUp(self)

    def test_fleet_down_handoff_stops_main_only_and_never_archives_mission(
        self,
    ) -> None:
        feature = "shell-handoff"
        target = self.make_target_repo(f"target-{feature}")
        base_sha = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        compiled = workflow_config.compile_path(
            ROOT / "workflows" / "implementation.yaml"
        )
        mission_id, _ = fleet_mission.create_mission(
            self.runs,
            compiled=compiled,
            feature=feature,
            objective="handoff main runtime to an assured generation",
            target_repo=target.resolve(),
            base_sha=base_sha,
            idempotency_key=f"test:handoff:{feature}",
        )
        self.env["FLEET_MISSION_ID"] = mission_id
        socket_root = Path(tempfile.mkdtemp(prefix="fah-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, socket_root, True)
        self.env["FLEET_CONTROL_SOCKET_DIR"] = str(socket_root)
        with mock.patch.dict(os.environ, self.env, clear=False):
            lifecycle = fleet_control_service.ControlLifecycle(
                self.runs,
                mission_id,
                preset=compiled["resolved"]["preset"],
            )
            lifecycle.start()
        self.addCleanup(
            lambda: (
                lifecycle.stop_if_present()
                if lifecycle.lifecycle_path.exists()
                else None
            )
        )
        compiled_path = self.runs / "missions" / mission_id / "compiled-workflow.json"
        booted = subprocess.run(
            [
                "bash",
                str(FLEET_UP),
                feature,
                "--preset",
                compiled["resolved"]["preset"],
                "--target-repo",
                str(target),
                "--expected-base-sha",
                base_sha,
                "--compiled-workflow",
                str(compiled_path),
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
        self.assertEqual(booted.returncode, 0, booted.stderr)
        manifest = self.runs / f"fleet-{feature}.manifest"
        mission_state.append_event(
            self.runs,
            mission_id,
            kind="fleet_boot_started",
            actor="CONTROL",
            idempotency_key="handoff-shell:boot",
            payload={"feature": feature, "preset": compiled["resolved"]["preset"]},
        )
        mission_state.append_event(
            self.runs,
            mission_id,
            kind="mission_running",
            actor="CONTROL",
            idempotency_key="handoff-shell:running",
            payload={"manifest": str(manifest)},
        )

        reservation = fleet_admission.reserve_many(
            self.runs,
            mission_id,
            requests=[
                {
                    "request_key": "handoff-shell-lead",
                    "run_kind": "lead",
                    "recipient_instance": "lead",
                    "capability": "lead",
                    "delegated_budget": 0,
                    "writer": False,
                    "task_sha256": mission_state.sha256(
                        {
                            "kind": "handoff-shell-lead-task",
                            "feature": feature,
                        }
                    ),
                    "effect_sha256": mission_state.sha256(
                        {"kind": "handoff-shell-lead"}
                    ),
                }
            ],
            idempotency_key="handoff-shell:reserve",
        )["admissions"][0]
        committed = fleet_admission.commit(
            self.runs,
            mission_id,
            admission_id=reservation["admission_id"],
            request_digest=reservation["request_digest"],
            effect_sha256=reservation["effect_sha256"],
            recipient_instance=reservation["recipient_instance"],
            writer=reservation["writer"],
            run_id=reservation["run_id"],
            idempotency_key="handoff-shell:commit",
        )
        authorized = fleet_admission.authorize_launch(
            self.runs,
            mission_id,
            admission_id=reservation["admission_id"],
            commit_event_sha256=committed["commit_event_sha256"],
            request_digest=reservation["request_digest"],
            effect_sha256=reservation["effect_sha256"],
            recipient_instance=reservation["recipient_instance"],
            writer=reservation["writer"],
            run_id=reservation["run_id"],
            idempotency_key="handoff-shell:authorize",
        )
        fleet_admission.mark_started(
            self.runs,
            mission_id,
            admission_id=reservation["admission_id"],
            authorization_event_sha256=authorized["authorization_event_sha256"],
            request_digest=reservation["request_digest"],
            effect_sha256=reservation["effect_sha256"],
            recipient_instance=reservation["recipient_instance"],
            writer=reservation["writer"],
            run_id=reservation["run_id"],
            idempotency_key="handoff-shell:start",
        )
        mission_state.append_event(
            self.runs,
            mission_id,
            kind="lead_dispatched",
            actor="CONTROL",
            idempotency_key="handoff-shell:dispatched",
            payload={"run_id": reservation["run_id"], "prompt_sha256": "b" * 64},
        )
        _finalize_lead(self.runs, mission_id, reservation["run_id"])
        _approve_mission(self.runs, mission_id, expires_in=timedelta(minutes=10))
        mission_ledger_before = mission_state.ledger_path(
            self.runs, mission_id
        ).read_bytes()
        assured_sentinel = (
            self.runs / "missions" / mission_id / "control-assured" / "untouched.json"
        )
        assured_sentinel.parent.mkdir(mode=0o700)
        assured_sentinel.write_bytes(b'{"sentinel":true}\n')
        assured_sentinel.chmod(0o600)
        close_before = sum(call[:1] == ["close-workspace"] for call in self.calls())

        closed = subprocess.run(
            ["bash", str(FLEET_DOWN), feature, "--handoff-assurance"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=40,
            check=False,
        )

        self.assertEqual(closed.returncode, 0, closed.stderr)
        output = fleet_json.loads(closed.stdout)
        self.assertEqual(set(output), {"status", "mission_id", "feature", "receipt"})
        self.assertEqual(output["status"], "ready")
        self.assertEqual(output["mission_id"], mission_id)
        self.assertEqual(output["feature"], feature)
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()),
            close_before + 1,
        )
        self.assertEqual(
            [call for call in self.calls() if call[:1] == ["close-workspace"]][-1],
            ["close-workspace", "--workspace", "workspace:1"],
        )
        lifecycle_value = json.loads(
            lifecycle.lifecycle_path.read_text(encoding="utf-8")
        )
        self.assertIsNotNone(lifecycle_value["stopped_at"])
        self.assertEqual(assured_sentinel.read_bytes(), b'{"sentinel":true}\n')
        self.assertEqual(
            mission_state.ledger_path(self.runs, mission_id).read_bytes(),
            mission_ledger_before,
        )
        self.assertEqual(
            fleet_mission.load_state(self.runs, mission_id)["status"],
            "assurance_approved",
        )
        handoff_root = self.runs / "missions" / mission_id / "assurance-handoff"
        runtime = handoff_root / "main-runtime"
        for suffix in ("manifest", "state.json", "ledger.jsonl"):
            leaf = f"fleet-{feature}.{suffix}"
            self.assertTrue((runtime / leaf).is_file(), leaf)
            self.assertFalse((self.runs / leaf).exists(), leaf)
        receipt = handoff_root / "receipt.json"
        self.assertEqual(
            Path(output["receipt"]),
            Path("missions") / mission_id / "assurance-handoff" / "receipt.json",
        )
        receipt_before = receipt.read_bytes()
        self.assertFalse((self.runs / "archive").exists())
        self.assertFalse((self.runs / "missions" / mission_id / "archive").exists())

        # Simulate the shell dying after the receipt commit but before releasing
        # its close marker. An exact replay must quarantine that stale owner and
        # leave no marker that could block the assured generation.
        receipt_value = fleet_json.loads(receipt_before)
        fleet_leases.begin_close(
            self.runs,
            feature=feature,
            close_id="crashed-shell-close",
            workspace_uuid=receipt_value["workspace_uuid"],
            tree_reader=lambda: "",
        )
        self.assertTrue(fleet_leases.closing_path(self.runs, feature).is_file())

        replay = subprocess.run(
            ["bash", str(FLEET_DOWN), feature, "--handoff-assurance"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(fleet_json.loads(replay.stdout), output)
        self.assertEqual(receipt.read_bytes(), receipt_before)
        self.assertFalse(fleet_leases.closing_path(self.runs, feature).exists())
        self.assertEqual(
            len(
                list(
                    (self.runs / "archive" / "closing" / feature).glob("*-closing.json")
                )
            ),
            1,
        )
        self.assertEqual(
            sum(call[:1] == ["close-workspace"] for call in self.calls()),
            close_before + 1,
        )


if __name__ == "__main__":
    unittest.main()
