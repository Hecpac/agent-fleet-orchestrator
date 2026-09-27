"""Herdr owner transport contract, executable only with trusted test injection.

Real Herdr lacks conditional send/stop tied atomically to a native frontier.
No subprocess implementation or permission grant is exposed by this module.
"""
from __future__ import annotations

import copy

import fleet_herdr_effects as effects
import fleet_herdr_owner_contract as contracts
import fleet_herdr_owner_cycle as cycle
import fleet_herdr_owner_native as native
import fleet_herdr_owner_runtime as runtime
import fleet_herdr_permissions as permissions
import fleet_herdr_work_packet as work
import fleet_json


def launch(contract):
    contracts.validate(contract)
    spec = contract["runtime"]
    policy = contract["prepared"]["sources"]["permissions"]
    return ["codex", "--model", spec["model"], "-c", 'model_reasoning_effort="' + spec["effort"] + '"',
            *permissions.launch_flags("worker", policy["cwd"], version=policy["version"])]


class InjectedHerdrBackend(cycle.OfflineBackend):
    """Runner is CONTROL-owned fixture code; never supplied by a task/model."""
    def __init__(self, owner, *, runner=None):
        if runner is None:
            effects.require_native_mediation()
        self.owner, self.runner = owner, runner
        contract = owner.load()["contract"]
        if contract["version"] != "owner-cycle-contract-v2":
            raise contracts.ContractError("observed transport requires its versioned contract")
        self.contract = contract

    def _attempt(self, admission):
        contracts.validate_admission(admission, self.contract)
        current = self.owner.load()
        if current["terminal"] or not current["attempts"] or current["attempts"][-1]["admission"] != admission:
            raise contracts.ContractError("transport is not for the current admitted attempt")
        return current["attempts"][-1]

    def before(self, admission):
        attempt = self._attempt(admission)
        if attempt["intent"]:
            raise contracts.ContractError("cannot replace a dispatched frontier")
        return self.runner.observe(copy.deepcopy(admission["surface"]), runtime.resource(admission))

    def send(self, admission, task):
        attempt = self._attempt(admission)
        if not attempt["intent"] or attempt["native_baseline"] is None or work.digest(task) != admission["prompt_sha256"]:
            raise contracts.ContractError("send lacks its persisted CONTROL intent")
        baseline = self.owner.json(attempt["native_baseline"])
        native.before_dispatch(baseline, admission, self.owner.get, contract=self.contract)
        # Conditional compare-and-send is a required fixture capability, not a
        # claim that Herdr's existing name-based prompt offers that operation.
        return self.runner.send_if_unchanged(copy.deepcopy(admission), launch(self.contract),
            fleet_json.canonical_bytes(task), {**baseline, "transcript": self.owner.get(baseline["transcript"])})

    def poll(self, admission):
        self._attempt(admission)
        return self.runner.observe(copy.deepcopy(admission["surface"]), runtime.resource(admission))

    def cancel(self, target):
        current = self.owner.load()
        if current["terminal"] or not current["attempts"]:
            raise contracts.ContractError("no active owned cancellation")
        attempt = current["attempts"][-1]
        admission = attempt["admission"]
        if target != runtime.resource(admission) or not attempt["cancel_intent"] or not attempt["cancel_signal"]:
            raise contracts.ContractError("cancel lacks its persisted exact-resource intent")
        snapshot = self.owner.json(attempt["cancel_signal"]["snapshot"])
        try:
            observed = native.observe(snapshot, contract=self.contract, admission=admission,
                                      baseline=self.owner.json(attempt["native_baseline"]), read=self.owner.get)
        except (ValueError, KeyError, TypeError):
            return {"status": "conflicting_native_observation"}
        if observed["binding"] != attempt["native_binding"] or not observed["active"]:
            return {"status": "reconcile_without_signalling"}
        # Runner must compare the complete latest observation atomically with
        # the signal. A new manual turn between poll and stop must reject it.
        return self.runner.stop_if_unchanged(copy.deepcopy(admission), copy.deepcopy(attempt["native_binding"]),
            {**snapshot, "transcript": self.owner.get(snapshot["transcript"])})
