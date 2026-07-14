# Mission Control Slice 7 — unified archive evidence

Date: 2026-07-14

## Implemented

- `scripts/fleet_archive.py` creates and verifies content-addressed Mission archives.
- Standard evidence, Git commit metadata, a binary full-index patch, and the exact final tree are indexed.
- Assured dialogue/assurance stores and the public signed audit envelope are included; CONTROL HMAC, private signing keys, service logs, environment, and credentials are excluded explicitly.
- `full`, `redacted`, and `hash-only` content policies preserve hashes and record every omission.
- Mission completion and mission-bound teardown verify the archive before success or workspace removal.

## Deterministic evidence

Command:

```text
python3 -m unittest tests.test_fleet_archive -v
```

Result: `Ran 4 tests ... OK`.

The tests use a real temporary Git repository. They prove that a binary writer
commit remains reachable, the archived patch matches the exact base/final
pair, `final-tree.tar` hashes to the final Git tree, and verification succeeds
after copying the archive outside the runs directory. They also prove that a
one-byte mutation and a source symlink fail closed.

The assured test starts the real local AuditService, records
`ArchiveContentRoot`, produces a signed Ed25519 verification receipt, verifies
it offline, and confirms that neither `control-hmac.key` nor the private
Ed25519 key enters the archive.

## Deviation log

- A Mission whose risk categories include `credentials` or `private_data`
  cannot currently use `full` at all. This is intentionally stricter than the
  plan's optional archive-specific approval because the existing approval
  contract scopes assurance effects, not disclosure of archived plaintext.
  Introducing a second human approval semantic inside this slice would weaken
  rather than preserve that contract. `redacted` or `hash-only` is required.
- The optional prerequisite Git bundle described in the architecture is not
  emitted because `final-tree.tar`, `change.patch`, and `commits.json` satisfy
  every mandatory acceptance criterion without adding a second restoration
  format.
