"""Lease adversaries use actual flock/fork and exact local process identities."""
import copy
import fcntl
import json
import os
from pathlib import Path
import pickle
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fleet_harness_control as control


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="fleet-control-lease-")
        self.root=Path(self.temp.name)/"campaign";self.root.mkdir(mode=0o700)
        self.plan=control.prepare(self.root,campaign_sha256="a"*64,seconds=60,launch={})
        self.pin=control.pin(self.plan)

    def tearDown(self):self.temp.cleanup()

    def lease(self):return control.OwnedLease(self.root,self.pin)

    def test_duplicate_controller_is_busy_and_guard_is_not_a_receipt(self):
        with self.lease() as lease:
            lease.assert_owned()
            with self.assertRaises(BlockingIOError):
                with self.lease():pass
            with self.assertRaisesRegex(control.AuthorityError,"launch"):lease.assert_effect()
            for fn in (copy.copy,copy.deepcopy,pickle.dumps):
                with self.assertRaises(TypeError):fn(lease)
        with self.assertRaises(control.AuthorityError):lease.assert_owned()
        with self.assertRaisesRegex(control.AuthorityError,"reused"):
            with lease:pass
        with self.lease() as resumed:self.assertNotEqual(resumed.generation["id"],lease.generation["id"])

    def test_fork_invalidates_child_without_unlocking_parent(self):
        with self.lease() as lease:
            rfd,wfd=os.pipe();pid=os.fork()
            if pid==0:
                os.close(rfd)
                result={"guard_denied":False,"parent_still_locked":False}
                try:
                    try:lease.assert_owned()
                    except control.AuthorityError:result["guard_denied"]=True
                    descriptor=os.open(self.root/".control-lease.lock",os.O_RDWR)
                    try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:result["parent_still_locked"]=True
                    finally:os.close(descriptor)
                    os.write(wfd,json.dumps(result).encode())
                finally:os._exit(0)
            os.close(wfd)
            try:observed=json.loads(os.read(rfd,4096));_,status=os.waitpid(pid,0)
            finally:os.close(rfd)
            self.assertEqual(status,0)
            self.assertEqual(observed,{"guard_denied":True,"parent_still_locked":True})
            lease.assert_owned()

    def test_copied_root_cannot_multiply_budget_authority(self):
        with self.lease():pass
        copied=self.root.parent/"copy";shutil.copytree(self.root,copied)
        with self.assertRaises(control.AuthorityError):control.OwnedLease(copied,self.pin)
        # Even rewriting the path with a newly computed pin leaves the old inode binding.
        plan=control.read(copied,"control-plan.json");plan["root"]=str(copied)
        (copied/"control-plan.json").write_text(json.dumps(plan))
        with self.assertRaises(control.AuthorityError):
            with control.OwnedLease(copied,control.pin(plan)):pass

    def test_lock_and_root_replacement_are_detected_before_effect(self):
        with self.lease() as lease:
            leaf=self.root/".control-lease.lock";saved=self.root/"saved-lock"
            leaf.rename(saved)
            try:
                fd=os.open(leaf,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600);os.close(fd)
                with self.assertRaises(control.AuthorityError):lease.assert_owned()
            finally:leaf.unlink();saved.rename(leaf)
            moved=self.root.parent/"moved";self.root.rename(moved);self.root.mkdir(mode=0o700)
            try:
                with self.assertRaises(control.safe.SafePathError):lease.assert_owned()
            finally:self.root.rmdir();moved.rename(self.root)

    def test_recovery_does_not_restart_clock_and_boot_change_denies(self):
        with self.lease() as first:clock=copy.deepcopy(first.clock)
        with mock.patch.object(control.time,"time",return_value=clock["started_at"]-100):
            with self.lease() as second:
                self.assertEqual(second.clock,clock)
                second._bound={"synthetic_fixture":True,"generation_sha256":control.pin(second.generation)}
                with mock.patch.object(control.time,"monotonic",return_value=clock["monotonic_deadline"]+1):
                    with self.assertRaisesRegex(control.AuthorityError,"deadline"):second.assert_effect()
        with mock.patch.object(control,"boot_identity",return_value="different-boot"):
            with self.assertRaisesRegex(control.AuthorityError,"clock"):
                with self.lease():pass

    def test_cancel_is_durable_and_cleanup_remains_authorized(self):
        with self.lease() as lease:
            lease._bound={"synthetic_fixture":True,"generation_sha256":control.pin(lease.generation)}
            lease.begin_transport("exact-socket")
            record=control.cancel(self.root,self.pin,"test")
            self.assertEqual(control.cancel(self.root,self.pin,"duplicate"),record)
            with self.assertRaisesRegex(control.AuthorityError,"cancellation"):lease.assert_effect()
            with self.assertRaisesRegex(control.AuthorityError,"undrained"):lease.__exit__(None,None,None)
            with self.assertRaisesRegex(control.AuthorityError,"quiescence"):lease.end_transport("exact-socket",closed=False)
            lease.assert_owned();lease.end_transport("exact-socket",closed=True)
        with self.lease() as resumed:
            resumed._bound={"synthetic_fixture":True,"generation_sha256":control.pin(resumed.generation)}
            with self.assertRaisesRegex(control.AuthorityError,"cancellation"):resumed.assert_effect()

    def test_changed_birth_and_plan_are_denied(self):
        with self.lease() as lease:
            with mock.patch.object(control.runtime,"process_observation",return_value=(None,False)):
                with self.assertRaisesRegex(control.AuthorityError,"birth"):lease.assert_owned()
            original=(self.root/"control-plan.json").read_bytes()
            (self.root/"control-plan.json").write_bytes(original+b" ")  # identical JSON is not new authority
            lease.assert_owned()
            changed=copy.deepcopy(self.plan);changed["seconds"]=100
            try:
                (self.root/"control-plan.json").write_text(json.dumps(changed))
                with self.assertRaisesRegex(control.AuthorityError,"plan changed"):lease.assert_owned()
            finally:(self.root/"control-plan.json").write_bytes(original)

    def test_interrupted_clock_publication_recovers_original_time(self):
        original=control.sandbox.publish;captured={}
        def crash(root,name,value):
            if name=="control-clock.json":
                captured.update(value);raise KeyboardInterrupt()
            return original(root,name,value)
        with mock.patch.object(control.sandbox,"publish",side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                with self.lease():pass
        self.assertFalse((self.root/"control-clock.json").exists())
        with self.lease() as recovered:self.assertEqual(recovered.clock,captured)

    def test_old_generation_launch_cannot_authorize_new_generation(self):
        with self.lease() as first:old={"generation_sha256":control.pin(first.generation)}
        with self.lease() as second:
            second._bound=old
            with self.assertRaisesRegex(control.AuthorityError,"generation"):second.begin_transport("x")

    def test_dead_first_generation_recovers_without_release_directory(self):
        child=os.fork()
        if child==0:
            with self.lease():os._exit(91)
        _,status=os.waitpid(child,0);self.assertEqual(os.waitstatus_to_exitcode(status),91)
        self.assertFalse((self.root/"control-releases").exists())
        with self.lease() as recovered:
            self.assertEqual(len(recovered.generation["previous"]),1)
            recovered.assert_owned()

    def test_cancel_committed_before_path_stops_existing_guard(self):
        with self.lease() as lease:
            lease._bound={"generation_sha256":control.pin(lease.generation)}
            child=os.fork()
            if child==0:
                original=control.sandbox.publish
                def crash(root,name,value):
                    if name=="control-cancel.json":os._exit(91)
                    return original(root,name,value)
                control.sandbox.publish=crash
                control.cancel(self.root,self.pin,"cancel before path")
                os._exit(92)
            _,status=os.waitpid(child,0);self.assertEqual(os.waitstatus_to_exitcode(status),91)
            self.assertFalse((self.root/"control-cancel.json").exists())
            with self.assertRaisesRegex(control.AuthorityError,"cancellation committed"):lease.assert_effect()

    def test_release_retry_reuses_original_cas_timestamp(self):
        lease=self.lease().__enter__();original=control.sandbox.publish
        def crash(root,name,value):
            if name.startswith("control-releases/"):raise OSError("publication interrupted")
            return original(root,name,value)
        with mock.patch.object(control.sandbox,"publish",side_effect=crash),self.assertRaises(OSError):lease.__exit__(None,None,None)
        lease.__exit__(None,None,None)
        with self.lease() as recovered:recovered.assert_owned()


if __name__=="__main__":unittest.main()
