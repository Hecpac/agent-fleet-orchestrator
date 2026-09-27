# Opt-in physical scope acceptance (A)

This adds candidate acceptance evidence to `sol_minimal_v1`. It does **not**
implement the autonomous repair/amendment cycle, change the one-Worker topology,
grant permissions, apply changes to the original checkout, or launch extra roles.
Without `--scope-contract`, existing creation and archive behavior is unchanged.

## Contract and activation

Pass the following JSON as `--scope-contract /absolute/path/to/scope.json` to
`mission-run.py dry` or an already authorized `mission-run.py run` using
`--workflow herdr-minimal-implementation`. Other profiles reject this opt-in.
An artifact acceptance contract remains required independently.

```json
{
  "schema_version": 1,
  "editable_paths": ["answer.txt"],
  "temporary_directories": [".fleet-scratch"],
  "max_entries": 1000,
  "max_bytes": 1048576
}
```

Paths are exact canonical relative paths, not globs. Editable paths are files;
temporary paths are directories that must not preexist in the baseline.
Overlaps, aliases, root `.git` paths and traversal are rejected. New parent
directories required for declared paths are allowed. Changes to preexisting
directories are conservatively rejected in v1. Symlinks, hardlinks, special
files and special permission bits are unsupported, never silently skipped.
There are at most 256 declarations per list, 10,000 inventory entries, 128 MiB
of file content per capture pass and 64 levels of directory descent. Lower
entry/byte limits can be selected in the contract. Limits include temporaries.

```sh
python3 -B scripts/mission-run.py dry scope-example "Implement the agreed answer" \
  --workflow herdr-minimal-implementation \
  --target-repo /absolute/path/to/clean/repository \
  --acceptance-contract /absolute/path/to/acceptance.json \
  --scope-contract /absolute/path/to/scope.json --json
```

Dry mode validates configuration and reports the contract digest and expected
archive version; it does not capture a candidate or prove physical acceptance.
The existing clean-target/preparation restrictions still apply. This change
does not add dirty-origin or general snapshot support.

## Evidence and acceptance

`mission_created.scope_contract_sha256` pins the exact contract. After candidate
preparation, before boot/admission, CONTROL captures a complete physical baseline
in CAS and records `herdr_scope_baseline_captured`. It cannot be replaced or
recaptured after a Worker has started. The Worker receives `physical_scope` in
its task packet; it does not calculate baseline hashes or author the receipt.

Capture uses two bounded descriptor-based passes, with no-follow reads and
identity/change checks. It records file content hashes, sizes and modes, plus
directories; ignored files are included. Only root `.git` is excluded. Existing
candidate Git/HEAD/identity checks remain separate; this feature does not audit
all Git metadata. Hash/size limits, unreadable files, unsupported types or a
detected capture race produce `incomplete`, never a partial PASS.

The receipt distinguishes:

- `accepted`: the observed delta meets the contract.
- `rejected`: an undeclared new path, changed protected baseline path, invalid
  temporary declaration or tree/inventory mismatch exists.
- `incomplete`: a complete observation could not be made.

Preexisting ignored files, where a baseline contains them, must be preserved and
are not automatically exported. Files tracked in the baseline and explicitly
editable files form the delivered tree. An explicitly editable ignored file may
be delivered. Declared temporaries are inventoried but omitted from the private
Git index, tar and patch, even if Git does not ignore them. No automatic cleanup
is performed. Snapshot construction preserves the original index/HEAD and
verifies that the patch reconstructs the selected tree.

Scope is checked before/after freeze and again while capturing archive contents.
The final tree must match receipt content and Git executable modes. Permitted
temporary changes do not change the frozen product or cause an immutable-freeze
conflict. A later undeclared residue still blocks archive creation.

Opt-in archives use schema **v8**, with `scope/baseline.json` and
`scope/result.json`, linked to the creation pin, baseline event and frozen tree.
The existing durable index selection and archive anchor bind these bytes.
Offline verification re-evaluates the receipt against the archived inventories
and tree; it does not inspect a now-missing candidate or reobserve the filesystem.
Removing scope evidence or downgrading the schema cannot satisfy its creation
binding. Historical v2–v7 readers and the default minimal v7 remain supported.

On rejection/incomplete capture the driver returns `scope_rejection`, including
an immutable CAS receipt ID; the foreground supervisor stops instead of polling
the same failure. The Mission is not declared successful, no extra Worker is
started, and automatic repair is **not implemented**. Explicit recovery retains
the original baseline and existing freeze/terminal rules. Binding corruption
fails closed; existing exact cancellation/recovery remains available.

The receipt proves the observed candidate state and delivered bytes. It does
not prove absence of transient writes, effects outside the observed root, OS
containment or functional correctness. Artifact/functional acceptance and
permission attestation remain separate. Provider execution and the future
autonomous cycle remain `NOT_VERIFIED` by these local fixtures.

## Local checks

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest tests.test_fleet_herdr_scope -q
```

Fixtures cover ignored residue, permitted temporaries, altered baseline content
and substituted baseline evidence, incomplete capture, link/race rejection,
creation binding, offline v8 verification and downgrade rejection. The Mission
fixtures use a simulated backend and the real controller/ledger/archive code;
they do not launch Herdr, Codex, providers or Docker.
