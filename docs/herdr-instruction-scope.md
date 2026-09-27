# Instruction discovery and transfer

The project root `AGENTS.md` is the maintainer contract for this repository.
There were no existing tracked project `AGENTS.md` files at this change's
baseline. It is not installed into another repository or the user's global
Codex home. Historical CMUX workflows keep their own launch path.

## Orchestration and CLI context

Declare Herdr as the project's orchestration layer once in `AGENTS.md`.
Herdr manages sessions and panes; a CLI such as Codex or OpenCode executes an
agent, and its provider/model is a separate identity. Users do not need to
repeat that environment in each request. A direct OpenCode/DeepSeek session
inside Herdr does not become a registered Mission role or gain closure authority.

Keep each value in its existing authoritative location:

| Context | Source and consumer |
| --- | --- |
| Mission, run, role and stage | The task's `mission_id`, `run_id`, `instance_id`, `stage` and `writer` identify the assignment. |
| Candidate and result scope | The task's `candidate_repo`, `frozen_candidate` and `result_contract` define the role's work and output. Capsule tasks also supply `capsule_context`; host paths there identify resources, not accessible directories. |
| Herdr session and agent binding | The controller's backend state and run/submission binding identify the exact resources. Roles do not gain permission to control them. |
| CLI versions and startup behavior | The selected runtime contract is checked by the backend; see [CLI versions](herdr-cli-versions.md). Do not duplicate version numbers in role guidance. |
| Effective permissions and model identity | Bound execution evidence and transcript verification support these claims; configured labels, pane titles and environment declarations alone do not. |

Shared `role_guidance.common.instruction_scopes` carries this distinction to
each role, even when the target repository's instructions do not mention Herdr.
It adds guidance text to the existing bundle, without a new task or runtime
schema. New Missions freeze that guidance at Plan; existing Missions keep their
original CAS bundle during recovery. No environment dump or credential values
are added to task packets. The role uses supplied context and reports material
missing task information; it does not need to self-attest its model identity.

## Explicit role autonomy and skills

New task packets include `role_guidance`, separate from `project_instructions`.
One project AGENTS hierarchy defines directory rules; the task identifies the
role. No global maintainer AGENTS or skill directory is implicitly inherited.

Each role chooses its methods, investigates relevant evidence, resolves
reversible technical decisions, tries bounded alternatives and finishes its own
stage without waiting for step-by-step human instructions. It continues
independent work when blocked and returns a precise dependency and recommendation.
This does not change the sole writer, effect permissions, delegation policy or
controller closure authority. A skill never grants permission or overrides the
task's raw JSON result protocol.

| Role | Selected skills and conditions |
| --- | --- |
| Lead | `codex-os`; `entrevista-pre-slice` only for unresolved material user choices; `slice-gate` for synthesis. |
| Research | `fase-0-recon`; `deep-research` only for broad investigations. |
| Worker | `fase-0-recon` for unfamiliar code; `smoke-verify`; `impl-notes` for material deviations. |
| Reviewer | `fase-0-recon`; `slice-gate` for its review criteria. |
| Verifier | `smoke-verify`; `slice-gate` for its verification criteria. |

The seven skill sources are local copies in `orchestration/role-skills`, not
changes to installed global skills. `fleet_herdr_role_guidance.py` snapshots the
contracts, shared rules and full skill content into CAS. Each task receives its
selected content, use conditions, individual hashes and the bundle artifact ID.
Later stages and recovery reuse the first Plan's bundle, even when installed
skills or candidate instructions change. Missing, aliased or oversized sources
fail closed when creating a bundle. Historical tasks without guidance retain
their previous behavior; they are not silently upgraded.

References within skills do not provision more skills, tools or subagents.
In particular `deep-research` metadata `context: fork` / `agent: Explore` does
not start a runtime agent, and other projects' operational references do not
apply here. Specialized skills require an explicit future selection.

The default profile retains four roles and five stages. Research is active only
in the explicit `astra_sol_research_v1` profile, with five roles and six stages;
its profile, input and acceptance contracts are versioned separately.
The native execution admission block remains in place. Verified behavior is
instruction delivery and recovery with provider-free fixtures, not autonomous
model behavior, skill obedience or runtime effect containment.

Role criteria are deliberately different: Research answers material uncertainty
with pinned sources and negative cases; Reviewer examines the complete change for
actionable defects and may report none; Verifier reproduces acceptance/evidence
on the same frozen tree; Synthesis reconciles discrepancies only when selected.
The minimal profile selects only Build from creation. Research-profile Build tasks
created through the official entry also carry `input_evidence` under
`bounded-cas-v1`: useful summaries plus bounded CAS detail references, never all
transcripts or a global catalog. Worker summaries must identify findings used or
discarded. This is auditability of delivery rather than proof of obedience.

Focused verification (reuses local Git history; creates no commits):

```sh
python3 -B -m unittest tests.test_fleet_herdr_role_guidance
git diff --check
```

