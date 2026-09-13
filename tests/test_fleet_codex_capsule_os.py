"""Real Seatbelt checks with a compiled local canary, never a provider."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import fleet_codex_sandbox as capsule

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/clang').is_file(),
                     'existing macOS compiler and Seatbelt required')
class BoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='capsule-canary-', dir=ROOT/'outputs')
        cls.addClassCleanup(cls.build.cleanup)
        cls.image = Path(cls.build.name)/'canary'
        subprocess.run(['/usr/bin/clang', '-Wall', '-Wextra', '-Werror',
            str(ROOT/'tests/fixtures/codex_capsule_canary.c'), '-o', str(cls.image)],
            check=True, capture_output=True, timeout=30)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='capsule-os-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.private = self.root/'capsule'
        for name in ('bin', 'home', 'scratch', 'work'):
            (self.private/name).mkdir(parents=True)
        self.image_copy = self.private/'bin/codex'
        shutil.copyfile(self.image, self.image_copy)
        self.image_copy.chmod(0o500)
        self.processes = capsule.Processes([self.image_copy])
        self.addCleanup(self.stop)

    def stop(self):
        self.assertTrue(self.processes.stop())

    def listener(self, family=socket.AF_INET, address=None):
        sock = socket.socket(family)
        self.addCleanup(sock.close)
        sock.bind(address or ('127.0.0.1', 0)); sock.listen(8)
        sock.settimeout(.05)
        return sock

    def test_real_policy_denies_host_effects_and_preserves_broker_port(self):
        secret = self.root/'synthetic-secret'; secret.write_text('SYNTHETIC_ONLY')
        marker = self.root/'outside-write'
        allowed, denied = self.listener(), self.listener()
        unix_path = str(self.root/'control.sock')
        unix = self.listener(socket.AF_UNIX, unix_path)
        user_shell = self.private/'work/user-shell'
        shutil.copyfile('/bin/sh', user_shell); user_shell.chmod(0o700)
        args = [str(secret), str(marker), str(allowed.getsockname()[1]),
            str(denied.getsockname()[1]), unix_path, str(os.getpid()), str(user_shell)]
        positive = subprocess.run([str(self.image_copy), *args], capture_output=True, check=True, timeout=5)
        self.assertEqual(set(json.loads(positive.stdout).values()), {1})
        marker.unlink()
        for sock in (allowed, denied, unix):
            conn, _ = sock.accept(); conn.close()
        policy = capsule.profile(self.private, 'build', allowed.getsockname()[1])
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', policy, str(self.image_copy), *args],
            cwd=self.private/'work', env={}, capture_output=True, check=True, timeout=5)
        self.assertEqual(json.loads(result.stdout), {'read':0,'write':0,'allowed_port':1,
            'denied_port':0,'unix':0,'parent_procargs':0,'shell':0,'fork_read':0})
        self.assertFalse(marker.exists())
        conn, _ = allowed.accept(); conn.close()
        for sock in (denied, unix):
            with self.assertRaises(TimeoutError): sock.accept()

    def test_detached_grandchild_is_stopped_without_touching_unrelated_image(self):
        unrelated = subprocess.Popen([str(self.image), 'hold'], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup_unrelated():
            if unrelated.poll() is None: unrelated.kill()
            unrelated.wait(timeout=3)
        self.addCleanup(cleanup_unrelated)
        pid_file = self.private/'work/daemon.pid'
        policy = capsule.profile(self.private, 'build', 65534)
        subprocess.run(['/usr/bin/sandbox-exec', '-p', policy, str(self.image_copy),
            'daemon', str(pid_file)], env={}, check=True, timeout=3)
        deadline = time.monotonic()+2
        while not pid_file.exists() and time.monotonic() < deadline: time.sleep(.01)
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        self.assertIn(pid, {p['pid'] for p in self.processes.snapshot()})
        self.assertTrue(self.processes.stop())
        self.assertEqual(self.processes.snapshot(), [])
        self.assertIsNone(unrelated.poll())


if __name__ == '__main__': unittest.main()
