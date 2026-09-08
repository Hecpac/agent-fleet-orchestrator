# Worker sandbox integration binding: receiver contract, runtime hook missing

Status at 2026-09-07:

| Layer | Verdict | Evidence scope |
| --- | --- | --- |
| POLICY_CONSTRUCTION | VERIFIED (input evidence, not repeated) | Fleet's requested Worker policy |
| SANDBOX_CONFORMANCE | PASS (input evidence, not repeated) | Codex CLI 0.153.0 / Seatbelt, independent sandbox command |
| INTEGRATION_BINDING | NOT_VERIFIED | No authenticated observation from the interactive Worker's actual exec |

This change adds an **offline receiver contract and adversarial tests**, not a
working runtime emitter. It does not change boot, dispatch, routing, models,
permissions, recovery or acceptance. No effective attestation of a real Worker
was produced. No real canary was sent through Fleet/Herdr/Codex in this audit.
The tests use synthetic records and actual temporary CAS/ledger files; they do
not substitute a simulated backend for the interactive runtime.

## Where evidence is lost

The current path is:

1. `fleet_herdr_permissions.policy()` / `launch_flags()` specify workspace-write,
   approval never, network false, empty extra writable roots and both temporary
   exclusions false. They describe requested policy, not materialized policy.
2. `HerdrBackend._compiled_launch()` binds the model/effort from the compiled
   router. `_start_arguments()` adds project trust and permission arguments.
3. `HerdrBackend.boot()` persists an attempt, then calls
   `_command(_start_arguments(member), timeout=45)`. `_command()` starts the
   Herdr **CLI** with Fleet's environment. It does not spawn the Codex Worker.
4. Herdr's session/pane runtime launches its canonical `codex` executable from
   an interactive shell. The returned `agent_started` receipt contains `agent`
   and `argv`. `_verify_start_argv()` verifies argv and the basename `codex`,
   not the native executable image, its version, loaded configuration or the
   environment that will reach a tool subprocess.
5. `submit()` later sends a Driver JSON prompt via `herdr agent prompt`.
   Only at this point is a stage `run_id` bound to that already-running member.
   One member can serve multiple runs. `attempt_id` denotes a launch attempt;
   `run_id` must not be fabricated during boot or reused as a process identity.
6. Codex resolves configuration and permissions and dispatches a tool command
   through its sandboxing implementation. Fleet cannot observe that object at
   the spawn boundary. The npm launcher can also select a separate native image.
7. `fleet_herdr_evidence.verify_result()` validates CAS-bound transcript context.
   `fleet_herdr_permissions.attest()` explicitly returns scope
   `recorded_codex_turn_configuration`, not OS enforcement or integration binding.

There are at least **two different boundaries**. Immediately before starting
Codex, the launcher can observe executable/argv/environment but cannot observe
Codex configuration that has not yet been loaded. Effective tool policy must
also be captured inside Codex, immediately before the corresponding tool exec.
A Fleet-side wrapper or a later transcript reconstruction cannot replace that.

## Installed interfaces inspected, without contacting a live session

Metadata commands (temporary HOME/CODEX_HOME, no credentials copied):

```sh
herdr --version
herdr agent start --help
herdr pane process-info --help
herdr api schema --json
codex --version
codex exec-server --help
codex app-server generate-json-schema --experimental --out <temporary-schema>
```

Observed Herdr: **0.8.2**, bundled API protocol **20**, schema version **1**.
`AgentStartParams` accepts name/kind/pane/args/timeout; it has no pre-exec
observer or executable-override field. `agent_started` returns `agent` and
`argv`. `PaneProcessInfoProcess` exposes pid/name and optional argv/argv0/cmdline/
cwd; it has no process birth identity, effective environment or sandbox policy.
The bundled schema contains no sandbox/attestation/pre-exec facility.

Observed Codex: **0.153.0**. Its generated `CommandExecParams` explicitly
describes a **standalone** command without a thread or turn. It accepts optional
permissionProfile/sandboxPolicy/env/cwd. Its response contains exitCode/stdout/
stderr, not the required binding. `exec-server` is also a standalone service.
Neither is an operation inside the Worker launched through the existing Fleet
path; neither was started. `ActivePermissionProfile` supplies id/extends, which
is not an OS-applied grant set. No model session or provider was invoked.

Local native-binary symbols identify useful implementation locations (not
supported extension APIs; offsets are specific to this installed image):

- `codex_core::config::permissions::resolve_permission_profile`
- `codex_core::exec_env::inject_permission_profile_env`
- `codex_sandboxing::manager::SandboxManager::transform` at `0x1072351b4`
- `codex_sandboxing::seatbelt::create_seatbelt_command_args` at `0x107224e6c`

Native Codex image SHA-256 previously observed in the same audit session:
`a29d9e86eef88cbbd69f97ce8c590b1d0a287c8f77424f5eef226b883d7eaa22`.
Re-observe the executable image at launch; this value is not a runtime pin for
future operations. Source for these installed runtimes is not part of the
authorized Fleet checkout. Binary symbols do not prove an internal call path
was executed in this audit.

## Minimum missing hook: one authenticated operation, two observations

The following is an interface proposal, **not available CLI syntax**:

1. CONTROL freezes a launch challenge before the existing `boot()` start call:
   mission_id, generation, role, attempt_id, requested-policy digest and the
   physical candidate identity. Herdr must forward it over a trusted runtime
   channel, not through model prompt text or a Worker-writable config file.
2. In the Herdr launcher, observe the executable resolution/exec chain, exact
   argv and actual environment at process creation. The parent records kernel
   process identity including birth identity, not PID alone. Npm shim identity
   and the actual native Codex image must both be reconciled. Store a launch
   receipt outside all effective Worker writable roots.
