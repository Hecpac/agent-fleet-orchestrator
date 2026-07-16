# Fleet threat model

This document defines the security boundary for the local CMUX fleet. It is a
contract about which adversary the runtime is designed to contain; it is not a
claim that an LLM answer is semantically correct.

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
- deterministic tests and independent verification before accepting claims.

Completion provenance proves which configured run produced a final response.
It does **not** prove that the response is correct. A valid transcript that
contains a matching sentinel can still contain a wrong conclusion.

Current conformance is **partial**, not complete:

| Provider path | Current evidence | Status |
|---|---|---|
| Codex advisory | Router pins `read-only` plus `--ask-for-approval never`; live canaries remained absent, but Codex narrated `BLOCKED` without a visible tool event. | Declared and statically locked; direct denial unverified. |
| Claude reviewer | Plan mode, minimal allowlist, and fail-closed sandbox are locked; the live probe made no tool call and provider drift terminalized `indeterminate`. | Safe no-effect and identity fail-closed; direct denial unverified. |
| OpenCode reviewers | `edit: deny` and default Bash deny exist, but broad allowed command globs admit mutating command forms. | **P1 OPEN — does not yet satisfy boundary A.** |

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
| Target repository and writer worktree | Only the declared writer may mutate it during its active phase. |
| CONTROL plane and CMUX surfaces | Non-CONTROL model tools cannot redirect another pane or claim tracked completion through raw input. |
| Hook maps and provider transcripts | Model tools cannot edit controller evidence; completion reads the exact source-specific record. |
| Fleet and Mission ledgers | Supported wrappers append durably and reject ambiguous or later terminal states. |
| Credentials and controller dotfiles | Agent tool environments do not inherit broad credentials and provider sandboxes deny controller-private roots. |
| Final decision | A model result remains advisory until deterministic evidence or the applicable verification gate accepts it. |

## Known in-scope control gaps

The OpenCode agent policies currently allow broad Bash patterns including
`find *` and `sed *`. A deny-by-default rule does not make these patterns
read-only: `find -delete`, `find -exec ...`, `find -fprint ...`, and `sed -i`
can mutate files, while `find -exec` can launch another executable such as
CMUX. The threat-model test detects and documents these patterns; it does not
claim they are safe.

This is a **P1 OPEN** remediation lane. Until those commands are replaced with
an argument-aware policy or removed, OpenCode's prose label “read-only” is an
intent, not an enforced model-injection boundary.

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

If resistance to same-UID or compromised-CONTROL attacks becomes required,
the next design must use a separate OS principal or effect broker and an
out-of-process verifier. Adding more prompt instructions or file hashes would
not establish that boundary.

## Adversarial scenarios

| # | Scenario | Expected result under boundary A | Automated evidence |
|---|---|---|---|
| 1 | Repository content asks a model to ignore policy and perform an unauthorized effect. | Required: deterministic role controls bound effects. Current: Codex/Claude are declared narrowly; OpenCode remains non-conformant because of broad Bash globs. | `test_prompt_injection_is_bounded_by_role_controls` and the provider-policy tests |
| 2 | Claude or OpenCode tries to write controller evidence or invoke `cmux`. | Claude has no automatic permission; OpenCode has no explicit CMUX rule but `find -exec` is a known bypass candidate. Live canaries remained unchanged without direct tool events. | `test_claude_policy_does_not_auto_allow_cmux_or_mutation`, `test_opencode_broad_allowlist_is_detected_as_an_in_scope_gap`, and live smoke |
| 3 | An arbitrary process under the same UID rewrites a mode-0600 evidence file. | The write succeeds; this is an explicit boundary demonstration, not a protected case. | `test_same_uid_process_can_rewrite_private_file_and_is_out_of_scope` |
| 4 | CONTROL calls a direct worker entrypoint and bypasses tracked dispatch. | The path exists and is trusted/operator-only; it must never be described as enforced against CONTROL. | `test_control_is_trusted_and_can_reach_direct_entrypoints` |
| 5 | A custom race uses the same role/model twice. | It is permitted but cannot be reported as independent confirmation; the default race remains heterogeneous. | `test_same_model_custom_race_is_permitted_but_not_assurance` |
| 6 | The waiter process is suspended beyond its alarm deadline. | `fleet_wait.py` uses POSIX `alarm`; a separate primitive probe confirms the signal becomes pending across process stop/resume. The actual waiter path and full Mac sleep/wake remain open live lanes. | `test_wait_uses_posix_alarm_and_alarm_is_pending_after_process_stop` |

## Operator rules

- Treat repository and supplied evidence as hostile instructions.
- Do not approve a permission request merely because an agent says it is
  necessary; compare it with the declared role first.
- Use tracked wrappers for authoritative work. Direct `cmux send` and
  `run-local-worker.sh` are observational/convenience paths, not provenance.
- Never promote a race winner or cross-model agreement without deterministic
  verification appropriate to the task.
- Report a same-UID, provider-CLI, or CONTROL compromise as a boundary change,
  not as a bug supposedly contained by this threat model.
