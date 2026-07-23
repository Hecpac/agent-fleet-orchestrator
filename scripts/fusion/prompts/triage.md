# TRIAGE — read-only diagnosis, round {{ROUND}}

The builder has failed the gate {{ROUND}} times. Diagnose the ROOT CAUSE by
inspecting the actual workspace state — verify against files, do not trust
either the builder's claims or the gate's phrasing.

Task: {{TASK}}

<gate>
{{GATE_SOURCE}}
</gate>

Latest gate output:

{{GATE_OUTPUT}}

Workspace to inspect (read-only): {{WORKSPACE}}

Answer with your diagnosis, then EXACTLY ONE final line:
TRIAGE_VERDICT: BUILDER_DEFECT — <one-line direction for the builder>
or
TRIAGE_VERDICT: GATE_DEFECT — <what the gate checks wrongly and why>
Declare GATE_DEFECT only when the gate itself checks something the task
never asked for or checks it incorrectly — not merely because passing it
is hard.
