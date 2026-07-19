# Fleet threat model

This document defines the S0 security boundary for the local CMUX fleet: one
trusted Mac, one trusted Unix UID, and one local runs root. It is a contract
about which adversary the runtime is designed to contain; it is not a claim
that an LLM answer is semantically correct or that the host is multi-tenant
safe.

## Selected boundary: model-injection containment

The selected boundary is **A: the model and every input it reads are
untrusted; the provider CLI, the local Unix account, and CONTROL are trusted**.

Untrusted inputs include the mission objective, prompts, repository files,
diffs, webpages copied into evidence packs, tool output, and messages produced
by another model. An injected model may ignore instructions, lie about its
work, emit a false `DONE`, or attempt to use every tool exposed to its role.

Boundary A requires the runtime to contain those attempts with deterministic
controls outside the model:

- provider sandbox and permission configuration;
- role authority and tool allowlists;
- one-writer and phase gates;
- exact `run_id`, workspace, surface, provider, model, and transcript binding;
- local-worker leases and budgets on the tracked dispatch path;
- deterministic tests and separately executed, identity-bound verification
  before accepting claims.

Completion provenance proves which configured run produced a final response.
It does **not** prove that the response is correct. A valid transcript that
contains a matching sentinel can still contain a wrong conclusion.

Current conformance is **partial**, not complete:

| Provider path | Current evidence | Status |
|---|---|---|
| Codex advisory | Router pins `read-only` plus `--ask-for-approval never`; the direct canary attempted the requested operation, the provider returned `Operation not permitted`, and the sentinel remained absent. | Declared, statically locked, and live-verified fail-closed. |
| Claude reviewer | Minimal allowlist and fail-closed sandbox are locked; the direct `stream-json` canary emitted a real `Bash` tool request followed by a denied tool result, and the sentinel remained absent. | Declared, statically locked, and live-verified fail-closed. |
| OpenCode reviewers | P1-OC1 defaults every tool to deny, enables only built-in `read`/`glob`/`grep`, denies external roots except a fresh isolated tool-output directory, and validates the resolved provider policy before TUI boot. FDP-2 receives CONTROL-generated Git evidence instead of shell access. | Implemented, test-locked, and live-verified in `orchestration/smoke-evidence/p1-oc1-opencode-boundary-20260716.md`. |

Those provider canaries are dated evidence from before the final integrated S0
smoke. In particular, the Claude MCP attempt connected and discovered tools but
did not complete the requested envelope; it reported USD 0.56816 despite a
USD 0.05 CLI cap. Paid inference is therefore opt-in evidence, not part of the
automatic local acceptance lane.

## Trusted computing base

The following components are trusted under boundary A:

- the macOS host and the `hector` Unix account;
- the installed Codex, Claude, OpenCode, Ollama, and CMUX executables;
- repository-owned launch wrappers, hooks, ledgers, and controller code;
- the CONTROL lead and commands deliberately run by the human operator;
- provider authentication and the provider APIs returning the declared model.

Compromise of one of these components is a different threat model and must not
be described as covered by this boundary.

## Assets and required properties

| Asset | Required property under boundary A |
|---|---|
| Target repository and isolated writer clone | Only the declared writer may mutate its private clone; only CONTROL may publish the quiescent commit into the target repository. |
| CONTROL plane and CMUX surfaces | Non-CONTROL model tools cannot redirect another pane or claim tracked completion through raw input. |
| Hook maps and provider transcripts | Model tools cannot edit controller evidence; completion reads the exact source-specific record. |
| Fleet and Mission ledgers | Supported wrappers append durably and reject ambiguous or later terminal states. |
| Compiled workflow and router | Effect consumers use the complete digest-bound router snapshot embedded in compiled v2; later edits to the live router cannot change the admitted plan. |
| Mission admission | Deadline, delegation credits, active slots, writer ownership, run identity, and token policy are frozen; every current launch is exact-bound through `reserved → committed → authorized → started → finalized`. |
| Credentials and controller dotfiles | Agent tool environments do not inherit broad credentials and provider sandboxes deny controller-private roots. |
| Final decision | A model result remains advisory until deterministic evidence or the applicable verification gate accepts it. |

The admission state machine separates authority from observation.
`effect_sha256` binds the complete requested effect and `task_sha256` binds the
submitted logical task before launch authorization. The authorization event is
the deadline-linearization point; only its exact proof can become `started`.
Finalization requires a closed schema-version-1 terminal envelope bound to the
admission's source event, run, task, status, recipient, and writer ownership
before claims are released. Its `source_event_sha256` remains an attestation
made by trusted CONTROL inside S0, not an independently controlled receipt.

