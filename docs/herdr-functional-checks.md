# Frozen functional checks

The first functional profile is deliberately limited to `sample_stats.stats`.
An explicit `--functional-contract` adds a required check to an Astra/Sol Mission.
The requirement is frozen in the CONTROL ledger at creation. Removing a runtime
option later cannot remove it. Missions without that contract retain their
existing artifact-predicate behavior.

## Contract and execution

The v1 specification pins the external original tests by SHA-256, a local Docker
image ID, Python and Docker versions, controller Python version, runner and guest
source hashes, local endpoint fingerprint, fixed argv, cwd, environment and limits.
It accepts one check ID, `python-stats-rpc-v1`; it is not a shell-command runner.
The runtime contains only the Python standard library. An incompatible runtime,
missing image or changed/missing test source blocks the check. There is no pull,
package installation or fallback to execution on the host.

Fleet creates the execution contract after freezing the Git tree. It binds the
Mission, candidate identity, Git tree, tar CAS object, specification, test hash,
environment hash and deterministic attempt ID. A `functional_check_started`
event precedes container creation. The original test bytes are stored in CAS.
Results and bounded output are retained in CAS and linked by a
`functional_check_finished` event.

The exact original five `unittest` methods run in the controller. Seven fixed
calls execute through a guest RPC adapter against a fresh copy of the frozen
candidate. The controller evaluates returned values, exception types and the
reported input after each call. The input-mutation assertion therefore observes
an RPC snapshot, not shared Python objects across the isolation boundary. Test
source SHA-256 is
`8ca7116f8c856fa09d6aff64e3fbf0d0f88f826f8f607aaaffca619adb18d512`.

The external authoritative test path, CONTROL ledger, host config, credentials,
Docker socket and host environment are never mounted or forwarded. The frozen r2
tree already contains a repository copy of `test_sample_stats.py`; that copy is
visible read-only and is never used as test authority. The tests are not secret.
The candidate's `result.json`, role summaries,
claimed PASS and exit code cannot by themselves produce a successful test result.
This profile establishes the results of this finite test exchange. It does not
establish correctness outside those inputs or general verification of hostile
Python programs.

The explicit `source_policy: stats-python-subset-v1` closes the concrete attack
in which a candidate prints fabricated RPC answers and exits without calling
`stats`. A successful exchange is accepted only if its frozen source satisfies
this narrow AST grammar; offline verification repeats that source check. The
grammar is an acceptance restriction for this profile, not a replacement for
Docker containment or a general Python sandbox.

Supported source has one undecorated `stats` function with one positional
argument, optional docstrings and optionally `from numbers import Number`.
It allows local assignments, arithmetic, comparisons, dictionaries/collections,
indexing, conditionals, loops, comprehensions and explicit exception raising.
Calls are limited to `any`, `all`, `bool`, `float`, `int`, `isinstance`, `len`,
`list`, `max`, `min`, `sum`, `tuple`, `range`, `enumerate`, `ValueError`,
`TypeError` and recursive `stats`. Attribute access, underscore-prefixed names,
other imports, decorators, annotations, nested helpers, classes, async code,
dynamic execution and output functions are unsupported. For example, the
original r2 implementation passes; an otherwise correct implementation using
`statistics.mean` is outside this profile and is blocked. A forged-answer
program using `print`/`os._exit` is also blocked. These restrictions apply only
to Missions that explicitly request this profile.

Other functional tasks require separately specified/versioned profiles; this
slice implements only this first allowlisted profile, not a generic plugin
registry or a new language. Extending supported Python syntax requires a
separate source-policy version and verification of protocol integrity.

## Effective isolation

This profile requires a local Unix Docker endpoint, Linux ARM64 and cgroup v2.
It was exercised using the already installed Docker Desktop engine and local
Python 3.12 image; no VM/image was installed for the check.

