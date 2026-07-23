# BUILDER — round {{ROUND}} of {{MAX_ROUNDS}}

{{TASK_BLOCK}}

Your working directory is the workspace; build the task there. The
acceptance gate below is VISIBLE but IMMUTABLE — the harness restores a
sealed copy before every run, so editing it cannot help you and is
recorded. Your work is done only when "uv run gate.py <workspace>" exits 0.

<gate>
{{GATE_SOURCE}}
</gate>

{{ROUND_FEEDBACK}}
