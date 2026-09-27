# Agent Fleet project instructions

Apply these instructions within this repository. Respond in Spanish; preserve
code, commands and identifiers. Preserve existing work and keep other projects'
operational procedures out of this repository.

## Work and authority

- Establish the objective, evidence, constraints and observable success criteria for substantive work. Review requests are read-only; implement when authorized.
- Continue authorized work through relevant verification and delivery. Resolve reversible technical decisions within scope, continue independent work through blockers, and report concrete unresolved dependencies.
- Do not create agents, tasks or live provider campaigns without explicit authorization. Only the registered Worker writes a Mission candidate; Lead, Reviewer and Verifier are readers. Local maintenance may use one explicitly assigned implementer.
- Follow the task's authority and role contract. Commit, push, deploy, spending, messages, global settings and unrelated resources require explicit authorization for that action and scope. Tools, writable paths and skills grant no permission.
- Git history authorized in an isolated maintenance fixture does not authorize repository commits. Reuse existing authorization without repeated approvals after reviews or checks.

Herdr is the project's orchestration lane. CMUX has no active project skill;
its procedures remain in docs/cmux-legacy-reference.md for explicitly selected
legacy work. Preserve historical CMUX readers and legacy compatibility; never
introduce CMUX fallback into Herdr.

## Orchestration environment

Herdr manages sessions and panes; the selected CLI executes the agent; the
provider/model is a separate identity. Keep CLI versions and startup rules in
the runtime contract, not repeated in user requests or copied into role skills.
The controller binds the session, agent, working directory and permissions to
each run. Use the supplied task and bound runtime evidence; do not infer these
values from pane names or ask the user to repeat information already available.
For interactive pane control, verify `HERDR_ENV=1` and the exact target before
using the Herdr skill. Being inside Herdr does not authorize agent control.
An ad hoc CLI session is not a registered Mission role without its admission.

## Stages and acceptance

Herdr stages depend on the profile bound to the task. Judge only the
assigned stage's supplied criteria: a failing baseline can support Plan PASS.
Do not wait for future stages to report the current stage's result.

Stage PASS, transport completion, artifact acceptance and Mission success are
distinct. Closure depends on the controller ledger, immutable CAS evidence,
frozen candidate, independent archive verification and configured acceptance
contracts. Screenshots, exit zero, model reports and green tests alone do not
prove semantic success. Report verified facts, inferences and missing evidence.

Artifact predicates validate pinned files; the optional functional contract
separately checks the frozen tree. The Python stats profile supports a limited
source subset and fixed RPC/test protocol, not general hostile Python. Distinguish
OS isolation, runtime permission attestation, source acceptance and behavior.

## Lifecycle and recovery

Use explicit Mission, runs directory and Herdr session identities. Supervision
is a bounded foreground observer of one Mission. Persist pause/cancel requests
independently of the driver lock; requested pause is not confirmed pause.
Resume reuses durable evidence and original deadlines. Cancel only exact owned
run/generation resources, accounting for results arriving before cancellation.
Confirm quiescence and functional container cleanup before closure. After an
ambiguous send, reconcile the same run; do not blindly resend or promise
exactly-once delivery. Terminal historical records remain read-only.

## Instruction transfer

Use one shared project instruction hierarchy, not an AGENTS.md per agent
identity. The controller snapshots applicable tracked target instructions from
the candidate baseline and supplies their content, scope and hashes to all
roles. Role contracts and selected skills travel explicitly with the task.
Do not assume global maintainer instructions, skills, the coordinator's
conversation or another role's instructions are inherited; do not copy
maintainer globals into the candidate.

Roles choose their methods within the sole-writer rule and closure boundary.
Skill references do not provision tools or authorize delegation. Preserve the
exact result protocol; when raw JSON is required, put citations inside its summary. Model/effort
identity comes from the bound transcript, not configuration or a role's guess;
unavailable identity alone does not block a stage.

## Verification and references

Use existing local dependencies and provider-free fixtures. For maintenance,
run relevant `python3 -B -m unittest` modules when behavior needs testing, and
`git diff --check` for edits. Docker tests require the explicit existing local
test lane. Testing does not implicitly authorize live Herdr/Codex campaigns,
software installation, image pulls or broader cleanup. Real model quality and
billed cost remain NOT_VERIFIED without the corresponding evidence.

Read only the documentation relevant to the task:

- [Mission control](docs/herdr-mission-control.md): stage orchestration, task contracts and closure.
- [Functional checks](docs/herdr-functional-checks.md): frozen-tree execution, Python subset and isolation limits.
- [Supervision and measurement](docs/herdr-supervision-and-measurement.md): pause/resume/cancel, recovery and measurement semantics.
- [Instruction scope](docs/herdr-instruction-scope.md): instruction discovery, role skills, CAS snapshots and transfer.
