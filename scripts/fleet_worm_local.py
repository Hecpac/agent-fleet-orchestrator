#!/usr/bin/env python3
"""Provision and prove an ephemeral HTTPS RustFS Object Lock backend."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Iterator
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_audit_client  # noqa: E402
import fleet_audit_control as audit  # noqa: E402
import fleet_mission  # noqa: E402
import fleet_mission_state as mission_state  # noqa: E402
import workflow_config  # noqa: E402


IMAGE_TAG = "rustfs/rustfs:1.0.0-beta.3"
IMAGE_DIGEST = "sha256:378642b05b7dcb4849fb77ebe6aca4ced1c3f66e7e504247df95a5c9018d3358"
IMAGE = f"{IMAGE_TAG}@{IMAGE_DIGEST}"
BACKEND_NAME = "rustfs:1.0.0-beta.3"
DOCKER_OWNER_LABEL = "io.agent-fleet-orchestrator.local-worm-owner"
DEFAULT_STATE = Path(f"/tmp/agent-fleet-worm-local-{os.getuid()}")
STATE_FILE = "state.json"
EVIDENCE_FILE = "latest-smoke.json"


class LocalWormError(RuntimeError):
    """The local WORM environment or proof failed closed."""


def _run(command: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalWormError(f"cannot run {Path(command[0]).name}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise LocalWormError(f"{Path(command[0]).name} failed: {detail}")
    return result


def _write(path: Path, content: bytes, mode: int) -> None:
    mission_state.atomic_write(path, content, mode=mode)
    os.chmod(path, mode)


def _docker_exists(kind: str, name: str) -> bool:
    if kind not in {"container", "volume"}:
        raise LocalWormError("unsupported Docker object kind")
    try:
        result = subprocess.run(
            ["docker", kind, "inspect", name],
            cwd=ROOT,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalWormError(f"cannot inspect Docker {kind}: {exc}") from exc
    if result.returncode == 0:
        return True
    if "no such" in result.stderr.lower():
        return False
    raise LocalWormError(f"cannot inspect Docker {kind}: {result.stderr.strip()}")


def _docker_owner(kind: str, name: str) -> str:
    if kind not in {"container", "volume"}:
        raise LocalWormError("unsupported Docker object kind")
    label_path = ".Config.Labels" if kind == "container" else ".Labels"
    result = _run(
        [
            "docker",
            kind,
            "inspect",
            name,
            "--format",
            f'{{{{ index {label_path} "{DOCKER_OWNER_LABEL}" }}}}',
        ],
        timeout=30,
    )
    return result.stdout.strip()


def _assert_docker_owner(kind: str, name: str, owner: str) -> None:
    if _docker_owner(kind, name) != owner:
        raise LocalWormError(
            f"refusing to use or remove Docker {kind} without matching ownership label"
        )


def _remove_owned_quietly(kind: str, name: str, owner: str) -> None:
    try:
        if not _docker_exists(kind, name) or _docker_owner(kind, name) != owner:
            return
    except LocalWormError:
        return
    command = ["docker", kind, "rm"]
    if kind == "container":
        command.append("--force")
    command.append(name)
    subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "backend": state["backend_name"],
        "bucket": state["bucket"],
        "ca_file": state["ca_file"],
        "container": state["container"],
        "endpoint": state["endpoint"],
        "image": state["image"],
        "image_digest": IMAGE_DIGEST,
        "state_dir": state["state_dir"],
        "volume": state["volume"],
    }


def _load_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / STATE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalWormError(f"local WORM state is missing or invalid: {path}") from exc
    required = {
        "schema_version", "marker", "state_dir", "container", "volume", "image",
        "backend_name", "endpoint", "bucket", "region", "access_key", "secret_key",
        "ca_file", "retention_days", "docker_owner",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != 1
        or value.get("marker") != "agent-fleet-local-worm"
        or Path(str(value.get("state_dir"))).resolve() != state_dir.resolve()
    ):
        raise LocalWormError("local WORM state identity is invalid")
    return value


def _names(state_dir: Path) -> tuple[str, str]:
    suffix = hashlib.sha256(str(state_dir.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"fleet-worm-local-{suffix}", f"fleet-worm-data-{suffix}"


def _generate_certificates(certs: Path) -> None:
    certs.mkdir(mode=0o700)
    ca_key = certs / "ca-key.pem"
    ca_cert = certs / "ca.pem"
    server_key = certs / "rustfs_key.pem"
    server_csr = certs / "server.csr"
    server_cert = certs / "rustfs_cert.pem"
    extension = certs / "server.ext"
    _write(
        extension,
        (
            b"subjectAltName=DNS:localhost,IP:127.0.0.1\n"
            b"extendedKeyUsage=serverAuth\n"
            b"keyUsage=critical,digitalSignature,keyEncipherment\n"
            b"basicConstraints=critical,CA:FALSE\n"
        ),
        0o600,
    )
    _run([
        "openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
        "-days", "7", "-subj", "/CN=Agent Fleet Local WORM CA",
        "-keyout", str(ca_key), "-out", str(ca_cert),
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    ])
    _run([
        "openssl", "req", "-new", "-newkey", "rsa:3072", "-sha256", "-nodes",
        "-subj", "/CN=localhost", "-keyout", str(server_key), "-out", str(server_csr),
    ])
    _run([
        "openssl", "x509", "-req", "-in", str(server_csr), "-CA", str(ca_cert),
        "-CAkey", str(ca_key), "-CAcreateserial", "-days", "7", "-sha256",
        "-extfile", str(extension), "-out", str(server_cert),
    ])
    server_csr.unlink()
    (certs / "ca.srl").unlink(missing_ok=True)
    os.chmod(ca_key, 0o600)
    os.chmod(server_key, 0o600)
    os.chmod(ca_cert, 0o644)
    os.chmod(server_cert, 0o644)


def _sink(state: dict[str, Any], trust_scope: str = "local-development") -> audit.S3ObjectLockSink:
    return audit.S3ObjectLockSink(
        bucket=str(state["bucket"]),
        region=str(state["region"]),
        credentials=audit.S3Credentials(str(state["access_key"]), str(state["secret_key"])),
        endpoint=str(state["endpoint"]),
        retention_days=int(state["retention_days"]),
        trust_scope=trust_scope,
        ca_file=Path(str(state["ca_file"])),
        backend_name=str(state["backend_name"]),
    )


def _s3_call(
    sink: audit.S3ObjectLockSink,
    method: str,
    object_key: str | None,
    *,
    query: tuple[tuple[str, str], ...] = (),
    headers: dict[str, str] | None = None,
    payload: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    request = sink._signed_request(method, object_key, payload, headers or {}, query)
    with sink._open(request, timeout=30) as response:
        return (
            int(response.status),
            {key.lower(): value for key, value in response.headers.items()},
            response.read(),
        )


def _xml_value(payload: bytes, name: str) -> str:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise LocalWormError(f"S3 returned invalid XML while reading {name}") from exc
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == name:
            return (element.text or "").strip()
    return ""


def _bucket_status(state: dict[str, Any]) -> dict[str, str]:
    sink = _sink(state)
    _, _, versioning = _s3_call(sink, "GET", None, query=(("versioning", ""),))
    _, _, lock = _s3_call(sink, "GET", None, query=(("object-lock", ""),))
    status = {
        "versioning": _xml_value(versioning, "Status"),
        "object_lock": _xml_value(lock, "ObjectLockEnabled"),
    }
    if status != {"versioning": "Enabled", "object_lock": "Enabled"}:
        raise LocalWormError(f"bucket lacks required versioning/Object Lock: {status}")
    return status


def _initialize_bucket(state: dict[str, Any]) -> dict[str, str]:
    sink = _sink(state)
    try:
        _s3_call(
            sink,
            "PUT",
            None,
            headers={"x-amz-bucket-object-lock-enabled": "true"},
        )
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(2048).decode("utf-8", "replace")
        finally:
            exc.close()
        if exc.code != 409:
            raise LocalWormError(f"cannot create Object Lock bucket: {exc.code} {detail}") from exc
    return _bucket_status(state)


def _wait_for_tls(state: dict[str, Any]) -> None:
    sink = _sink(state)
    deadline = time.monotonic() + 45
    last_error = "not ready"
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(str(state["endpoint"]) + "/", method="GET")
            with sink._open(request, timeout=2):
                return
        except urllib.error.HTTPError as exc:
            exc.close()
            return
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            last_error = str(exc)
            time.sleep(0.25)
    raise LocalWormError(f"RustFS HTTPS endpoint did not become ready: {last_error}")


def setup(state_dir: Path, port: int) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    if not 1024 <= port <= 65535:
        raise LocalWormError("port must be between 1024 and 65535")
    if (state_dir / STATE_FILE).exists():
        state = _load_state(state_dir)
        container = str(state["container"])
        volume = str(state["volume"])
        owner = str(state["docker_owner"])
        if not _docker_exists("container", container) or not _docker_exists("volume", volume):
            raise LocalWormError("owned Docker container or volume is missing")
        _assert_docker_owner("container", container, owner)
        _assert_docker_owner("volume", volume, owner)
        _bucket_status(state)
        return _public_state(state)
    if state_dir.exists() and any(state_dir.iterdir()):
        raise LocalWormError("state directory already exists without owned state")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    container, volume = _names(state_dir)
    certs = state_dir / "certs"
    access_key = "fleetlocal" + secrets.token_hex(6)
    secret_key = secrets.token_urlsafe(32)
    docker_owner = secrets.token_hex(32)
    state: dict[str, Any] = {
        "schema_version": 1,
        "marker": "agent-fleet-local-worm",
        "state_dir": str(state_dir),
        "container": container,
        "volume": volume,
        "image": IMAGE,
        "backend_name": BACKEND_NAME,
        "endpoint": f"https://localhost:{port}",
        "bucket": "fleet-worm-local",
        "region": "us-east-1",
        "access_key": access_key,
        "secret_key": secret_key,
        "ca_file": str(certs / "ca.pem"),
        "retention_days": 1,
        "docker_owner": docker_owner,
    }
    objects_prechecked = False
    try:
        _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
        if _docker_exists("container", container) or _docker_exists("volume", volume):
            raise LocalWormError(
                "derived Docker container or volume already exists outside owned state"
            )
        objects_prechecked = True
        _generate_certificates(certs)
        docker_env = state_dir / "docker.env"
        _write(
            docker_env,
            (
                f"RUSTFS_ACCESS_KEY={access_key}\n"
                f"RUSTFS_SECRET_KEY={secret_key}\n"
                "RUSTFS_REGION=us-east-1\n"
            ).encode("utf-8"),
            0o600,
        )
        worm_env = state_dir / "worm.env"
        _write(
            worm_env,
            (
                "FLEET_WORM_BUCKET=fleet-worm-local\n"
                "FLEET_WORM_REGION=us-east-1\n"
                f"FLEET_WORM_ENDPOINT=https://localhost:{port}\n"
                f"FLEET_WORM_CA_FILE={certs / 'ca.pem'}\n"
                "FLEET_WORM_RETENTION_DAYS=1\n"
                "FLEET_WORM_BACKEND_NAME=rustfs:1.0.0-beta.3\n"
                f"AWS_ACCESS_KEY_ID={access_key}\n"
                f"AWS_SECRET_ACCESS_KEY={secret_key}\n"
            ).encode("utf-8"),
            0o600,
        )
        _write(
            state_dir / STATE_FILE,
            (json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"),
            0o600,
        )
        _run(["docker", "pull", IMAGE], timeout=300)
        _run([
            "docker", "volume", "create", "--label",
            f"{DOCKER_OWNER_LABEL}={docker_owner}", volume,
        ])
        _assert_docker_owner("volume", volume, docker_owner)
        _run([
            "docker", "run", "--detach", "--name", container,
            "--label", f"{DOCKER_OWNER_LABEL}={docker_owner}",
            "--publish", f"127.0.0.1:{port}:9000",
            "--env-file", str(docker_env),
            "--volume", f"{volume}:/data",
            "--volume", f"{certs}:/certs:ro",
            IMAGE, "server", "--address", ":9000", "--tls-path", "/certs",
            "--region", "us-east-1", "/data",
        ])
        _assert_docker_owner("container", container, docker_owner)
        _wait_for_tls(state)
        _initialize_bucket(state)
        return _public_state(state)
    except Exception:
        if objects_prechecked:
            _remove_owned_quietly("container", container, docker_owner)
            _remove_owned_quietly("volume", volume, docker_owner)
        shutil.rmtree(state_dir, ignore_errors=True)
        raise


def _environment(state: dict[str, Any]) -> dict[str, str]:
    return {
        "FLEET_WORM_BUCKET": str(state["bucket"]),
        "FLEET_WORM_REGION": str(state["region"]),
        "FLEET_WORM_ENDPOINT": str(state["endpoint"]),
        "FLEET_WORM_CA_FILE": str(state["ca_file"]),
        "FLEET_WORM_RETENTION_DAYS": str(state["retention_days"]),
        "FLEET_WORM_BACKEND_NAME": str(state["backend_name"]),
        "AWS_ACCESS_KEY_ID": str(state["access_key"]),
        "AWS_SECRET_ACCESS_KEY": str(state["secret_key"]),
    }


@contextmanager
def _configured_environment(state: dict[str, Any]) -> Iterator[None]:
    values = _environment(state)
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _force_stop(lifecycle: fleet_audit_client.AuditLifecycle) -> None:
    try:
        value = json.loads(lifecycle.lifecycle_path.read_text(encoding="utf-8"))
        if value.get("stopped_at") is not None:
            lifecycle.socket_path.unlink(missing_ok=True)
            return
        pid = int(value["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    process = fleet_audit_client._LIVE_PROCESSES.pop(pid, None)
    if process is not None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    lifecycle.socket_path.unlink(missing_ok=True)


def _prove_delete(
    sink: audit.S3ObjectLockSink,
    object_key: str,
    version_id: str,
    event_sha256: str,
) -> dict[str, Any]:
    try:
        _s3_call(
            sink, "DELETE", object_key, query=(("versionId", version_id),)
        )
    except urllib.error.HTTPError as exc:
        delete_status = exc.code
        try:
            body = exc.read(4096)
        finally:
            exc.close()
    else:
        raise LocalWormError("exact retained object version was deleted")
    error_code = _xml_value(body, "Code")
    error_message = _xml_value(body, "Message")
    retention_evidence = (error_code + " " + error_message).lower()
    if delete_status != 403 or not any(
        marker in retention_evidence for marker in ("worm", "retention", "locked")
    ):
        raise LocalWormError(
            f"delete failed for a reason not attributable to retention: {delete_status} {error_code}"
        )
    status, headers, _ = _s3_call(
        sink, "HEAD", object_key, query=(("versionId", version_id),)
    )
    expected = {
        "x-amz-version-id": version_id,
        "x-amz-object-lock-mode": "COMPLIANCE",
        "x-amz-meta-event-sha256": event_sha256,
    }
    if status != 200 or any(headers.get(key) != value for key, value in expected.items()):
        raise LocalWormError("retained exact version is not present after rejected delete")
    if not headers.get("x-amz-object-lock-retain-until-date"):
        raise LocalWormError("retained exact version lacks retain-until after delete")
    return {
        "status": delete_status,
        "error_code": error_code,
        "retention_message_sha256": hashlib.sha256(error_message.encode()).hexdigest(),
        "version_present": True,
        "head_status": status,
    }


def smoke(state_dir: Path) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    state = _load_state(state_dir)
    bucket = _bucket_status(state)
    smoke_root = state_dir / "smoke" / uuid.uuid4().hex
    target = smoke_root / "target"
    runs = smoke_root / "runs"
    target.mkdir(parents=True, mode=0o700)
    lifecycle: fleet_audit_client.AuditLifecycle | None = None
    with _configured_environment(state):
        try:
            nonce = uuid.uuid4().hex[:12]
            feature = f"local-worm-{nonce}"
            compiled = workflow_config.compile_path(ROOT / "workflows" / "local-worm.yaml")
            mission_id, _ = fleet_mission.create_mission(
                runs,
                compiled=compiled,
                feature=feature,
                objective="prove local Object Lock COMPLIANCE through AuditLifecycle",
                target_repo=target.resolve(),
                base_sha="0" * 40,
                idempotency_key=f"create:{feature}",
            )
            manifest = smoke_root / "fleet-local-worm.manifest"
            _write(
                manifest,
                (
                    f"feature={feature}\nmission_id={mission_id}\n"
                    f"target_repo={target.resolve()}\npreset=fleet_dialogue\nmode=assured\n"
                ).encode("utf-8"),
                0o600,
            )
            lifecycle = fleet_audit_client.AuditLifecycle(runs, mission_id)
            preflight = lifecycle.preflight()
            lifecycle.start(manifest)
            current = fleet_mission.load_state(runs, mission_id)
            lifecycle.record_control_event(
                event_type="LocalWormLiveSmoke",
                subject_id=mission_id,
                subject_sha256=current["head_sha256"],
                metadata={"backend": "rustfs", "trust_scope": "local-development"},
                idempotency_key="local-worm:live-smoke",
            )
            verified = lifecycle.verify()
            stopped = lifecycle.stop()
            ledger_path = lifecycle.ledger_root / mission_id / "a2a_ledger.jsonl"
            events = fleet_audit_client.read_verified_public_chain(ledger_path)
            last = events[-1]
            anchor_path = lifecycle.anchor_receipts / f"{last['event_id']}.json"
            anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
            required_anchor = {
                "worm": True,
                "trust_scope": "local-development",
                "backend": BACKEND_NAME,
                "object_key": last["worm_object_key"],
                "event_sha256": last["event_sha256"],
                "retention_mode": "COMPLIANCE",
            }
            if any(anchor.get(key) != value for key, value in required_anchor.items()):
                raise LocalWormError("live anchor receipt differs from ledger or policy")
            if not anchor.get("version_id") or not anchor.get("retained_until"):
                raise LocalWormError("live anchor receipt lacks version or retention")
            sink = _sink(state)
            delete = _prove_delete(
                sink,
                str(anchor["object_key"]),
                str(anchor["version_id"]),
                str(anchor["event_sha256"]),
            )

            regulated_compiled = workflow_config.compile_path(
                ROOT / "workflows" / "regulated.yaml"
            )
            regulated_id, _ = fleet_mission.create_mission(
                runs,
                compiled=regulated_compiled,
                feature=f"regulated-negative-{nonce}",
                objective="reject local endpoint for external compliance",
                target_repo=target.resolve(),
                base_sha="0" * 40,
                idempotency_key=f"create:regulated-negative:{nonce}",
            )
            regulated = fleet_audit_client.AuditLifecycle(runs, regulated_id)
            try:
                regulated.preflight()
            except fleet_audit_client.AuditClientError as exc:
                negative_error = str(exc)
            else:
                raise LocalWormError("regulated preflight accepted a loopback endpoint")
            if "public global" not in negative_error or regulated.root.exists():
                raise LocalWormError("regulated local-endpoint rejection was not fail-closed")

            evidence = {
                "schema_version": 1,
                "backend": BACKEND_NAME,
                "image": IMAGE,
                "endpoint": state["endpoint"],
                "bucket": state["bucket"],
                "bucket_status": bucket,
                "preflight": preflight,
                "mission_id": mission_id,
                "records": verified["records"],
                "worm": verified["worm"],
                "trust_scope": verified["trust_scope"],
                "verification_valid": verified["valid"],
                "service_stopped": stopped["stopped"],
                "anchor_receipt": anchor,
                "delete": delete,
                "regulated_negative": {
                    "rejected": True,
                    "audit_state_created": regulated.root.exists(),
                    "error_sha256": hashlib.sha256(negative_error.encode()).hexdigest(),
                    "reason": "endpoint does not resolve only to public global addresses",
                },
            }
            _write(
                state_dir / EVIDENCE_FILE,
                (json.dumps(evidence, separators=(",", ":"), sort_keys=True) + "\n").encode(),
                0o600,
            )
            return evidence
        except Exception:
            if lifecycle is not None:
                _force_stop(lifecycle)
            raise


def delete_test(state_dir: Path) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    state = _load_state(state_dir)
    try:
        evidence = json.loads((state_dir / EVIDENCE_FILE).read_text(encoding="utf-8"))
        anchor = evidence["anchor_receipt"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError) as exc:
        raise LocalWormError("run smoke before delete-test") from exc
    return _prove_delete(
        _sink(state),
        str(anchor["object_key"]),
        str(anchor["version_id"]),
        str(anchor["event_sha256"]),
    )


def teardown(state_dir: Path) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    if not state_dir.exists():
        return {"removed": False, "state_dir": str(state_dir)}
    state = _load_state(state_dir)
    container = str(state["container"])
    volume = str(state["volume"])
    owner = str(state["docker_owner"])
    expected_container, expected_volume = _names(state_dir)
    if container != expected_container or volume != expected_volume:
        raise LocalWormError("refusing teardown because Docker object identity differs")
    _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    container_exists = _docker_exists("container", container)
    volume_exists = _docker_exists("volume", volume)
    if container_exists:
        _assert_docker_owner("container", container, owner)
    if volume_exists:
        _assert_docker_owner("volume", volume, owner)
    if container_exists:
        _run(["docker", "rm", "--force", container], timeout=60)
    if volume_exists:
        _run(["docker", "volume", "rm", volume], timeout=60)
    shutil.rmtree(state_dir)
    return {
        "removed": True,
        "container": container,
        "volume": volume,
        "state_dir": str(state_dir),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup")
    setup_parser.add_argument("--port", type=int, default=9443)
    commands.add_parser("smoke")
    commands.add_parser("delete-test")
    commands.add_parser("teardown")
    all_parser = commands.add_parser("all")
    all_parser.add_argument("--port", type=int, default=9443)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "setup":
            result: Any = setup(args.state_dir, args.port)
        elif args.command == "smoke":
            result = smoke(args.state_dir)
        elif args.command == "delete-test":
            result = delete_test(args.state_dir)
        elif args.command == "teardown":
            result = teardown(args.state_dir)
        else:
            setup_result = setup(args.state_dir, args.port)
            try:
                smoke_result = smoke(args.state_dir)
            finally:
                teardown_result = teardown(args.state_dir)
            result = {
                "setup": setup_result,
                "smoke": smoke_result,
                "teardown": teardown_result,
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (LocalWormError, OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
        print(f"local-worm: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