3. At `submit()`, CONTROL creates a fresh operation challenge binding that
   launch receipt to run_id, attempt_id, generation and the deterministic canary
   command digest. Do not invent a run_id during boot. A recovered operation
   must use the exact current attempt/challenge, or reconcile already-existing
   evidence; it cannot treat an older attempt's receipt as a new observation.
4. Add the emitter inside Codex **after configuration, profile intersections,
   workspace/temp resolution, environment injection and sandbox transform**,
   directly adjacent to the actual spawn. Emit from the very same finalized
   object consumed by spawn, including the final Seatbelt profile digest. Do
   not regenerate this object from CLI flags. Use a controller acknowledgement
   barrier so a mismatch aborts the tool exec before any effect.
5. The spawned tool cannot inherit the write end of the attestation channel or
   a signing/commitment secret. Authenticate runtime/process identity using an
   OS-backed channel and a controller-issued capability. A JSON `trusted=true`,
   matching basename, peer UID alone, HMAC produced with a Worker-readable key,
   or a self-reported PID is insufficient. Same-user process access must be
   included in the actual threat boundary assessment.
6. An external observer pins pre/post files and roots with no symlink following,
   observes the exact child/process birth identity, and checks allowed writes,
   all protected canaries, traversal/symlink/absolute attempts, local network
   bind denials and stdout's lack of ledger/acceptance authority. It must await
   all owned descendants and verify quiescence; a parent exit is insufficient.
7. CONTROL links requested policy, launch receipt, pre-exec receipt, operation
   digest, effects report and process termination into CAS and a new ledger
   event before any binding verdict. The ledger anchor must be controller-owned
   and independently protected, not supplied by the Worker. Version and replay
   rules belong to this hook, not to existing artifact acceptance predicates.

This requires an extension in the actual runtime(s). The current task only
authorizes local Fleet changes, so no installed executable, global PATH,
provider, routing or hook configuration was altered to simulate this channel.
An authorized future deterministic loopback provider could drive the actual
interactive tool dispatch, but still needs this observation channel. Its
Responses/tool-call compatibility, tool selection, session identity and
approval/permission propagation would remain to be tested; no loopback provider
has been implemented or invoked here.

## Receiver implemented in this checkout

`scripts/fleet_herdr_binding.py` defines a version-1 candidate
`effective-policy-attestation` record. It requires:

- mission/run/generation/role/attempt, fresh challenge and requested-policy SHA;
- canonical candidate realpath plus device/inode;
- native Codex image realpath/device/inode/content SHA and observed version;
- redacted argv/config commitments and inherited environment variable names;
- HMAC of full environment/invocation values, separately from sanitized SHA;
- effective sandbox/approval/product roots/materialized temporary roots;
- Codex and tool PID **plus birth identities**, final Seatbelt profile digest;
- operation digest, external observer CAS id and a Mission ledger event link.

`validate_record(raw, expected=..., pinned_sha256=...)` compares against
independent CONTROL observations and rejects inconsistent records. It performs
online path/inode/image checks. It cannot observe a process, authenticate an
emitter, interpret an effects report or prove OS storage isolation by itself.
Even fully consistent synthetic evidence returns:

```json
{"record_consistent": true,
 "INTEGRATION_BINDING": "NOT_VERIFIED",
 "reason": "trusted_exec_hook_unavailable"}
```

The caller must never derive `expected` or `pinned_sha256` from an untrusted
submission. HMAC keys must be transient controller material, excluded from the
Worker and its readable files. Redacted hashes alone cannot detect changes to
secret values. HMAC equality with an independently held observation detects
value drift; it still does not prove emitter provenance.

`quarantine_record()` stores a consistent, unauthenticated candidate in existing
CAS and checks its ledger/observer links and root placement. It is **not wired
into boot, submit, recovery, archives or acceptance**. The returned authority
is `none`; the ledger link is one-way and no acceptance/anchor event is created.
The same-user CAS ownership/modes are not a claim of Worker non-writability.

## Verification and remaining boundaries

```sh
python3 -B -m unittest tests.test_fleet_herdr_binding tests.test_fleet_artifacts
git diff --check
```

Tests use temporary directories under HOME (or `FLEET_TEST_CONTROL_PARENT`,
which must be outside `/tmp`) and remove only their own fixtures. Synthetic
image bytes are read but never executed; no Git history or provider is created.
They test alteration/forgery, identities, policy digest/roots, symlink/alias/
traversal, same-path inode replacement, image/version drift, redacted value
drift, process-birth/challenge/attempt replay, CAS tampering and missing links.
They are receiver tests, not SANDBOX_CONFORMANCE or INTEGRATION_BINDING tests.

Inherited risks remain explicit:

- workspace-write permits external reads; it does not protect confidentiality;
- HOME/CODEX_HOME and effective config/environment may differ inside Herdr;
- `/tmp` and TMPDIR are writable policy roots and can be shared by same-user
  processes; empty `writable_roots` does not remove those implicit roots;
- CONTROL/CAS/ledger/runs need physical placement outside **every** effective
  writable root; role names and mode 0600 do not enforce this;
- Codex's tool sandbox is not proof that the entire Codex/Herdr process tree is
  confined, that same-user processes cannot tamper, or that evidence is safe;
- fresh hashing of an executable cannot alone close the subsequent exec race.

No real permitted/denied canary effects or descendant cleanup were measured in
this iteration. Those parts remain NOT_VERIFIED. The next decision is to add
the above runtime hook within a separately authorized runtime change, then
exercise one operation through the existing Fleet path. Existing conformance
evidence cannot close this remaining boundary.
