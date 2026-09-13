# Single-Mission supervision and local measurement

`supervise` drives one explicit Mission in the foreground. It never launches a
second fleet, changes the Astra + three Sol roster, or changes the one-writer
policy. A separate nonblocking supervisor lock prevents two supervisor loops
owning the same Mission; the driver lock continues to serialize effects.

```sh
FLEET_RUNS_DIR=/absolute/runs just mission-supervise MISSION_UUID 60
FLEET_RUNS_DIR=/absolute/runs just mission-pause MISSION_UUID "Review current evidence" pause-1
FLEET_RUNS_DIR=/absolute/runs just mission-resume MISSION_UUID --json
FLEET_RUNS_DIR=/absolute/runs just mission-cancel-mission MISSION_UUID "Stop this Mission" cancel-1 --generation GENERATION_UUID
```

Pause/cancel commands persist requests without acquiring the long-lived driver
or supervisor lock. They do not imply that work has stopped. A driver observes
the request at its next safe boundary. `pause_requested` permits the admitted
in-flight turn or functional check to finish and reconcile; `paused` confirms
quiescence before new stages. Resume acknowledges the request and reuses the
existing prompt/results. The original Mission/admission deadline is unchanged.

Cancellation binds Mission, optional run and the currently owned generation.
Unknown/stale targets never signal another resource. A completed result is
consumed before cancellation; it can remain a successful stage in an abandoned
Mission. Confirmation requires matching run/prompt/generation evidence, inactive
admissions and reconciled functional execution. Cancellation does not implicitly
close idle role panes; the separately configured selective teardown controls that.
It never targets the focused pane. Repeated requests cannot reopen a terminal
Mission or replace the scope of an existing cancellation.

When cancellation selects one run and other admissions are active, the driver
reconciles the selected run regardless of admission order. It leaves the other
runs untouched and keeps Mission cancellation pending until they are inactive;
settling the selected run alone does not establish Mission-wide quiescence.

Supervision v1 records an explicit dispatch intent after admission authorization.
That ledger event is the boundary between a not-yet-dispatched stage and an
in-flight attempt. A pause that precedes the intent prevents it. A request after
the intent applies to an in-flight attempt, even if transport acknowledgement is
not yet available. A crash before the intent can continue that authorization;
after the intent, recovery observes the same run without blindly sending again.
Pre-existing authorizations at policy activation remain on the conservative
recovery-only path. This is durable reconciliation, not exactly-once delivery.

`--seconds` is a bounded observation budget (0–3600 seconds, exclusive of zero),
not a replacement Mission deadline or a guaranteed process-kill deadline. Runtime
waits use at most one-second slices; Herdr command timeouts are capped to the
remaining observation budget. Local Git operations retain their own timeouts;
synchronous snapshot/archive processing has file/count limits rather than a
preemptive wall timeout. An already-started functional check may finish
within its contract's wall limit. Thus a final bounded operation can outlast the
observation budget. Exhaustion returns pending evidence; a later supervisor uses
the same Mission. No background watchdog is installed. Pause and cancellation of
the functional runner preserve its immutable result; unconfirmed exact-container
cleanup keeps control pending rather than claiming a stop.

## Measurement semantics

The Mission ledger retains paired observation intervals and monotonic durations
for controller operations, controller waits, functional checks and supervisor
idle waits. A missing end after controller loss has a null duration. It is not
silently charged as execution, wait or pause. Cached functional receipts count
as controller operations, not a second functional execution.

Reports separate requested-pause intervals from confirmed-pause intervals and
show open intervals explicitly. Agent execution requires timestamps in its bound
Codex transcript. These measurements can overlap, so they must not be added to
produce wall time. A separate partition clips timestamp coverage to the Mission
window and applies the reported priority: functional, agent, controller wait,
controller operation, supervisor idle, confirmed pause, unknown. It exposes the
unclassified remainder. Monotonic measurement and timestamp coverage are distinct.

Token normalization takes differences between observed cumulative session
counters around the bound turn. Duplicate snapshots do not multiply usage.
Absent baselines, overlapping turns or counter resets yield null with a reason.
For the first session turn, a first total exactly equal to last-call usage can
establish a zero baseline. These are observed runtime counters, not invoices or
estimated spend. Model cost remains null without a durable billing receipt.
Trace export includes control requests, confirmations, dispatch attempts and
observation intervals, and remains observational rather than Mission authority.

## Reproducible local campaign

The campaign creates fresh synthetic repositories and Mission stores in a new
explicit output directory. It uses a simulated role backend and the real Fleet
driver, ledger, archive, functional controller and existing local Docker image.
It does not call Codex, Herdr or any provider process. Its Git fixture commits
exist only in those synthetic repositories. Existing output directories are
rejected. No image pulls or installations occur.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/fleet_herdr_campaign.py --output /absolute/new-campaign-directory
```

Requirements: repository tests/fixtures available, compatible local Docker
Linux/ARM64 cgroup-v2 engine, the existing `python:3.12-slim` image, Python and Git.
Use the same controller Python for contract creation/execution. The campaign has
five fixed scenarios: correct implementation, functional failure, artifact
rejection, pause/resume after an injected post-send exception, and functional
cancellation. Simulated role waits and a 50 ms pause are deliberate fixtures.

Each case retains result, report, trace, measured latencies, expected/observed
outcome, control interventions, injected loss, recovery calls and physical prompt
retry counts. The summary supplies numerator/denominator, runtime/source hashes
and sample counts. It is a component baseline for this machine, not a model
quality or cost benchmark. Model quality/tokens/cost stay `NOT_VERIFIED`/null;
zero provider calls does not mean free host/Docker execution. The historical
15-minute closure included human pause and is not used as a benchmark.

A future provider campaign needs separate authorization for a new real Mission,
provider usage/cost, an explicit Herdr session, fixed targets/contracts and a
comparable campaign design. Preparation can use the existing `mission-run.py dry`
command with both contracts. After that authorization, the corresponding `run`
and `supervise` commands can collect observed transcripts and counters. Real
quality, reliability, acceptance rates and billed cost cannot be inferred from
the synthetic campaign or the requested roster.

Preparation template (replace paths and session with the reviewed campaign's
values; `dry` compiles without launching a provider):

```sh
python3 -B scripts/mission-run.py --runs-dir /absolute/campaign-runs dry \
  stats-campaign "Implement the reviewed stats acceptance case" \
  --workflow herdr-implementation --target-repo /absolute/clean-target \
  --herdr-session REVIEWED_SESSION --timeout 600 \
  --acceptance-contract /absolute/artifact-contract.json \
  --functional-contract /absolute/functional-contract.json --json
```

Only after provider authorization, replace `dry` with `run` using the same
reviewed inputs. Continue its returned Mission UUID with `supervise --mission-id
MISSION_UUID --seconds 60 --json` under the same `--runs-dir`; do not create a new
Mission merely because the observation budget expired. Record all attempts,
accepted/rejected changes, human interventions and billing evidence; do not
infer cost per accepted change from incomplete counters.