| Surface | Transfer and authority |
| --- | --- |
| Local maintainer | Root project AGENTS plus the current user's authorized task. |
| `mission-run.py` → Herdr driver | Task contract contains role, stage criteria, permission boundary, output protocol and a target instruction packet. |
| Candidate | A clean, independent clone at the durable baseline retains target files; no synthetic AGENTS is injected over them. |
| Five Herdr turns | `project_instructions` contains the same CAS snapshot ID and actual baseline instruction content, SHA-256 and directory scope. The whole task is also in CAS and bound to its run. |
| Herdr CLI launch | Explicit candidate cwd, configured model/effort and role permission flags; uses the selected Codex home. This change neither replaces that home nor proves automatic global instruction discovery. |
| Historical interactive Codex launcher | `fleet_codex_home.py` provisions/verifies its existing ephemeral home with a small role contract. It does not copy the maintainer's global AGENTS or skills. Existing auth binding behavior is unchanged. |
| Historical Kimi readers | `fleet-up.sh` copies `orchestration/prompts/kimi_reviewer_agents.md` into its own reader clone before sealing; `run-kimi-reviewer.sh` supplies `KIMI_AGENTS_MD` for the standalone reader. These existing, explicitly selected routes are preserved and are not used by Herdr. |

`fleet_herdr_instructions.py` discovers tracked `AGENTS.md` and
`AGENTS.override.md` blobs from the exact Git baseline. Within each directory,
override replaces AGENTS. Root scope applies throughout the candidate; nested
scope refines it only within that directory. All selected scopes are supplied,
so a role need not guess whether automatic CLI nested discovery occurred.
The controller packet does not claim to reproduce every Codex configuration
option (custom fallback names, ancestor/global files or automatic truncation).
Those are outside this explicit, bounded transfer protocol.

Only regular UTF-8 instruction files are supported: at most 32 selected scopes,
32 KiB per file and 64 KiB total. Symlinks and excess size fail before role boot;
content is never silently truncated. The clean-baseline requirement excludes
untracked target instructions. The first Plan task anchors the snapshot in CAS;
later turns and recovery reuse it, including when Worker changes instruction
files as an authorized deliverable. Such edits do not grant new Mission
authority. Historical tasks lacking a packet retain their original contract.

Provider-free fixtures call the actual `mission-run.py` entry point with a
simulated backend, create/clone synthetic Git repositories, inspect all five
submitted task payloads and verify their CAS bytes. They also test nested and
override scopes, rejected linked/oversized files, baseline preservation and
recovery after a worker edit. Existing backend tests check CLI cwd/arguments;
ephemeral-home tests execute provisioning and verify its on-disk contract.
These prove file discovery and transfer, not real-model obedience or automatic
global-skill inheritance. A provider campaign remains separately authorized.

## Task-inline skills in new Research runtimes

New `astra_sol_research_v1` backends bind `skill_delivery=task-inline-skills-v1`
in the runtime contract. The official CLI remains the executor. Before startup,
its local `codex debug prompt-input` preview inventories the available skill names.
The controller freezes a CAS manifest of disabled host names and uses per-launch
`skills.config` overrides plus `features.skill_search=false`. A second preview
must expose no enabled skills; unknown preview formats and newly enabled names
fail before startup or dispatch. No global Codex configuration is edited.

Skills for the Mission are the existing inline `role_guidance` projection:
name, source path, content hash, full content, role and condition, bound through
the CAS bundle and exact task hash. Collection and offline archive verification
check that projection against the frozen bundle. A same-named installed skill is
not interchangeable with this source. References such as `$commit` inside a
procedure do not authorize loading it. No extra selected-skill message is accepted,
even if its metadata claims runtime origin or its name is selected for the role.

CLI runtime instructions (permissions, environment and built-in operation) remain
separate from Mission instructions. This mechanism controls skill selection; it
does not replace the entire CLI system/developer context, attest model obedience,
or provide OS isolation. Existing backends keep their original runtime contract
and launch behavior; they are never silently upgraded on recovery. The legacy
four-member profile and experimental launcher are unchanged.

A completed turn rejected by context/permission verification retains its final,
transcript and observed envelope in CAS. `herdr-evidence-rejection-<run>.json`
points to an immutable `herdr_execution_evidence_rejection` receipt, distinct from
an accepted result and from a role-protocol rejection. Recovery verifies the
runtime anchor, Mission/run/admission/session/task bindings and reproduces the
rejection offline. It does not read a replacement live transcript or resend the
task. Research and Build cannot follow a rejected Plan.

A completed prompt-bound turn whose final cannot be a role result (not JSON,
not an object, copied identities or `candidate_tree_sha` that differ, or a
backend-reserved field) is retained as `herdr_delivery_rejection` through
`herdr-delivery-rejection-<run>.json`. The observed envelope carries only
controller-derived identity plus the exact final and transcript CAS; the
receipt also records whether the turn's execution evidence verified. Recovery
reproduces both the content reason and that execution outcome offline. It never
becomes a verdict and follows the same block and exact-cancel rules below; the
driver reports `unbound final delivery rejected`. Previously such a final left
the run unresolved, and neither cancellation nor the deadline could close it.

A rejection is not evidence of quiescence. If the admission is still active,
recovery reports an explicit pause block while retaining `pause_requested` and
its unapplied request. It does not report `paused`, cancel implicitly on expiry,
or extend the deadline. Explicit cancellation must pass the existing generation
and run-scope guards before reconciling the exact execution. A rejected frozen
task cannot be changed or replayed in place. Continuing the feature after such
an incident requires authorized closure of that execution and a separately
authorized corrected Mission; historical evidence remains readable.

Provider-free regression: `python3 -B -m unittest tests.test_fleet_herdr_skill_context
tests.test_fleet_herdr_delivery_rejection`.
This exercises authorized inline delivery, nine-message rejection, scope/content
mutation, CAS recovery, pause/deadline/cancel boundaries and the six-stage archive
using synthetic CLI output. Local preview evidence is distinct from a live model
campaign, which requires separate authorization.
