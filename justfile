runs_dir := env_var_or_default("FLEET_RUNS_DIR", "orchestration/runs")
fleet_python := env_var_or_default("FLEET_PYTHON", "python3.12")
# Retain the existing personal override; its default shares the product runtime.
personal_python := fleet_python

# Personal CLI path. Diagnostics never start agents or send model prompts.
personal-check target:
    "{{personal_python}}" -B scripts/fleet_personal_preflight.py --target-repo "{{target}}"

personal-snapshot source output *flags:
    "{{personal_python}}" -B scripts/fleet_personal_snapshot.py --source "{{source}}" --output "{{output}}" {{flags}}

personal-prepare pool target session:
    "{{personal_python}}" -B scripts/fleet_personal_pool.py --state-dir "{{pool}}" prepare --target-repo "{{target}}" --session "{{session}}"

personal-show pool:
    "{{personal_python}}" -B scripts/fleet_personal_pool.py --state-dir "{{pool}}" show

personal-close pool:
    "{{personal_python}}" -B scripts/fleet_personal_pool.py --state-dir "{{pool}}" close

personal-assign pool feature objective contract:
    "{{personal_python}}" -B scripts/fleet_personal_pool.py --state-dir "{{pool}}" assign "{{feature}}" "{{objective}}" --acceptance-contract "{{contract}}" --runs-dir "{{runs_dir}}"

# Explicit names for the retained historical fleet entry points.
legacy-fleet feature *roles:
    ./scripts/fleet-up.sh {{feature}} {{roles}}

legacy-fleet-down feature:
    ./scripts/fleet-down.sh {{feature}}

check:
    ./scripts/check-env.sh

# Portable contract used by hosted CI and safe to run locally.
ci:
    FLEET_PYTHON="{{fleet_python}}" ./scripts/check-ci.sh

