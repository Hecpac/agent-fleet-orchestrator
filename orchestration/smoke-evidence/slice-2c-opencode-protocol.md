# Slice 2C OpenCode completion smoke

Date: 2026-07-12

Verdict: PASS

## Runtime adaptation

No daemon restart applies to this repository. The changed surfaces are
`fleet-up`, `fleet-send`, `fleet-wait`, and `fleet_frontier.py`; each smoke fleet
loaded the uncommitted working-tree scripts in a new process. The disposable
workspace was `workspace:23` / `DE9C5AF7-815B-477F-94EA-E19306C276C4` and was
closed after both OpenCode panes were visibly idle.

## Static and unit evidence

- `python3 -m unittest discover -s tests`: 102 tests passed.
- `python3 -m py_compile scripts/fleet_frontier.py scripts/router_config.py`: passed.
- `bash -n scripts/fleet-up.sh scripts/fleet-send.sh`: passed.
- `git diff --check`: passed.
- Two independent read-only reviews returned PASS after their findings were
  addressed.

## Live round trips

### GLM short response

- Run: `fcd38da4-8a41-45e5-89be-a786c0bd9906`
- Session: `opencode-ses_0a751d40fffeuqaO4u2zXd9uu5`
- Identity: `zai` / `glm-5.2`
- Submitted: `2026-07-12T23:33:57.923Z`
- Completed: `2026-07-12T23:34:08.674Z`
- Round trip: 10.751 seconds
- Result: `succeeded`, `frontier_sentinel_verified`

### MiniMax second turn in the same session

- Run: `3976257d-73f6-43bc-bd0b-4ee4d6e7b8f0`
- Session: `opencode-ses_0a751d59fffe8gEtyixxAlkeXV`
- Identity: `minimax` / `MiniMax-M3`
- Submitted: `2026-07-12T23:34:31.118Z`
- Completed: `2026-07-12T23:34:39.129Z`
- Round trip: 8.011 seconds
- Result: `succeeded`, `frontier_sentinel_verified`
- This reused the session from a prior rejected run and therefore verifies that
  stale response evidence was not reused.

### GLM response beyond workstream truncation

- Run: `efd8b54e-5798-4b37-83d4-d019fc355c60`
- Session: `opencode-ses_0a751d40fffeuqaO4u2zXd9uu5`
- Identity: `zai` / `glm-5.2`
- Submitted: `2026-07-12T23:35:01.605Z`
- Completed: `2026-07-12T23:35:21.057Z`
- Full assistant response read from `opencode db`: 2721 characters.
- Result: `succeeded`, `frontier_sentinel_verified`
- This exceeds the cmux plugin's 1000-character `assistantPreamble` limit.

## Explicit no-regression

MiniMax run `8a7a55e2-c3fc-4e69-a9aa-454103e5d752` produced three completed
Stops but answered with a plan asking for confirmation instead of a final
sentinel. The runtime correctly terminalized it as `indeterminate` with reason
`frontier_sentinel_missing` and retained the lease. After the pane was visibly
idle, explicit abandonment released that lease. The next run on the same
session then succeeded. This reproduces the old multi-Stop path without
accepting an incomplete answer.

Each successful OpenCode turn also emitted three completed Stops. Only the
third carried the final structured marker and became the ledger completion
event.

## Open lanes

- `BLOCKED` and `FAILED` mappings were test-locked but not intentionally
  induced against paid providers. Close by sending one explicit live prompt per
  status and checking the exact exit codes.
- Provider/model mismatch was test-locked but not induced live because it would
  require changing the interactive model. Close in a disposable fleet by
  switching models before dispatch and confirming retained `indeterminate`.
- Cross-boot audit recovery and corrupt/gapped event streams were exercised by
  unit tests, not by restarting cmux during a paid turn. Close with a disposable
  cmux restart drill.
- A tool-heavy live response was not dispatched. Historical OpenCode database
  rows with multi-kilobyte tool sessions were read successfully, while the
  corresponding export was invalid; close with a bounded read-only tool task
  carrying the fleet sentinel.

## Cleanup

`fleet-down.sh slice2c-opencode-live` closed `workspace:23`. No Slice 2C fleet
leases or manifest remain. The durable ledger remains under
`orchestration/runs/fleet-slice2c-opencode-live.ledger.jsonl` for audit.
