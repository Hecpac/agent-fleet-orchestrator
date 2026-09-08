runs_dir := env_var_or_default("FLEET_RUNS_DIR", "orchestration/runs")

check:
    ./scripts/check-env.sh

# Portable contract used by hosted CI and safe to run locally.
ci:
    ./scripts/check-ci.sh

# Validate every typed Mission Control workflow without CMUX effects.
workflow-validate:
    python3 scripts/workflow_config.py validate workflows/*.yaml

# Ephemeral local-only RustFS WORM environment. State and credentials stay in /tmp.
worm-local-setup:
    python3 scripts/fleet_worm_local.py setup

worm-local-smoke:
    python3 scripts/fleet_worm_local.py smoke

worm-local-delete-test:
    python3 scripts/fleet_worm_local.py delete-test

worm-local-teardown:
    python3 scripts/fleet_worm_local.py teardown

worm-local-all:
    python3 scripts/fleet_worm_local.py all

provider-validate:
    python3 scripts/fleet_providers.py list
    python3 scripts/router_config.py --router orchestration/router.yaml validate

# Canonical durable Mission Control entry points.
mission feature objective *flags:
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" run {{feature}} "{{objective}}" {{flags}}

mission-dry feature objective *flags:
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" dry {{feature}} "{{objective}}" {{flags}}

# Mandatory artifact acceptance contract through the default Herdr workflow.
mission-verified feature objective contract *flags:
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" run {{feature}} "{{objective}}" --acceptance-contract "{{contract}}" {{flags}}

mission-show mission_id:
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" show --mission-id {{mission_id}}

mission-status mission_id *flags:
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" status --mission-id {{mission_id}} {{flags}}

# Context observation for explicit live agents; --publish only adds expiring UI badges.
herdr-context session_config roster *flags:
    python3 -B scripts/fleet_herdr_context.py --session-config "{{session_config}}" --roster "{{roster}}" {{flags}}

# Opt-in continuity controller. Default action only reads durable status.
herdr-continuity session_config plan state_dir *flags:
    python3 -B scripts/fleet_herdr_continuity.py --session-config "{{session_config}}" --plan "{{plan}}" --state-dir "{{state_dir}}" {{flags}}

mission-resume mission_id *flags:
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" resume --mission-id {{mission_id}} {{flags}}

mission-supervise mission_id seconds="60":
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" supervise --mission-id "{{mission_id}}" --seconds "{{seconds}}" --json

mission-pause mission_id reason key:
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" pause --mission-id "{{mission_id}}" --reason "{{reason}}" --idempotency-key "{{key}}" --json

mission-cancel-mission mission_id reason key *flags:
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" cancel-mission --mission-id "{{mission_id}}" --reason "{{reason}}" --idempotency-key "{{key}}" {{flags}} --json

herdr-campaign output:
    python3 scripts/fleet_herdr_campaign.py --output "{{output}}"

mission-cancel mission_id run_id reason key *flags:
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" cancel --mission-id {{mission_id}} --run-id {{run_id}} --reason "{{reason}}" --idempotency-key {{key}} {{flags}}

mission-approve mission_id scope *flags:
    python3 scripts/fleet-approve.py --runs-dir orchestration/runs --mission-id {{mission_id}} --scope {{scope}} {{flags}}

mission-approve-archive mission_id scope *flags:
    python3 scripts/fleet-approve.py --runs-dir orchestration/runs --mission-id {{mission_id}} --scope {{scope}} --archive {{flags}}

mission-advisory mission_id instance objective key *flags:
    python3 scripts/fleet_assured_runner.py --runs-dir orchestration/runs --mission-id {{mission_id}} advisory --instance {{instance}} --objective "{{objective}}" --idempotency-key {{key}} {{flags}}

mission-audit-verify mission_id:
    python3 scripts/fleet_audit_client.py --runs-dir orchestration/runs --mission-id {{mission_id}} verify

mission-archive mission_id manifest *flags:
    python3 scripts/fleet_archive.py create --runs-dir orchestration/runs --mission-id {{mission_id}} --manifest {{manifest}} {{flags}}

mission-archive-verify archive *flags:
    python3 scripts/fleet_archive.py verify {{archive}} {{flags}}

mission-herdr-archive-verify mission_id *flags:
    python3 scripts/fleet_herdr_archive.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} {{flags}}

# Read-only runtime discovery; never pulls an image or launches a provider.
functional-spec tests:
    python3 scripts/fleet_functional.py spec --tests "{{tests}}"

# Execute the frozen required check, or return its durable existing receipt.
mission-functional-run mission_id:
    python3 scripts/fleet_functional.py run --runs-dir "{{runs_dir}}" --mission-id {{mission_id}}

mission-report mission_id *flags:
    python3 scripts/fleet_report.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} {{flags}}

mission-trace mission_id *flags:
    python3 scripts/fleet_export_trace.py --runs-dir "{{runs_dir}}" --mission-id {{mission_id}} {{flags}}

mission-control-start mission_id:
    python3 scripts/fleet_control_service.py --runs-dir orchestration/runs --mission-id {{mission_id}} start

mission-control-health mission_id:
    python3 scripts/fleet_control_service.py --runs-dir orchestration/runs --mission-id {{mission_id}} health

mission-control-stop mission_id:
    python3 scripts/fleet_control_service.py --runs-dir orchestration/runs --mission-id {{mission_id}} stop

mission-decision-request mission_id brief key:
    python3 scripts/fleet_control.py --runs-dir orchestration/runs --mission-id {{mission_id}} request-decision --brief-file {{brief}} --idempotency-key {{key}}

mission-decisions mission_id *flags:
    python3 scripts/fleet-decision.py --runs-dir orchestration/runs --mission-id {{mission_id}} list {{flags}}

mission-decision-show mission_id decision_id *flags:
    python3 scripts/fleet-decision.py --runs-dir orchestration/runs --mission-id {{mission_id}} {{flags}} show --decision-id {{decision_id}}

mission-decision-resolve mission_id decision_id option_id key reason:
    python3 scripts/fleet-decision.py --runs-dir orchestration/runs --mission-id {{mission_id}} resolve --decision-id {{decision_id}} --option-id {{option_id}} --idempotency-key {{key}} --reason "{{reason}}"

manifest-inspect manifest:
    python3 scripts/fleet_manifest.py inspect {{manifest}}

manifest-migrate manifest *flags:
    python3 scripts/fleet_manifest.py migrate {{manifest}} {{flags}}

# Explicit legacy CMUX Dan+ entry point; the normal entry point is `mission`.
# Ex: just dan checkout-fix "fix checkout retries and verify the regression"
dan feature task *flags:
    python3 scripts/fleet-run.py {{feature}} "{{task}}" {{flags}}

# Fusion harness F0: independent model perspectives on one question.
# Ex: just opinion "top 3 sqlite persistence strategies for the ledger" --panel
opinion question *flags:
    python3 scripts/fusion/fusion_harness.py opinion "{{question}}" {{flags}}

# Fusion harness F1: two perspectives plus an adjudicated synthesis.
# Ex: just fusion "should the ledger move to sqlite?" "focus on migration cost"
fusion question *flags:
    python3 scripts/fusion/fusion_harness.py fusion "{{question}}" {{flags}}

# Fusion harness F2: gate-first build loop in an isolated workspace.
# Ex: just auto-validate "create hello.py that prints the first 10 primes"
auto-validate task *flags:
    python3 scripts/fusion/fusion_harness.py auto-validate "{{task}}" {{flags}}

# Preview the exact autonomous lead mission without touching CMUX.
dan-dry feature task *flags:
    python3 scripts/fleet-run.py {{feature}} "{{task}}" --dry-run {{flags}}

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
    python3 scripts/mission-run.py --runs-dir "{{runs_dir}}" status --mission-id {{mission_id}} {{flags}}

# Historical CMUX radar, explicitly outside the daily Herdr path.
legacy-status *flags:
    python3 scripts/fleet_status.py {{flags}}

# Advance the durable capability gate with an evidence reference.
# Standalone guided BUILD exit uses --approved-by <operator-attestation>.
# Mission-bound assured fleets instead require --approval-event-sha256.
advance feature phase evidence *flags:
    python3 scripts/fleet_state.py advance "${FLEET_RUNS_DIR:-orchestration/runs}/fleet-{{feature}}.manifest" {{phase}} --evidence {{evidence}} {{flags}}

# Send to an interactive agent through UUID and phase validation.
send feature instance task:
    ./scripts/fleet-send.sh {{feature}} {{instance}} "{{task}}"

# Explicitly release one indeterminate/active frontier run.
abandon feature instance run_id reason="operator_abandoned":
    ./scripts/fleet-abandon.sh {{feature}} {{instance}} {{run_id}} "{{reason}}"

# Start one bounded FDP-2 Maker/Checker conversation from a strict JSON spec.
fdp2-start feature spec key:
    python3 scripts/fleet_dialogue_controller.py start orchestration/runs --feature {{feature}} --spec-file {{spec}} --idempotency-key {{key}}

fdp2-show feature:
    python3 scripts/fleet_dialogue_controller.py show orchestration/runs --feature {{feature}}

fdp2-step-run feature run_id key:
    python3 scripts/fleet_dialogue_controller.py step orchestration/runs --feature {{feature}} --run-id {{run_id}} --idempotency-key {{key}}

fdp2-step-message feature message_id key:
    python3 scripts/fleet_dialogue_controller.py step orchestration/runs --feature {{feature}} --message-id {{message_id}} --idempotency-key {{key}}

fdp2-verify feature:
    python3 scripts/fleet_dialogue_controller.py verify orchestration/runs --feature {{feature}}

fdp2-abandon feature reason key:
    python3 scripts/fleet_dialogue_controller.py abandon orchestration/runs --feature {{feature}} --reason "{{reason}}" --idempotency-key {{key}}

# Start and step the independent FDP-3 GLM -> Claude assurance chain.
fdp3-start feature key:
    python3 scripts/fleet_assurance_controller.py start orchestration/runs --feature {{feature}} --idempotency-key {{key}}

fdp3-show feature:
    python3 scripts/fleet_assurance_controller.py show orchestration/runs --feature {{feature}}

fdp3-step-run feature run_id key:
    python3 scripts/fleet_assurance_controller.py step orchestration/runs --feature {{feature}} --run-id {{run_id}} --idempotency-key {{key}}

fdp3-step-message feature message_id key:
    python3 scripts/fleet_assurance_controller.py step orchestration/runs --feature {{feature}} --message-id {{message_id}} --idempotency-key {{key}}

fdp3-step-phase feature key:
    python3 scripts/fleet_assurance_controller.py step orchestration/runs --feature {{feature}} --phase-advanced --idempotency-key {{key}}

fdp3-verify feature:
    python3 scripts/fleet_assurance_controller.py verify orchestration/runs --feature {{feature}}

fdp3-abandon feature reason key:
    python3 scripts/fleet_assurance_controller.py abandon orchestration/runs --feature {{feature}} --reason "{{reason}}" --idempotency-key {{key}}

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
