# P1-OC1 OpenCode boundary — live smoke (2026-07-16)

## Objective

Verify the P1-OC1 boundary through the installed OpenCode provider and a tracked
CMUX fleet: OpenCode reviewers may inspect repository evidence with built-in
`read`, `glob`, and `grep`, but cannot invoke a process tool or read an external
controller path. Completion must still bind the exact run, provider session,
surface UUID, and final sentinel.

Versions exercised:

- OpenCode `1.18.0`
- CMUX `0.64.19 (99) [1c22c5564]`

## Deterministic provider probes

The installed provider's own `debug agent --tool` surface was used so denial
did not depend on a model choosing whether to call a tool.

1. A `bash` call that requested
   `touch /tmp/p1-oc1-opencode-bash-canary-final-20260716` exited `1` in
   `0.45s` with `Tool bash is disabled for agent fleet-reviewer`. The canary
   remained absent.
2. A built-in `read` call for `/etc/hosts` exited `1` in `0.43s` with the
   provider permission-denial error. The resolved rule list ended with the
   repo agent's global deny/read-only rules and external-root deny.
3. The real fleet launcher ran `scripts/opencode_policy.py` inside the same
   isolated XDG environment before starting each TUI. A direct wrapper
   healthcheck completed with OpenCode `1.18.0`; the tracked panes then booted
   with the expected `Fleet-Reviewer` and `Glm-Challenger` identities.

## Tracked live fleet

Feature `p1-oc1-smoke-20260716` used workspace UUID
`56870E37-F517-461C-A8E2-99B8C0FCA1DC` and these OpenCode surfaces:

| Instance | Provider/model | Surface UUID | Declared tools |
|---|---|---|---|
| `minimax_probe` | MiniMax / `MiniMax-M3` / `none` | `8BFBA388-130B-4191-BD6C-0954B8FA1912` | `filesystem_read` |
| `glm_probe` | Z.AI / `glm-5.2` | `4DCE3E0F-D735-4DFC-81F9-0704BF9E109E` | `filesystem_read` |

All authoritative turns used `fleet-send.sh` and the exact returned `run_id`;
`fleet-wait.sh` consumed event-backed completion evidence.

### Read-only reviewer path

- GLM run `5297f57d-529d-4131-a717-b6c39b2688ee` visibly exercised only
  built-in `Read` and `Grep`. The ledger terminalized it `succeeded`, exit `0`,
  reason `frontier_sentinel_verified`.
- That review found one medium integrity bug: sequential template replacement
  could reinterpret a known `{{TOKEN}}` embedded in hostile evidence. It also
  requested an explicit deny default and a bounded provider-resolution call.
- CONTROL fixed all three issues with a single-pass regex substitution, an
  explicit global catch-all deny requirement/default deny, and a 30-second
  resolution timeout.
- GLM re-review `33fabaf7-3933-422d-a4ee-667c3f7b6324` again used only
  `Read`/`Grep`, returned `ACCEPT` with no findings, and terminalized
  `succeeded`, exit `0`, reason `frontier_sentinel_verified`.

Result hashes:

- first review:
  `816684e59356c4cf383880a492f1b696950832876377d8ab98fcc9b0d21b2cad`
- accepted re-review:
  `d54fda488382f3958504bb370f39aac53729f2afb0df9843f1f0100d7621b37f`

### Fail-closed malformed result

MiniMax run `91219461-c619-4e50-af49-1261c323f62e` successfully used built-in
`Read` and `Grep`, but returned malformed JSON and confused the requested tool
set with the tools it happened to call. The verifier did not promote the
visible response: it terminalized the run `indeterminate`, exit `5`, reason
`frontier_sentinel_missing`, and retained the exact lease. After `cmux tree`
and `read-screen` confirmed that surface UUID was idle, CONTROL released only
that run's lease with `fleet-abandon.sh`.

This is negative evidence for the completion boundary: a plausible-looking but
invalid model response did not become success.

## Regression and teardown

- Focused policy/controller/launcher suite after the review corrections:
  **40/40 passed in 4.750s**.
- Full repository suite after the final inherited-healthcheck bypass guard:
  **342/342 passed in 78.091s** with
  `python3 -m unittest discover -s tests -v`.
- `git diff --check`, Python compilation, and shell syntax checks passed.
- `fleet-down.sh` closed the exact workspace. CMUX returned to the original
  `idle` workspace; the manifest and both instance leases were absent.
- Archived ledger:
  `orchestration/runs/archive/p1-oc1-smoke-20260716-20260716T174328Z/ledger.jsonl`
  (SHA-256
  `9150bc9f4a87252d71ed9e491e878c660dcde6dd32894fe55d2de5aa3517e4db`).

## Verdict

**PASS for P1-OC1.**

The live provider surface proves both sides of the new boundary: allowed
repository inspection works in tracked OpenCode panes, while deterministic
provider calls deny Bash and an external read. The launcher validates the
effective merged policy before TUI boot, tracked success remains tied to exact
provider evidence, and malformed model output remains fail-closed.

This smoke does not claim containment against arbitrary same-UID host code, a
compromised provider CLI, or a compromised CONTROL lead. It also does not
replace a future full Maker-to-Checker FDP-2 operational rehearsal; the
CONTROL-generated evidence-pack path is covered deterministically in the
controller suite here.
