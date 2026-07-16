# Local WORM with RustFS

This recipe proves S3 versioning and Object Lock `COMPLIANCE` mechanics on one
computer. Its evidence is deliberately labeled `local-development`; it is not
evidence of external regulatory compliance, independent custody, or durable
off-machine retention.

## Prerequisites and owned state

The helper requires Docker Desktop, OpenSSL, and Python 3. It pins:

```text
rustfs/rustfs:1.0.0-beta.3@sha256:378642b05b7dcb4849fb77ebe6aca4ced1c3f66e7e504247df95a5c9018d3358
```

By default it owns only `/tmp/agent-fleet-worm-local-$UID`, one Docker container,
and one Docker volume whose names are derived from that path and bound to a
random ownership label stored in the private state. The directory is
mode `0700`; generated credentials, CA private key, server private key, state,
and environment files are mode `0600`. Nothing under that directory is
versioned.

Check Docker before setup:

```bash
docker version --format '{{.Server.Version}}'
```

## Generate CA/certificate, start RustFS, and initialize the bucket

One command generates a seven-day local CA and a server certificate with
`DNS:localhost` and `IP:127.0.0.1`, starts RustFS over TLS on loopback, creates
the bucket with Object Lock enabled, and verifies both versioning and Object
Lock:

```bash
just worm-local-setup
```

The generated files are:

```text
/tmp/agent-fleet-worm-local-$UID/certs/ca.pem
/tmp/agent-fleet-worm-local-$UID/certs/rustfs_cert.pem
/tmp/agent-fleet-worm-local-$UID/certs/rustfs_key.pem
/tmp/agent-fleet-worm-local-$UID/worm.env
```

Verify the certificate chain independently:

```bash
STATE_DIR="/tmp/agent-fleet-worm-local-$UID"
openssl verify -CAfile "$STATE_DIR/certs/ca.pem" "$STATE_DIR/certs/rustfs_cert.pem"
```

The setup request sends `x-amz-bucket-object-lock-enabled:true`; it then reads
`?versioning` and `?object-lock` and requires `Enabled` from both. Setup fails
closed and removes its partial container/volume/state if any step fails.

To use another state directory or loopback port, call the helper directly:

```bash
python3 scripts/fleet_worm_local.py \
  --state-dir /tmp/my-fleet-worm setup --port 9543
```

## Configure the application environment

Setup writes a private, generated environment file outside the repository. Load
it only in the shell that will run the local workflow:

```bash
STATE_DIR="/tmp/agent-fleet-worm-local-$UID"
set -a
. "$STATE_DIR/worm.env"
set +a
```

Its relevant non-secret policy values are:

```text
FLEET_WORM_ENDPOINT=https://localhost:9443
FLEET_WORM_CA_FILE=/tmp/agent-fleet-worm-local-$UID/certs/ca.pem
FLEET_WORM_RETENTION_DAYS=1
FLEET_WORM_BACKEND_NAME=rustfs:1.0.0-beta.3
```

Do not copy the generated access or secret key into the repository. Omitting a
custom CA uses the system trust store; configuring a missing, unreadable,
symlinked, empty, or invalid CA fails closed. HTTP and disabled certificate
verification are unsupported. Each request revalidates DNS and connects only to
the validated numeric loopback address while preserving TLS hostname checks.

## Run the real local-WORM smoke

```bash
just worm-local-smoke
```

The smoke compiles `workflows/local-worm.yaml`, creates an isolated Mission
outside the repository, and exercises the production `AuditLifecycle` and
`S3ObjectLockSink`. It requires every anchor receipt and ledger event to agree
on:

- `worm=true`;
- `trust_scope=local-development`;
- backend and object key;
- non-empty exact version ID;
- `retention_mode=COMPLIANCE` and a future retain-until value;
- the event SHA-256 digest.

It then signs and verifies the aggregate audit receipt offline. Finally it
compiles `workflows/regulated.yaml` and preflights the same endpoint. That
negative smoke must reject the loopback DNS resolution before creating audit
state.

## Prove exact-version deletion is rejected

The smoke already performs this proof. It can be repeated against the last
receipt:

```bash
just worm-local-delete-test
```

The helper sends `DELETE ?versionId=<exact-version>`, requires HTTP `403` with a
retention/Object-Lock reason, then sends `HEAD ?versionId=<exact-version>` and
requires the same version ID, `COMPLIANCE`, retain-until, and digest. A generic
TLS, network, authentication, or missing-object failure is not accepted as
retention evidence.

## Teardown

The teardown checks the state marker, exact derived Docker object names, and
the random ownership label on both objects before any removal. A missing or
mismatched label fails closed without deleting either object:

```bash
just worm-local-teardown
```

For a single disposable run that always attempts owned-resource teardown:

```bash
just worm-local-all
```

Object Lock protects object versions through the RustFS API; it does not make a
locally controlled Docker volume indestructible to the computer owner. Teardown
therefore removes the entire ephemeral volume intentionally.
