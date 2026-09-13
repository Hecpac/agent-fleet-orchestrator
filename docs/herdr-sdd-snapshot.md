# SDD stage-2 snapshot primitive (standalone)

`scripts/fleet_sdd_snapshot.py` is a bounded standalone primitive for the SDD
roadmap stage 2: it freezes one validated `fleet.sdd.plan.v1` document per
Mission and recovers it without rereading the mutable source. It is **not** the
full stage-2 integration.

## Public API

- `freeze_plan(store: pathlib.Path, mission_id: str, source: pathlib.Path) -> dict`
  reads the exact source bytes, validates them, stores them, and writes an
  immutable Mission binding. It returns the manifest.
- `load_plan(store: pathlib.Path, mission_id: str, expected_sha256: str) -> dict`
  verifies the binding, requested Mission, trusted caller-supplied digest, blob
  hash and SDD validation, then returns the parsed original plan.
- `SnapshotError(ValueError)` is raised for every rejected input, identifier or
  integrity condition.

## Storage layout

```
STORE/sdd/blobs/<sha256>.json      raw exact UTF-8 source bytes, name = sha256
STORE/sdd/missions/<mission_id>.json   manifest, canonical lowercase UUID name
```

Manifest keys are exact: `schema="fleet.sdd.snapshot.v1"`, `mission_id`,
`plan_sha256`, `functional_status="NOT_VERIFIED"`. No canonical reserialization
of the plan occurs: the blob is byte-for-byte the accepted source bytes.

## CLI

```sh
python3 -B scripts/fleet_sdd_snapshot.py freeze --store /absolute/store \
  --mission-id MISSION_UUID --plan /absolute/plan.json
python3 -B scripts/fleet_sdd_snapshot.py load --store /absolute/store \
  --mission-id MISSION_UUID --sha256 SHA256
```

Success prints JSON on stdout and exits `0`. A rejected operation prints a
concise `snapshot error: ...` line on stderr and exits `2`.

## Guarantees

- Mission IDs must be canonical lowercase UUID text; digests must be exactly 64
  lowercase hex characters. Malformed or traversal identifiers are rejected
  before any filesystem mutation.
- Source parsing rejects duplicate JSON keys and non-JSON constants
  (`NaN`/`Infinity`) and runs the existing `fleet_sdd_contract.validate` before
  any state is published.
- Publication is atomic and non-clobbering: a conflicting binding or blob is
  detected, never silently repaired, and a partial bound plan is never exposed.
  An interrupted publication may leave an unreferenced valid blob, which `load`
  ignores because no binding references it.
- Repeated freeze of identical bytes for the same Mission is idempotent; a
  different plan for that Mission fails without replacing the old binding. The
  same plan for two Missions yields independent bindings sharing one blob.
- `load` never consults the original source; recovery works in a fresh process
  after the source is edited or deleted.
- The source is opened non-blocking and must be a regular file: directories,
  FIFOs and devices are rejected promptly without hanging. Source `open`, `fstat`
  and `read` failures surface as `SnapshotError` with the descriptor closed and
  no store mutation.
- Existing symlinks at or under `STORE` (including `STORE` itself) are rejected,
  as are manifest and blob symlinks.

## Limitations

- This module is the **standalone snapshot primitive** only. The opt-in Herdr
  driver integration and archive/frozen-candidate binding are layered on top in
  the sections below; that integration does not grant acceptance, execution or
  Mission authority either. Any remaining **NOT_VERIFIED** status is scoped
  there, not to this primitive.
- The store is a trusted local directory. The primitive does not claim hostile
  same-UID race resistance, external custody or OS isolation; `fleet_safe_paths`
  provides the existing trusted-host/Unix-UID boundary.
- Concurrency safety covers non-clobbering first-writer binding for one Mission.
  It does not schedule, retry or garbage-collect unreferenced blobs.

## Herdr integration (stage-2 slice)

`scripts/fleet_herdr_sdd.py` is the opt-in integration helper used by
`fleet_mission.create_mission` and the Herdr driver. It is not part of the
standalone primitive.

- `freeze(runs_dir, mission_id, plan_path) -> str` freezes with the store set to
  `runs_dir`, so bindings live at `runs_dir/sdd/missions/<mission_id>.json` and
  blobs at `runs_dir/sdd/blobs/<sha256>.json`.
- `binding_exists(runs_dir, mission_id) -> bool` reads the optional binding
  manifest with a no-follow rooted read and reports whether it is present.
- `verify(runs_dir, current) -> dict | None` uses the ledger projection field
  `sdd_plan_sha256` as the only digest authority and returns the versioned stage
  packet, or `None` for a legacy Mission without the field.

`fleet_mission.create_mission(..., sdd_plan_path=...)` and
`mission-run.py run ... --sdd-plan PATH` are Herdr-only. The plan is frozen
before any ledger admission, candidate preparation, boot or dispatch; the
frozen digest is pinned as an optional `sdd_plan_sha256` in the
`mission_created` payload and projected state only when present. The digest is
never taken from `runtime-options.json`. Different bytes for the same identity
conflict without replacing the binding, and omitting `--sdd-plan` on an existing
opt-in retry fails closed instead of recreating a legacy Mission.

Every stage task packet then carries `sdd_plan`:

```json
{"schema_version": 1, "mission_id": "<uuid>",
 "plan_sha256": "<64 hex>", "plan": {"...": "validated fleet.sdd.plan.v1"}}
```

The plan is data only: scenario/check text is never executed and grants no
tools or permissions. Checks remain `NOT_VERIFIED`. Integrity failures (missing,
corrupt or foreign snapshot; wrong binding; changed digest; symlink) fail closed
before new admissions, dispatch or archive work, but do **not** block
reconciling an already-authorized pause or exact cancellation of an owned run.

## Archive closure (schema v5)

Herdr archive **schema v5** adds the frozen SDD evidence to the offline archive:

- `sdd/plan.json` — the exact frozen plan bytes (blob).
- `sdd/binding.json` — the immutable snapshot binding manifest.

Authority is explicit and layered:

- The Mission ledger `sdd_plan_sha256` (from `mission_created`) is the only
  digest authority. It is never taken from `runtime-options.json`, the snapshot
  manifest or a recomputed index.
- `candidate-freeze.json` carries the same `sdd_plan_sha256`, so the frozen
  candidate receipt is bound to the plan independently of producer inputs.
- `creation-request.json` `request.sdd_plan_sha256` must equal the ledger pin.
- Before `archive_created`, the durable `herdr_archive_selection` event pins the
  index artifact; rewriting or coherently re-indexing the producer snapshot
  *after* that durable selection cannot pass. Independently of index integrity,
  malformed producer content is rejected before acceptance by semantic binding
  verification (plan/binding/creation/freeze pins), while harmless extra
  non-authority metadata is not universally rejected. An SDD v5 completion with
  no selection fails closed.
- `archive.verify` revalidates the archived plan, binding and digests from
  archived bytes plus the authority ledger only. Producer receipts, agent PASS
  summaries and a recomputed unanchored index alone never authorize acceptance.

Independent verification works after the mutable source, candidate, runtime
files and live SDD store are removed. It does not claim authenticity against a
full trusted-ledger replacement. Legacy non-SDD archives and the existing
artifact acceptance, functional and permission gates are unchanged.

**Scope:** this closes stage-2 integrity/membership only. Scenario-to-test
compliance is stage 5 and remains `NOT_VERIFIED`; plan prose is never executed.
Stages 3–6 of the roadmap remain pending.
