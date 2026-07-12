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

# Agent race: same task to N agents; first completion is an unverified candidate.
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
