# Herdr Mission Control

The default canonical workflow is `herdr-implementation`, with the pinned
`astra_sol` roster: Lead `gpt-6-astra`, Worker/Reviewer/Verifier `gpt-5.6-sol`.
All launches request `model_reasoning_effort="high"`. Result collection checks
the actual Codex turn context in addition to the launch receipt.

```sh
python3 scripts/mission-run.py dry sample "Implement the agreed task" \
  --target-repo /absolute/path/to/clean/repository \
  --acceptance-contract /absolute/path/to/contract.json --json

python3 scripts/mission-run.py run sample "Implement the agreed task" \
  --target-repo /absolute/path/to/clean/repository \
  --herdr-session agent-fleet-orchestrator \
  --acceptance-contract /absolute/path/to/contract.json --json

python3 scripts/mission-run.py resume --mission-id MISSION_UUID --json

python3 scripts/mission-run.py cancel --mission-id MISSION_UUID --run-id RUN_UUID \
  --reason "Cancel this exact run" --idempotency-key CANCEL_KEY --json
```

`--runs-dir` is a global argument, before `run`, `resume`, or `dry`. `--router`
remains an explicit source for compilation; its canonical snapshot is frozen in
the Mission. `--herdr-session` is also frozen in runtime options. Session names
never mean the currently focused workspace. `just mission`, `mission-dry`, and
`mission-verified` use the same entry point.
For `just`, select a store with `FLEET_RUNS_DIR=/absolute/runs just mission-status
MISSION_UUID --json` (or `just --set runs_dir /absolute/runs ...`). The recipe
places the global option before the subcommand. Do not pass `--runs-dir` in the
trailing recipe flags. `just status MISSION_UUID` and `just mission-status
MISSION_UUID` read native Mission state; `just legacy-status` is the historical
CMUX radar. Direct `mission-run.py` and `fleet_report.py` commands also honor
`FLEET_RUNS_DIR`; an explicit `--runs-dir` takes precedence.

```sh
FLEET_RUNS_DIR=/absolute/runs just status MISSION_UUID --json
FLEET_RUNS_DIR=/absolute/runs just mission-report MISSION_UUID --json
FLEET_RUNS_DIR=/absolute/runs just mission-herdr-archive-verify MISSION_UUID
```

The Herdr report has `schema_version=2`. It counts distinct admitted run IDs,
observed Codex sessions, delegation records and synthesis turns separately. The
complete default sequence has five runs, four observed sessions, four delegation
records and one synthesis. Requested identities come from the compiled policy;
observed provider/model/effort require a bound completed transcript. Missing
evidence produces `null` plus a reason; it never turns configured identities into
observations. Transport `settled` does not finalize an admission. Mission status
comes from the ledger, while archive state is independently `verified`, `absent`,
`unverifiable` (including unanchored staging) or `corrupt`. Acceptance is exposed
only after archive verification. Bound Codex cumulative counters are normalized
only when the turn has a provable baseline; they are runtime observations, not
billing. Cost remains null without a billing receipt. Controller operation/wait,
functional execution, requested pause, confirmed pause and unknown intervals are
reported separately where instrumented. Categories can overlap; the report also
provides a disjoint wall-coverage partition. Missing interval ends stay unknown. Wall
time includes pauses and is not an execution benchmark. These readers do not
query live Herdr or CMUX.

The controller advances planning, implementation, review, verification and Lead
synthesis. Worker receives a private clone. The other roles launch with read-only
sandboxes. No stage creates commits or publishes changes back into the source
checkout. The candidate and its portable patch remain available for review.
Dirty source checkouts currently require snapshot support and are rejected before
agent effects; `--allow-dirty-baseline` does not silently drop their changes.
Each launch supplies an exact-candidate, invocation-only `projects` override with
`trust_level="untrusted"`. It does not edit the global Codex configuration or enable
project-local hooks/configuration. Update or approval dialogs are not accepted
automatically. After an operator exits an unsubmitted startup, `retry-start
--mission-id MISSION_UUID --instance lead --json` can retry only if the exact owned
pane is back at its shell and Herdr proves the old agent absent. Attempts retain
separate receipts; this operation refuses any existing submission.

Every prompt has a Mission admission, deterministic run ID, effect hash and
durable transport intent. The transport is bound to a named Herdr session,
workspace, tab, pane, terminal, agent session and generation. On ambiguous delivery,
recovery observes the same run; it does not automatically resend the prompt.
This is a single physical attempt with durable reconciliation, not an unconditional
exactly-once delivery guarantee.

Herdr `idle`, `done`, and `settled` only describe execution state. A role result
requires its bound Codex transcript, a completed turn, the expected model and
reasoning, structured output, and matching artifact hashes. After Worker finishes,
the candidate Git tree is frozen. Review, Verify and synthesis must refer to that
same tree; later candidate changes fail validation.