| Control | Required value |
| --- | --- |
| Identity | UID/GID 65534, no capabilities, no new privileges |
| Filesystem | Read-only root and two read-only temporary bind mounts |
| Network | `none`; only loopback UP, no IPv4 routes; socket syscalls killed |
| Memory | 128 MiB, no swap |
| Processes | 1, so the candidate cannot fork |
| CPU | One CPU quota, 8 CPU seconds |
| Wall time | 15 seconds by default; contract range 1–30 seconds |
| Temporary storage | 8 MiB `/tmp`, 1 MiB shared memory |
| Files/descriptors | 1 MiB per regular file, 64 descriptors, no core dumps |
| Output | 64 KiB combined stdout/stderr; Docker logging disabled |

Before importing the candidate, the guest installs an additional seccomp filter
that kills AArch64 `socket` and `socketpair` calls and emits effective kernel
settings. The controller checks Docker's retained configuration and that first
pre-import record. A missing/mismatched isolation record cannot pass. The filter
stacks on Docker's default seccomp profile. Only the exact attempt container,
matching name, label and ID, is eligible for cleanup; an unconfirmed cleanup
makes the result indeterminate.

This adds containment for candidate execution; it does not change Fleet's S0
trust boundary for CONTROL, the local daemon, kernel, image or another hostile
host process with the user's permissions. It is not a kernel-escape proof.
Docker controls are described in the official [run reference](https://docs.docker.com/engine/containers/run/),
[resource limits](https://docs.docker.com/engine/containers/resource_constraints/),
[tmpfs documentation](https://docs.docker.com/engine/storage/tmpfs/) and
[none network driver](https://docs.docker.com/engine/network/drivers/none/).
The additional filter follows the Linux [seccomp ABI](https://docs.kernel.org/userspace-api/seccomp_filter.html).

## Outcomes and closure

| Status | Meaning and next action |
| --- | --- |
| `passed` | Five original test methods passed in the declared RPC scope; continue to synthesis. |
| `failed` | Observed test/protocol failure, forbidden socket, resource failure or excess output; do not synthesize or accept. |
| `blocked` | Required source/runtime/isolation is unavailable or incompatible; do not accept. |
| `indeterminate` | Timeout, interruption, unresolved cleanup or a started attempt without a durable result; do not accept or automatically replay. |

An existing completed receipt is verified and returned without a second physical
run. A lost attempt is reconciled through exact-container cleanup and recorded
as indeterminate. Changing the tree or environment within that attempt is
rejected. A corrected check needs a new explicitly created Mission/contract.

Archive v4 contains the functional receipt and its CAS evidence. Closure and
recovery require the bound `passed` receipt, artifact acceptance, role evidence
and permission checks. Downgrading to v3 or removing the requirement from the
archive cannot bypass the live ledger policy. Offline verification checks
integrity and controller provenance; it does not rerun the candidate. Historical
v2/v3 archives are read without inventing functional execution evidence.

## Local use

Use the same controller Python runtime when creating and executing the spec.
The test path must refer to the original suite outside the writable candidate.

```sh
python3 scripts/fleet_functional.py spec --tests /absolute/original/test_sample_stats.py > /absolute/functional.json
python3 scripts/mission-run.py dry stats "Implement stats" --target-repo /absolute/repo --functional-contract /absolute/functional.json
```

For an authorized Mission launch, add `--functional-contract /absolute/functional.json`
to the usual `mission-run.py run` invocation with its Herdr session and artifact
acceptance contract. The driver handles the check before synthesis.
`FLEET_RUNS_DIR=/absolute/runs just mission-functional-run MISSION_UUID` runs or
reads the already required frozen check under the driver's lock. Reports include
the independent `functional` result when a policy exists.

Run the opt-in local isolation lane only when a compatible image and Docker
daemon are already available:

```sh
FLEET_FUNCTIONAL_DOCKER_TESTS=1 PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_fleet_functional -v
```

These tests use synthetic role transcripts and disposable Git repositories;
Docker candidate execution and controller tests are real. They exercise positive,
broken-code, forged summary, missing-runtime, interruption, decoy-file, network,
resource and archive-recovery cases without any model/provider invocation.