The main and assurance admission lanes are disjoint. `CONTROL` owns the main
lane and `ASSURED` owns the assured lane; neither actor may mutate the other's
admissions. The assurance request cannot transition state until every main
admission is inactive. The idempotent handoff stops the main control generation
and preserves its runtime below
`missions/<mission_id>/assurance-handoff/main-runtime/`; the assured service
uses a separate `control-assured/` generation. A descriptor-rooted quiescing
marker cuts off new cooperative writers before the mutation barrier drains
in-flight writers with a deadline; rejected mutations are not queued. This is a
correctness/liveness guarantee inside trusted S0, not containment against a
hostile same-UID process.

## P1-OC1 OpenCode containment

The previous policy tried to approximate read-only Bash with broad patterns.
That was not enforceable: `find -exec`, `sed -i`, shell redirection, and future
commands could turn an allowed prefix into a mutation or CMUX invocation.

P1-OC1 removes model-driven process execution instead of extending that
allowlist. Every OpenCode reviewer now starts from `"*": deny`, enables only
the provider's built-in `read`, `glob`, and `grep` tools, denies external roots
except OpenCode's exact isolated `tool-output` directory, and keeps `.env` reads
denied. Controller tool-output history is removed from the copied provider
state. `run-interactive-agent.sh` resolves
the merged OpenCode configuration with `opencode debug agent --pure` and aborts
before TUI boot unless the effective tool set and permission probes match the
repo contract exactly.

FDP-2 no longer asks its Checker to execute `fleet_dialogue.py` or Git. CONTROL
reuses its existing Git validation and durable prompt writer to embed a bounded
evidence pack containing the exact dialogue envelope, parsed source result,
base/head/branch identity, clean worktree state, commit metadata, name-status,
and a no-ext-diff/no-textconv patch. The prompt hash binds that pack into the
existing control ledger and offline archive.

This closes the static/runtime configuration gap. The acceptance smoke is
recorded in
`orchestration/smoke-evidence/p1-oc1-opencode-boundary-20260716.md`: the
installed provider deterministically denied Bash and an external read, while a
tracked GLM pane completed a built-in Read/Grep review and accepted the fixes
after finding one real template-integrity defect.

## P1-IND1 identity diversity

The runtime uses **identity diversity**, not “independence”, for the property it
can prove. A declared group is valid only when every member has a different
configured `(provider, model, variant)` tuple. `router_config.py` rejects a
duplicate identity in the default race or any preset `identity_groups` before
CMUX effects. The resolved groups are copied into plan output, workflow
compilations, and fleet manifests beside the exact member identities.

This contract deliberately permits same-identity partitions outside declared
groups, including custom races and the two bounded triage partitions in the
`research` preset. A custom race still prints only `FIRST CANDIDATE (NOT
VERIFIED)`. Distinct tuples make corroboration auditable; they do not measure
training lineage, uncorrelated errors, or semantic truth.

Live evidence is recorded in
`orchestration/smoke-evidence/p1-ind1-identity-groups-20260716.md`: a duplicate
default-race identity failed before any CMUX effect, a valid three-identity
group survived into the live manifest, and two tracked identity-distinct
reviews completed successfully. One reviewer also emitted a disproven factual
claim, demonstrating why tuple diversity is not semantic verification.

## P1-HITL1A approval provenance

Mission-bound assured fleets do not accept a free-form `--approved-by` string
for BUILD exit. They require the exact current `event_sha256` from
`assurance_approved` or `assurance_approval_renewed`; `fleet_state.py`
re-derives the Mission chain and validates assured-running state, feature,
target scope, and expiry before recording the reference.
`fleet_assured_runner.py` propagates that event hash, and FDP-3 revalidates it
at start. A foreign, stale, expired, or label-only approval fails closed.

Approval is rechecked at assurance boot/start and at each ASSURED reservation,
commit, and launch authorization; authorization binds its exact event hash.
Renewal is a distinct HUMAN event after the prior approval expires. It cannot
widen the request, workflow, target scope, or risk, and is allowed either before
assurance boot or from a quiescent `assured_running` lane with no run claims,
active admissions, or writer claim. Already-authorized work may finish after
expiry, but new effects fail closed. The operator command is
`fleet-approve.py ... --renew`; the canonical guide describes the recovery
sequence.

Standalone guided fleets retain `--approved-by` as a compatibility attestation
and cannot claim Mission-bound provenance. Neither path proves out-of-band
human presence: `fleet-approve.py` and CONTROL still execute under the same
local Unix account, and the approver hash is derived from `$USER`.

Live evidence is recorded in
`orchestration/smoke-evidence/p1-hitl1a-approval-provenance-20260716.md`: the
real phase CLI rejected a free-form label and a foreign event without state or
CMUX effects, accepted the exact active event, and the FDP-3 preflight
revalidated it. Codex and MiniMax then accepted the diff in tracked,
identity-distinct reviews.

## Explicit non-goals

The following are **OUT OF SCOPE** for boundary A:

