"""Real OS canaries with synthetic inputs; no Git commits or live providers."""
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleet_native_sandbox as sandbox
import fleet_herdr_effects as effects
from tests.repo_outputs import repo_outputs


class ContractTests(unittest.TestCase):
    def test_invalid_inputs_fail_before_launch(self):
        with mock.patch.object(sandbox, "_capture") as capture:
            for files in ({"../escape": b"x"}, {"/escape": b"x"}, {"a//b": b"x"},
                          {"a": b"x", "a/b": b"x"}, {"a": "text"}, {"a\0": b"x"}):
                with self.subTest(files=files), self.assertRaises(sandbox.SandboxError):
                    sandbox.execute(b"pass", files=files, role="build", parent=ROOT)
            for bounds in ({"timeout": float("nan")}, {"timeout": True}, {"timeout": 61},
                           {"max_output": False}, {"max_output": 0}):
                with self.subTest(bounds=bounds), self.assertRaises(sandbox.SandboxError):
                    sandbox.execute(b"pass", files={}, role="build", parent=ROOT, **bounds)
            capture.assert_not_called()

    def test_profile_injection_and_unknown_roles_rejected(self):
        for path in ('/tmp/evil")) (allow default)', "/tmp/evil\n", "/tmp/evil\\"):
            with self.assertRaises(sandbox.SandboxError):
                sandbox.profile(Path(path), Path("/runtime/image"), Path("/runtime"), "build")
        with self.assertRaises(sandbox.SandboxError):
            sandbox.profile(Path("/tmp/capsule"), Path("/runtime/image"), Path("/runtime"), "owner")

    def test_unsupported_host_fails_closed(self):
        with mock.patch.object(sandbox.sys, "platform", "linux"), \
                mock.patch.object(sandbox, "_capture") as capture:
            with self.assertRaisesRegex(sandbox.SandboxError, "no unsandboxed fallback"):
                sandbox.execute(b"pass", files={}, role="build", parent=ROOT)
            capture.assert_not_called()

    def test_local_report_and_environment_never_open_herdr(self):
        with mock.patch.dict(os.environ, {"FLEET_NATIVE_MEDIATION": "fleet.native.offline.v1"}):
            with self.assertRaises(effects.EffectMediationDenied):
                effects.require_native_mediation()

    def test_export_rejects_replaced_root_before_reading_host(self):
        with tempfile.TemporaryDirectory(dir=repo_outputs()) as temp:
            root = Path(temp) / "work"
            root.mkdir()
            identity = (root.stat().st_dev, root.stat().st_ino)
            root.rmdir()
            root.symlink_to(ROOT, target_is_directory=True)
            with self.assertRaisesRegex(sandbox.SandboxError, "root was replaced"):
                sandbox._collect(root, identity)