# Validate every workflow schema without admitting effects.
workflow-validate:
    "{{fleet_python}}" -B scripts/workflow_config.py validate workflows/*.yaml

# Inspect compiler availability separately from schema validity and runtime readiness.
workflow-catalog:
    "{{fleet_python}}" -B scripts/workflow_config.py catalog

# Ephemeral local-only RustFS WORM environment. State and credentials stay in /tmp.
worm-local-setup:
    "{{fleet_python}}" -B scripts/fleet_worm_local.py setup

worm-local-smoke:
    "{{fleet_python}}" -B scripts/fleet_worm_local.py smoke

worm-local-delete-test:
    "{{fleet_python}}" -B scripts/fleet_worm_local.py delete-test

worm-local-teardown:
    "{{fleet_python}}" -B scripts/fleet_worm_local.py teardown

worm-local-all:
    "{{fleet_python}}" -B scripts/fleet_worm_local.py all

provider-validate:
    "{{fleet_python}}" -B scripts/fleet_providers.py list
    "{{fleet_python}}" -B scripts/router_config.py --router orchestration/router.yaml validate

# Canonical durable Mission Control entry points.
mission feature objective *flags:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" run {{feature}} "{{objective}}" {{flags}}

mission-dry feature objective *flags:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" dry {{feature}} "{{objective}}" {{flags}}

# Mandatory artifact acceptance contract through the default Herdr workflow.
mission-verified feature objective contract *flags:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" run {{feature}} "{{objective}}" --acceptance-contract "{{contract}}" {{flags}}

mission-show mission_id:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" show --mission-id {{mission_id}}

mission-status mission_id *flags:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" status --mission-id {{mission_id}} {{flags}}

# Context observation for explicit live agents; --publish only adds expiring UI badges.
herdr-context session_config roster *flags:
    "{{fleet_python}}" -B scripts/fleet_herdr_context.py --session-config "{{session_config}}" --roster "{{roster}}" {{flags}}

# Opt-in continuity controller. Default action only reads durable status.
herdr-continuity session_config plan state_dir *flags:
    "{{fleet_python}}" -B scripts/fleet_herdr_continuity.py --session-config "{{session_config}}" --plan "{{plan}}" --state-dir "{{state_dir}}" {{flags}}

mission-resume mission_id *flags:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" resume --mission-id {{mission_id}} {{flags}}

mission-supervise mission_id seconds="60":
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" supervise --mission-id "{{mission_id}}" --seconds "{{seconds}}" --json

mission-pause mission_id reason key:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" pause --mission-id "{{mission_id}}" --reason "{{reason}}" --idempotency-key "{{key}}" --json

mission-cancel-mission mission_id reason key *flags:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" cancel-mission --mission-id "{{mission_id}}" --reason "{{reason}}" --idempotency-key "{{key}}" {{flags}} --json

herdr-campaign output:
    "{{fleet_python}}" -B scripts/fleet_herdr_campaign.py --output "{{output}}"

mission-cancel mission_id run_id reason key *flags:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" cancel --mission-id {{mission_id}} --run-id {{run_id}} --reason "{{reason}}" --idempotency-key {{key}} {{flags}}

mission-approve mission_id scope *flags:
    "{{fleet_python}}" -B scripts/fleet-approve.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} --scope {{scope}} {{flags}}

mission-approve-archive mission_id scope *flags:
    "{{fleet_python}}" -B scripts/fleet-approve.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} --scope {{scope}} --archive {{flags}}

mission-advisory mission_id instance objective key *flags:
    "{{fleet_python}}" -B scripts/fleet_assured_runner.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} advisory --instance {{instance}} --objective "{{objective}}" --idempotency-key {{key}} {{flags}}

mission-audit-verify mission_id:
    "{{fleet_python}}" -B scripts/fleet_audit_client.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} verify

mission-archive mission_id manifest *flags:
    "{{fleet_python}}" -B scripts/fleet_archive.py create --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} --manifest {{manifest}} {{flags}}

mission-archive-verify archive *flags:
    "{{fleet_python}}" -B scripts/fleet_archive.py verify {{archive}} {{flags}}

mission-herdr-archive-verify mission_id *flags:
    "{{fleet_python}}" -B scripts/fleet_herdr_archive.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} {{flags}}

# Read-only runtime discovery; never pulls an image or launches a provider.
functional-spec tests:
    "{{fleet_python}}" -B scripts/fleet_functional.py spec --tests "{{tests}}"

# Execute the frozen required check, or return its durable existing receipt.
mission-functional-run mission_id:
    "{{fleet_python}}" -B scripts/fleet_functional.py run --runs-dir "{{runs_dir}}" --mission-id {{mission_id}}

mission-report mission_id *flags:
    "{{fleet_python}}" -B scripts/fleet_report.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} {{flags}}

mission-trace mission_id *flags:
    "{{fleet_python}}" -B scripts/fleet_export_trace.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} {{flags}}

mission-control-start mission_id:
    "{{fleet_python}}" -B scripts/fleet_control_service.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} start

mission-control-health mission_id:
    "{{fleet_python}}" -B scripts/fleet_control_service.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} health

mission-control-stop mission_id:
    "{{fleet_python}}" -B scripts/fleet_control_service.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} stop

mission-decision-request mission_id brief key:
    "{{fleet_python}}" -B scripts/fleet_control.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} request-decision --brief-file {{brief}} --idempotency-key {{key}}

mission-decisions mission_id *flags:
    "{{fleet_python}}" -B scripts/fleet-decision.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} list {{flags}}

mission-decision-show mission_id decision_id *flags:
    "{{fleet_python}}" -B scripts/fleet-decision.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} {{flags}} show --decision-id {{decision_id}}

mission-decision-resolve mission_id decision_id option_id key reason:
    "{{fleet_python}}" -B scripts/fleet-decision.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} resolve --decision-id {{decision_id}} --option-id {{option_id}} --idempotency-key {{key}} --reason "{{reason}}"

manifest-inspect manifest:
    "{{fleet_python}}" -B scripts/fleet_manifest.py inspect {{manifest}}

manifest-migrate manifest *flags:
    "{{fleet_python}}" -B scripts/fleet_manifest.py migrate {{manifest}} {{flags}}

# Explicit legacy CMUX Dan+ entry point; the normal entry point is `mission`.
# Ex: just dan checkout-fix "fix checkout retries and verify the regression"
dan feature task *flags:
    "{{fleet_python}}" -B scripts/fleet-run.py {{feature}} "{{task}}" {{flags}}

# Fusion harness F0: independent model perspectives on one question.
# Ex: just opinion "top 3 sqlite persistence strategies for the ledger" --panel
opinion question *flags:
    "{{fleet_python}}" -B scripts/fusion/fusion_harness.py opinion "{{question}}" {{flags}}

# Fusion harness F1: two perspectives plus an adjudicated synthesis.
# Ex: just fusion "should the ledger move to sqlite?" "focus on migration cost"
fusion question *flags:
    "{{fleet_python}}" -B scripts/fusion/fusion_harness.py fusion "{{question}}" {{flags}}

# Fusion harness F2: gate-first build loop in an isolated workspace.
# Ex: just auto-validate "create hello.py that prints the first 10 primes"
auto-validate task *flags:
    "{{fleet_python}}" -B scripts/fusion/fusion_harness.py auto-validate "{{task}}" {{flags}}

# Preview the exact autonomous lead mission without touching CMUX.
dan-dry feature task *flags:
    "{{fleet_python}}" -B scripts/fleet-run.py {{feature}} "{{task}}" --dry-run {{flags}}

# Boot a cmux fleet from the default `small` preset, or pass explicit instances.
# Ex: just fleet review build=codex verify=reviewer
fleet feature *roles:
    ./scripts/fleet-up.sh {{feature}} {{roles}}

# Boot a named capability preset. Ex: just fleet-preset audit audit
fleet-preset feature preset:
    ./scripts/fleet-up.sh {{feature}} --preset {{preset}}

# Tear down a fleet workspace and its manifest. Ex: just fleet-down sse
fleet-down feature:
    ./scripts/fleet-down.sh {{feature}}

# Agent race: same task to N agents; first durable success is an unverified candidate.
# Ex: just race hotfix "find the bug in scripts/foo.sh" codex minimax
race name task *roles:
    ./scripts/fleet-race.sh {{name}} "{{task}}" {{roles}}

# Daily status reads native Mission evidence; an explicit mission ID is required.
status mission_id *flags:
    "{{fleet_python}}" -B scripts/mission-run.py --runs-dir "{{runs_dir}}" status --mission-id {{mission_id}} {{flags}}

# Historical CMUX radar, explicitly outside the daily Herdr path.
legacy-status *flags:
    "{{fleet_python}}" -B scripts/fleet_status.py {{flags}}

# Advance the durable capability gate with an evidence reference.
# Standalone guided BUILD exit uses --approved-by <operator-attestation>.
# Mission-bound assured fleets instead require --approval-event-sha256.
advance feature phase evidence *flags:
    "{{fleet_python}}" -B scripts/fleet_state.py advance "${FLEET_RUNS_DIR:-orchestration/runs}/fleet-{{feature}}.manifest" {{phase}} --evidence {{evidence}} {{flags}}

# Send to an interactive agent through UUID and phase validation.
send feature instance task:
    ./scripts/fleet-send.sh {{feature}} {{instance}} "{{task}}"

# Explicitly release one indeterminate/active frontier run.
abandon feature instance run_id reason="operator_abandoned":
    ./scripts/fleet-abandon.sh {{feature}} {{instance}} {{run_id}} "{{reason}}"

# Start one bounded FDP-2 Maker/Checker conversation from a strict JSON spec.
fdp2-start feature spec key:
    "{{fleet_python}}" -B scripts/fleet_dialogue_controller.py start orchestration/runs --feature {{feature}} --spec-file {{spec}} --idempotency-key {{key}}

fdp2-show feature:
    "{{fleet_python}}" -B scripts/fleet_dialogue_controller.py show orchestration/runs --feature {{feature}}

fdp2-step-run feature run_id key:
    "{{fleet_python}}" -B scripts/fleet_dialogue_controller.py step orchestration/runs --feature {{feature}} --run-id {{run_id}} --idempotency-key {{key}}

fdp2-step-message feature message_id key:
    "{{fleet_python}}" -B scripts/fleet_dialogue_controller.py step orchestration/runs --feature {{feature}} --message-id {{message_id}} --idempotency-key {{key}}

fdp2-verify feature:
    "{{fleet_python}}" -B scripts/fleet_dialogue_controller.py verify orchestration/runs --feature {{feature}}

fdp2-abandon feature reason key:
    "{{fleet_python}}" -B scripts/fleet_dialogue_controller.py abandon orchestration/runs --feature {{feature}} --reason "{{reason}}" --idempotency-key {{key}}

# Start and step the independent FDP-3 GLM -> Claude assurance chain.
fdp3-start feature key:
    "{{fleet_python}}" -B scripts/fleet_assurance_controller.py start orchestration/runs --feature {{feature}} --idempotency-key {{key}}

fdp3-show feature:
    "{{fleet_python}}" -B scripts/fleet_assurance_controller.py show orchestration/runs --feature {{feature}}

fdp3-step-run feature run_id key:
    "{{fleet_python}}" -B scripts/fleet_assurance_controller.py step orchestration/runs --feature {{feature}} --run-id {{run_id}} --idempotency-key {{key}}

fdp3-step-message feature message_id key:
    "{{fleet_python}}" -B scripts/fleet_assurance_controller.py step orchestration/runs --feature {{feature}} --message-id {{message_id}} --idempotency-key {{key}}

fdp3-step-phase feature key:
    "{{fleet_python}}" -B scripts/fleet_assurance_controller.py step orchestration/runs --feature {{feature}} --phase-advanced --idempotency-key {{key}}

fdp3-verify feature:
    "{{fleet_python}}" -B scripts/fleet_assurance_controller.py verify orchestration/runs --feature {{feature}}

fdp3-abandon feature reason key:
    "{{fleet_python}}" -B scripts/fleet_assurance_controller.py abandon orchestration/runs --feature {{feature}} --reason "{{reason}}" --idempotency-key {{key}}

pull-models:
    ./scripts/pull-local-models.sh

triage task:
    ./scripts/run-local-worker.sh triage "{{task}}"

review task:
    ./scripts/run-local-worker.sh reviewer "{{task}}"

code task:
    ./scripts/run-local-worker.sh code_worker "{{task}}"

light-code task:
    ./scripts/run-local-worker.sh light_code "{{task}}"