1. **Arbitrary code already running as the same Unix UID.** File mode `0600`
   separates other users, not another `hector` process. Such a process can
   rewrite ordinary hook maps, transcripts, manifests, and unsigned ledgers.
2. **A compromised provider CLI or CMUX binary.** Provider sandboxes constrain
   model tools; they are not a containment boundary around a malicious host
   executable.
3. **A compromised CONTROL lead.** CONTROL intentionally has shell and CMUX
   authority and can invoke direct convenience entrypoints. Boundary A treats
   it as part of the trusted computing base.
4. **Cryptographic human presence.** A scoped approval binds request, scope,
   risk, and expiry, but the local `$USER` value is not out-of-band proof that a
   distinct person approved it.
5. **Distributed or multi-host coordination.** Local `flock` and filesystem
   leases require one shared `FLEET_RUNS_DIR` on one host.
6. **Semantic truth from agreement.** Two matching model answers, including
   answers from distinct providers, are evidence to investigate, not proof.
7. **External WORM custody or regulatory compliance.** Local signed receipts
   and loopback Object Lock are useful development evidence but are controlled
   by the same machine owner. The current provider set also lacks `hard_total`,
   so `regulated.yaml` cannot admit effects honestly.
8. **Independent source-event attestation.** Structured terminal evidence binds
   the hash asserted by trusted CONTROL. A compromised CONTROL process can lie
   about that source, which is already outside boundary A.
9. **Authority-preserving migration of an active legacy admission ledger.**
   Historical formats remain readable, but the runtime does not invent current
   effect/task/authorization provenance for an already-active legacy Mission.

If resistance to same-UID or compromised-CONTROL attacks becomes required,
the next design must use a separate OS principal or effect broker and an
out-of-process verifier. Adding more prompt instructions or file hashes would
not establish that boundary.

## Adversarial scenarios

| # | Scenario | Expected result under boundary A | Automated evidence |
|---|---|---|---|
| 1 | Repository content asks a model to ignore policy and perform an unauthorized effect. | Required: deterministic role controls bound effects. Current: OpenCode has no process tool or external path tool surface; Codex/Claude retain their separately documented controls. | `test_prompt_injection_is_bounded_by_role_controls`, `test_opencode_reviewers_default_deny_process_and_external_access`, and the resolved-policy tests |
| 2 | Claude or OpenCode tries to write controller evidence or invoke `cmux`. | Claude has no automatic permission. OpenCode cannot call Bash or external paths; its launcher rejects a resolved policy that re-enables either. The P1-OC1 provider smoke directly denied Bash and an external read. | `test_claude_policy_does_not_auto_allow_cmux_or_mutation`, `test_opencode_reviewers_default_deny_process_and_external_access`, and `p1-oc1-opencode-boundary-20260716.md` |
| 3 | An arbitrary process under the same UID rewrites a mode-0600 evidence file. | The write succeeds; this is an explicit boundary demonstration, not a protected case. | `test_same_uid_process_can_rewrite_private_file_and_is_out_of_scope` |
| 4 | CONTROL calls a direct worker entrypoint and bypasses tracked dispatch. | The path exists and is trusted/operator-only; it must never be described as enforced against CONTROL. | `test_control_is_trusted_and_can_reach_direct_entrypoints` |
| 5 | A custom race uses the same role/model twice. | It remains permitted but is only an unverified candidate. Default race identities and declared preset groups fail closed on duplicate `provider/model/variant` tuples. | `test_same_model_custom_race_is_permitted_but_never_claims_assurance` and the router identity-group tests |
| 6 | The waiter process is suspended beyond its alarm deadline. | `fleet_wait.py` uses POSIX `alarm`; a separate primitive probe confirms the signal becomes pending across process stop/resume. The actual waiter path and full Mac sleep/wake remain open live lanes. | `test_wait_uses_posix_alarm_and_alarm_is_pending_after_process_stop` |
| 7 | Local run A finishes and run B reuses its CMUX surface while a late cancel targets A. | Fleet Control refuses local cancellation because no exact run-scoped process handle exists; it never sends pane-level `ctrl-c` that could stop B. | `test_local_cancel_rejects_without_signalling_reused_surface` |

## Operator rules

- Treat repository and supplied evidence as hostile instructions.
- Do not approve a permission request merely because an agent says it is
  necessary; compare it with the declared role first.
- Use tracked wrappers for authoritative work. Direct `cmux send` and
  `run-local-worker.sh` are observational/convenience paths, not provenance.
- Supply OpenCode reviewers with CONTROL-generated evidence; do not restore a
  Bash allowlist to make an ad-hoc review more convenient.
- Never promote a race winner or cross-model agreement without deterministic
  verification appropriate to the task.
- Report a same-UID, provider-CLI, or CONTROL compromise as a boundary change,
  not as a bug supposedly contained by this threat model.