@unittest.skipUnless(sys.platform == "darwin" and sandbox.SANDBOX_EXEC.exists(), "macOS OS lane required")
class SeatbeltTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-os-test-", dir=repo_outputs())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def execute(self, code, role="build", files=None, **bounds):
        result = sandbox.execute(code.encode(), files={} if files is None else files,
                                 role=role, parent=self.root, **bounds)
        self.assertFalse(list(self.root.glob("native-capsule-*")), "capsule was not cleaned")
        return result

    def success(self, code, **kwargs):
        result = self.execute(code, **kwargs)
        self.assertEqual(result.report["execution_status"], "exited", result.stderr)
        self.assertEqual(result.report["returncode"], 0, result.stderr)
        return result

    def test_worker_reads_explicit_inputs_and_exports_exact_candidate(self):
        result = self.success("from pathlib import Path\n"
            "Path('answer.txt').write_bytes(Path('input/data.txt').read_bytes().upper())\n"
            "print('finished')", files={"input/data.txt": b"explicit input\n"})
        self.assertEqual(result.files, {"input/data.txt": b"explicit input\n", "answer.txt": b"EXPLICIT INPUT\n"})
        self.assertEqual(result.stdout, b"finished\n")
        self.assertEqual(result.report["files"]["answer.txt"], sandbox._hash(b"EXPLICIT INPUT\n"))
        self.assertEqual(result.report["authority"], "none")
        self.assertEqual(result.report["mission_success"], "NOT_VERIFIED")
        self.assertEqual(result.report["profile_sha256"], sandbox._hash(result.report["profile"].encode()))

    def test_every_reader_has_read_only_candidate_and_private_scratch(self):
        code = """import os, json
from pathlib import Path
assert Path('input').read_bytes() == b'original'
Path(os.environ['TMPDIR'], 'scratch').write_text('temporary')
try:
    Path('input').write_text('changed')
except OSError as error:
    print(json.dumps({'errno': error.errno}))
else:
    raise AssertionError('candidate write was allowed')
"""
        for role in sorted(sandbox.READERS):
            with self.subTest(role=role):
                result = self.success(code, role=role, files={"input": b"original"})
                self.assertEqual(json.loads(result.stdout)["errno"], errno.EPERM)
                self.assertEqual(result.files, {"input": b"original"})

    def test_host_effects_positive_controls_then_os_denial(self):
        secret = self.root / "synthetic-credential"
        secret.write_text("SYNTHETIC-ONLY")
        marker = self.root / "external-marker"
        child_marker = self.root / "child-marker"
        image, _ = sandbox.runtime()
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.1)
        endpoint = listener.getsockname()
        source = f"""import ctypes, json, os, socket, subprocess
from pathlib import Path
results = {{}}
def probe(name, operation):
    try:
        operation()
        results[name] = 'ALLOWED'
    except OSError as error:
        results[name] = error.errno
def native_read():
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.open({str(secret).encode()!r}, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'native open denied')
    os.close(fd)
def child():
    pid = os.fork()
    if pid == 0:
        Path({str(child_marker)!r}).write_text('fork')
        os._exit(0)
    os.waitpid(pid, 0)
def spawn():
    pid = os.posix_spawn({str(image)!r}, [{str(image)!r}, '-I', '-S', '-c', 'pass'], {{}})
    os.waitpid(pid, 0)
probe('filesystem', lambda: Path({str(marker)!r}).write_text('write'))
probe('credentials', lambda: Path({str(secret)!r}).read_bytes())
probe('volume_alias', lambda: Path('/System/Volumes/Data' + {str(secret)!r}).read_bytes())
probe('native_credentials', native_read)
probe('network', lambda: socket.create_connection({endpoint!r}, timeout=1).close())
probe('fork', child)
probe('self_spawn', spawn)
probe('other_exec', lambda: subprocess.run(['/usr/bin/true'], check=True))
print(json.dumps(results))
"""
        positive = subprocess.run([str(image), "-I", "-S", "-B", "-c", source],
                                  capture_output=True, timeout=5, cwd=self.root, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(positive.returncode, 0, positive.stderr)
        expected = {name: "ALLOWED" for name in ("filesystem", "credentials", "volume_alias", "native_credentials",
                                                "network", "fork", "self_spawn", "other_exec")}
        self.assertEqual(json.loads(positive.stdout), expected)
        self.assertEqual(marker.read_text(), "write")
        self.assertEqual(child_marker.read_text(), "fork")
        marker.unlink()
        child_marker.unlink()
        connection, _ = listener.accept()
        connection.close()
        result = self.success(source)
        self.assertEqual(json.loads(result.stdout), {name: errno.EPERM for name in expected})
        self.assertFalse(marker.exists())
        self.assertFalse(child_marker.exists())
        with self.assertRaises(socket.timeout):
            listener.accept()
        self.assertEqual(secret.read_text(), "SYNTHETIC-ONLY")

    def test_no_ambient_env_stdin_or_inheritable_descriptor(self):
        secret = self.root / "fd-secret"
        secret.write_bytes(b"SYNTHETIC-FD-ONLY")
        with secret.open("rb") as stream:
            os.set_inheritable(stream.fileno(), True)
            code = f"""import os, sys, json
value = {{'secret': os.getenv('SYNTHETIC_TEST_SECRET'), 'stdin': sys.stdin.buffer.read().decode()}}
try:
    value['fd'] = os.read({stream.fileno()}, 100).decode()
except OSError as error:
    value['fd_errno'] = error.errno
print(json.dumps(value))
"""
            with mock.patch.dict(os.environ, {"SYNTHETIC_TEST_SECRET": "SYNTHETIC-ENV-ONLY",
                                              "PYTHONPATH": str(self.root), "DYLD_INSERT_LIBRARIES": "/bogus"}):
                result = self.success(code)
        value = json.loads(result.stdout)
        self.assertIsNone(value["secret"])
        self.assertEqual(value["stdin"], "")
        self.assertEqual(value["fd_errno"], errno.EBADF)
        self.assertNotIn(b"SYNTHETIC", result.stdout + result.stderr)

    def test_symlink_escape_denied_and_unsafe_outputs_not_exported(self):
        secret = self.root / "outside"
        secret.write_text("SYNTHETIC")
        code = f"""import os, json
from pathlib import Path
os.symlink({str(secret)!r}, 'link')
errors = []
for operation in (lambda: Path('link').read_bytes(), lambda: Path('link').write_text('changed')):
    try:
        operation()
    except OSError as error:
        errors.append(error.errno)
assert errors == [1, 1], errors
os.unlink('link')
print('denied')
"""
        self.assertEqual(self.success(code).stdout, b"denied\n")
        self.assertEqual(secret.read_text(), "SYNTHETIC")
        with self.assertRaisesRegex(sandbox.SandboxError, "unsafe output"):
            self.execute(f"import os\nos.symlink({str(secret)!r}, 'link')")
        self.assertFalse(list(self.root.glob("native-capsule-*")))

    def test_self_exec_keeps_the_policy(self):
        secret = self.root / "outside"
        secret.write_text("SYNTHETIC")
        second = f"""from pathlib import Path
try:
    Path({str(secret)!r}).read_bytes()
except OSError as error:
    assert error.errno == 1
    print('still confined')
else:
    raise AssertionError('self exec escaped')
"""
        result = self.success(f"import os, sys\nos.execve(sys.executable, [sys.executable, '-I', '-S', '-c', {second!r}], {{}})")
        self.assertEqual(result.stdout, b"still confined\n")

    def test_hardlink_and_unix_socket_cannot_bridge_host_resources(self):
        secret = self.root / "outside"
        secret.write_text("SYNTHETIC")
        # Keep AF_UNIX pathname below the macOS sockaddr limit.
        with tempfile.TemporaryDirectory(prefix="fleet-ipc-") as temp:
            endpoint = str(Path(temp) / "socket")
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(endpoint)
            listener.listen(2)
            listener.settimeout(0.1)
            source = f"""import os, socket, json
results = {{}}
try:
    os.link({str(secret)!r}, 'link')
except OSError as error:
    results['hardlink'] = error.errno
try:
    sock = socket.socket(socket.AF_UNIX)
    sock.connect({endpoint!r})
except OSError as error:
    results['unix_socket'] = error.errno
print(json.dumps(results))
"""
            result = self.success(source)
            self.assertEqual(json.loads(result.stdout), {"hardlink": errno.EPERM, "unix_socket": errno.EPERM})
            with self.assertRaises(socket.timeout):
                listener.accept()
            listener.close()

    def test_timeout_output_limits_and_nonzero_never_export(self):
        result = self.execute("import time\nfrom pathlib import Path\nPath('partial').write_text('x')\ntime.sleep(5)", timeout=0.3)
        self.assertEqual(result.report["execution_status"], "timed_out")
        self.assertLess(result.report["returncode"], 0)
        self.assertEqual(result.files, {})
        result = self.execute("import os\nwhile True: os.write(1, b'x' * 4096)", max_output=1000)
        self.assertEqual(result.report["execution_status"], "output_limit")
        self.assertEqual(len(result.stdout) + len(result.stderr), 1000)
        self.assertEqual(result.files, {})
        result = self.execute("from pathlib import Path\nPath('partial').write_text('x')\nraise RuntimeError('failed')")
        self.assertEqual(result.report["returncode"], 1)
        self.assertEqual(result.files, {})

    def test_invalid_os_policy_has_no_unsandboxed_retry(self):
        with mock.patch.object(sandbox, "profile", return_value="(invalid profile"):
            result = self.execute("print('UNCONFINED')")
        self.assertNotEqual(result.report["returncode"], 0)
        self.assertNotIn(b"UNCONFINED", result.stdout)
        self.assertEqual(result.files, {})

    def test_input_snapshot_must_match_before_process_start(self):
        original = sandbox._collect
        def changed_input(root, identity):
            snapshot = original(root, identity)
            snapshot['input'] = b'changed'
            return snapshot
        with mock.patch.object(sandbox, "_collect", side_effect=changed_input), \
                mock.patch.object(sandbox, "_capture") as capture:
            with self.assertRaisesRegex(sandbox.SandboxError, "input names collide"):
                self.execute("print('must not start')", files={"input": b"original"})
            capture.assert_not_called()

    def test_sysctl_cannot_obtain_process_arguments_or_environment(self):
        # CTL_KERN=1 and KERN_PROCARGS2=49 from the installed macOS SDK.
        # Target an owned process with ONLY synthetic environment, never the
        # test runner/controller. Querying one's own sanitized env is harmless
        # and macOS allows that even under the restrictive policy.
        image, _ = sandbox.runtime()
        with subprocess.Popen([str(image), '-I', '-S', '-c', 'import time; time.sleep(10)'],
                              env={'SYNTHETIC_KEY': 'SYNTHETIC-PROCESS-SECRET'}) as target:
            try:
                code = f"""import ctypes, json
libc = ctypes.CDLL(None, use_errno=True)
mib = (ctypes.c_int * 3)(1, 49, {target.pid})
buf = ctypes.create_string_buffer(262144)
size = ctypes.c_size_t(len(buf))
ret = libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0)
print(json.dumps({{'ret': ret, 'errno': ctypes.get_errno(),
                  'secret_present': b'SYNTHETIC-PROCESS-SECRET' in buf.raw}}))
"""
                positive = subprocess.run([str(image), '-I', '-S', '-c', code], env={},
                                          capture_output=True, timeout=5)
                self.assertEqual(positive.returncode, 0, positive.stderr)
                control = json.loads(positive.stdout)
                self.assertEqual(control['ret'], 0)
                self.assertTrue(control['secret_present'])
                value = json.loads(self.success(code).stdout)
                self.assertEqual(value, {'ret': -1, 'errno': errno.EPERM, 'secret_present': False})
            finally:
                target.terminate()
                target.wait(timeout=5)

    def test_host_signal_and_candidate_executable_mapping_denied(self):
        code = """import json, mmap, os
from pathlib import Path
errors = {}
try:
    os.kill(os.getppid(), 0)  # Permission probe only; never delivers a signal.
except OSError as error:
    errors['host_signal'] = error.errno
Path('data').write_bytes(b'0' * 4096)
with open('data', 'rb') as stream:
    try:
        mapping = mmap.mmap(stream.fileno(), 4096, prot=mmap.PROT_READ | mmap.PROT_EXEC)
    except OSError as error:
        errors['candidate_exec_mapping'] = error.errno
    else:
        mapping.close()
print(json.dumps(errors))
"""
        value = json.loads(self.success(code).stdout)
        self.assertEqual(value, {'host_signal': errno.EPERM, 'candidate_exec_mapping': errno.EPERM})

    def test_interrupt_still_reaps_process_after_it_closes_both_output_pipes(self):
        started = time.monotonic()
        result = self.execute("import os, time\nos.close(1)\nos.close(2)\ntime.sleep(10)",
                              interrupt=lambda: "cancel_requested" if time.monotonic() - started > 0.15 else None)
        self.assertEqual(result.report["execution_status"], "interrupted")
        self.assertEqual(result.report["interruption_reason"], "cancel_requested")
        self.assertLess(result.report["returncode"], 0)
        self.assertTrue(result.report["cleanup_confirmed"])
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