Permission policy v1 is shared by launch construction, completed-turn collection,
cached-result recovery, admission finalization and offline archive verification.
Every context in the bound turn, including compaction contexts, must record the
exact candidate `cwd`, expected model, `high`, and `approval_policy="never"`.
Lead, Reviewer and Verifier require `sandbox_policy={"type":"read-only"}`.
Worker requires `workspace-write`, `network_access=false`,
`exclude_tmpdir_env_var=false`, and `exclude_slash_tmp=false`; `writable_roots`
may be omitted or empty. Launches explicitly request these values. Missing,
changed or additional sandbox fields fail closed. Policy v1 deliberately retains
Codex temporary-directory write access for Worker; it does not promise that the
candidate is the only writable filesystem location. An incomplete turn remains
pending. A completed incompatible result cannot finalize its admission or advance
the next stage, including on recovery. The attestation covers recorded Codex
configuration, not an independent OS containment test.

The Herdr archive is explicitly versioned separately from historical CMUX
archives. It retains the Mission ledger, compiled policy, runtime options,
source objective, backend binding, all CAS artifacts, four mandatory role results,
the final tree and a binary Git patch. Archive verification replays the ledger and
checks role admissions, content hashes, the Git tree and acceptance predicates.
The immutable Mission ledger anchors the archive index. This is the existing S0
trusted-host/Unix-UID boundary, not external custody or adversarial same-UID isolation.
Before that anchor, creation returns `staged_valid=true, valid=false`; public
verification requires the anchor. Each role retains its original backend envelope
and exact Codex transcript segment through `task_complete` in CAS. Offline checks
revalidate session, model, `high`, prompt, final answer, unique writer and artifact
content against the frozen tree. Snapshot creation uses a private Git index, pins
the baseline OID and reapplies the binary patch to prove the resulting tree.

New archives use schema v3, or v4 when a required functional profile passes, and
`permissions_policy_version=1`; they recheck all
five admission results, including the initial Lead plan. Historical schema v2
archives keep their original validity rules and return
`permissions.status="not_attested"`. An optional read-only reassessment is
available with `just mission-herdr-archive-verify MISSION_UUID
--attest-permissions`. It adds a separate `permissions_reassessment` on success;
it never upgrades or rewrites the historical archive, its hashes or its ledger
anchor. Missing historical fields prevent reassessment while ordinary historical
verification remains available. Historical CMUX report/archive behavior is
separate and unchanged.

Historical readability does not authorize a new Mission completion. Before a
nonterminal driver proceeds, CONTROL freezes `herdr_finalization_policy_frozen`
in the Mission ledger, bound to its compiled digest: minimum archive schema 3,
permission policy 1, and five required turns. Completion re-verifies the actual
archive against that durable policy before anchoring and again before appending
the terminal result. This also applies when recovering directly from `completing`
or `archived`, without a candidate or live runtime. Lowering the index to v2
cannot lower the ledger requirement. An unfinished v2 archive cannot produce a
new success through this driver, even if ordinary historical verification passes;
it requires separate remediation, not an automatic in-place upgrade. Already
terminal historical missions return their existing verdict without freezing a
new policy or rewriting their evidence.

An acceptance contract uses the existing `text_contains`, `sha256`, and
`json_equals` predicates against the archived tree. Functional tests are separate
evidence; a model's PASS report alone cannot override a rejected contract.
No contract means `not_evaluated`, so the driver cannot claim accepted completion.

`run` and `resume` return `3` when further action is needed or the result is
`blocked`; `failed`, `indeterminate`, and `abandoned` return `1`; successful terminal
completion returns `0`. A durable terminal can be read without a live Herdr UI.
When requested, teardown is tracked separately; a cleanup problem does not rewrite
an already durable semantic verdict.
Use `--teardown` at creation to close only the mission-owned workspace after all
its agents are quiescent. `show` reads durable state. `cancel` requires an exact
authorized run; it never targets the focused pane or signals a finalized run.
The cancellation request and confirmed cancellation are distinct. Requests now
persist through a short ledger transaction independently of the waiting driver's
lock. The existing exact-run command and `cancel-mission` retain the owned backend
generation; optional `--generation` rejects a stale target. Repeated keys reuse
the request. A result that arrives first is retained before any signal. Confirmation
requires matching terminal evidence and functional-container reconciliation;
the Mission terminates `abandoned` (exit 1). Terminal historical state is unchanged.

`--timeout` bounds the mission from its creation timestamp; the controller enforces
it when run/resumed, not through a background watchdog. Positive token budgets are
currently rejected because this Herdr transport cannot enforce them. The bounded
default lane permits five role turns (Lead appears twice), not autonomous recursion.

The first native lane is local low/medium-risk work. High/unknown risk pauses for
assurance; this profile cannot silently borrow the historical heterogeneous assured
roster. Regulated/WORM and sensitive-data archival require their separately
authorized lanes. Existing CMUX evidence retains its original provenance.

## Tooling decisions

Use the installed Herdr CLI for discrete actions and bounded server-side waits.
Its lifecycle wait is not a per-prompt completion receipt. The Mission ledger and
Codex transcript provide the correlation needed here.
[Herdr agent automation](https://herdr.dev/docs/agent-automation/).

Herdr's socket event subscription can reduce repeated queries, but it does not
replace reconciliation after disconnection. A later App Server adapter could offer
explicit turn IDs and interruption; it requires a separate compatibility test with
interactive Herdr sessions. This migration does not install another SDK, queue,
database or telemetry service. No cost or latency improvement is claimed without
measurement. [Herdr socket API](https://herdr.dev/docs/socket-api/).
