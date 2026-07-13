check:
    ./scripts/check-env.sh

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

# Decision-queue radar: which agents are blocked waiting on a human, and how long.
status:
    python3 scripts/fleet_status.py

# Advance the durable capability gate with an evidence reference.
# Leaving BUILD additionally requires --approved-by <human> (pass it via flags).
advance feature phase evidence *flags:
    python3 scripts/fleet_state.py advance orchestration/runs/fleet-{{feature}}.manifest {{phase}} --evidence {{evidence}} {{flags}}

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
