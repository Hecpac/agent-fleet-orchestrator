"""Offline transport preparation only; no DNS service or model calls."""
import copy
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fleet_harness_https as https


class HTTPSTests(unittest.TestCase):
    def policy(self):
        now=time.time()
        return {"version":https.VERSION,"endpoint":https.ENDPOINT,"method":"POST","hostname":https.HOST,"port":443,
            "family":int(socket.AF_INET),"address":"1.1.1.1","resolved_at":now-1,"expires_at":now+30,
            "owner":str(uuid.uuid4()),"resolution_sha256":"a"*64,"tls":"CERT_REQUIRED+check_hostname"}

    def delayed_response(self,phase,delay,*,seconds,stopped=lambda:False,dns_seconds=50):
        client,peer=socket.socketpair();client.settimeout(50);peer.settimeout(2)
        policy=self.policy();policy["expires_at"]=time.time()+dns_seconds
        connection=https.NumericHTTPS(policy,deadline_at=time.time()+seconds,stopped=stopped)
        release=threading.Event();received=[];errors=[]
        def connect():connection.sock=client;connection.active_socket=client
        def serve():
            try:
                raw=b""
                while b"\r\n\r\n" not in raw:raw+=peer.recv(4096)
                while len(raw.partition(b"\r\n\r\n")[2])<2:raw+=peer.recv(4096)
                received.append(raw)
                header=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: "+(b"keep-alive" if phase=="keepalive-body" else b"close")+b"\r\n\r\n"
                if phase=="headers":release.wait(delay);peer.sendall(header+b"{}")
                else:peer.sendall(header);release.wait(delay);peer.sendall(b"{}")
            except OSError as exc:errors.append(type(exc).__name__)
            finally:peer.close()
        thread=threading.Thread(target=serve);thread.start()
        return connection,connect,thread,release,received,errors

    @unittest.skipUnless(os.environ.get("FLEET_HARNESS_LOCAL_TESTS")=="1","explicit local slow HTTP regression")
    def test_response_silence_over_30_seconds_keeps_one_original_request(self):
        # Real wall time reproduces the provider failure; all three socket
        # ownership paths run concurrently and never contact a provider.
        results={}
        def probe(phase):
            connection,connect,thread,release,received,errors=self.delayed_response(phase,31,seconds=45)
            start=time.monotonic();original=(connection.deadline_at,connection.monotonic_deadline)
            try:
                with mock.patch.object(connection,"connect",connect):result=connection.exchange(b"{}")
                results[phase]={"result":result,"outcome":connection.retained_outcome(),"requests":len(received),
                    "elapsed":time.monotonic()-start,"same_deadlines":original==(connection.deadline_at,connection.monotonic_deadline),"errors":errors}
            except BaseException as exc:results[phase]={"failure":repr(exc)}
            finally:release.set();connection.abort();thread.join(2)
        workers=[threading.Thread(target=probe,args=(phase,)) for phase in ("headers","close-body","keepalive-body")]
        for worker in workers:worker.start()
        for worker in workers:worker.join(50)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(set(results),{"headers","close-body","keepalive-body"})
        for phase,value in results.items():
            with self.subTest(phase=phase):
                self.assertNotIn("failure",value)
                self.assertEqual(value["result"],{"http_status":200,"body":b"{}"})
                self.assertTrue(value["outcome"]["response_complete"] and value["outcome"]["transport_closed"])
                self.assertTrue(value["same_deadlines"]);self.assertEqual(value["requests"],1)
                self.assertGreaterEqual(value["elapsed"],30);self.assertLess(value["elapsed"],45)
                self.assertEqual(value["errors"],[])

    def test_revocation_interrupts_silent_headers_and_transferred_body(self):
        for phase in ("headers","close-body","keepalive-body"):
            with self.subTest(phase=phase):
                revoked=threading.Event()
                connection,connect,thread,release,received,_=self.delayed_response(phase,1,seconds=10,stopped=revoked.is_set)
                timer=threading.Timer(.15,revoked.set);timer.start();start=time.monotonic()
                close_threads=[];original_close=connection.close
                def close():close_threads.append(threading.get_ident());original_close()
                try:
                    with mock.patch.object(connection,"connect",connect),mock.patch.object(connection,"close",side_effect=close):
                        with self.assertRaises((TimeoutError,OSError,https.http.client.HTTPException)):connection.exchange(b"{}")
                    self.assertLess(time.monotonic()-start,.8)
                    self.assertEqual(set(close_threads),{threading.get_ident()})
                    observed=connection.retained_outcome()
                    self.assertFalse(observed["response_complete"]);self.assertTrue(observed["transport_closed"])
                    self.assertEqual(len(received),1)
                    with self.assertRaisesRegex(ValueError,"cannot be repeated"):connection.exchange(b"{}")
                finally:timer.cancel();release.set();connection.abort();thread.join(2)

    def test_dns_expiry_bounds_silent_headers_and_body_before_task_deadline(self):
        for phase in ("headers","close-body"):
            with self.subTest(phase=phase):
                connection,connect,thread,release,received,_=self.delayed_response(phase,1,seconds=10,dns_seconds=.15)
                start=time.monotonic()
                try:
                    with mock.patch.object(connection,"connect",connect):
                        with self.assertRaises((TimeoutError,ValueError,OSError,https.http.client.HTTPException)):connection.exchange(b"{}")
                    self.assertLess(time.monotonic()-start,.8)
                    observed=connection.retained_outcome()
                    self.assertFalse(observed["response_complete"]);self.assertTrue(observed["transport_closed"])
                    self.assertEqual(len(received),1)
                finally:release.set();connection.abort();thread.join(2)

    def test_endpoint_numeric_identity_and_freshness_are_fixed(self):
        for key,value in (("port",8443),("method","GET"),("hostname","localhost"),("address","127.0.0.1"),("address","api.deepseek.com"),("family",True),("tls","insecure"),("expires_at",0),("resolution_sha256","z"*64)):
            policy=self.policy();policy[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):https.validate(policy)

    def test_no_dns_fallback_and_revocation_at_each_socket_transfer(self):
        for phase in ("tcp","wrap","handshake",None):
            stopped=[False];events=[];raw=mock.MagicMock();wrapped=mock.MagicMock()
            def tcp(address):
                self.assertIs(connection.sock,raw);events.append(("tcp",address))
                if phase=="tcp":stopped[0]=True
            def wrap(sock,**kwargs):
                events.append(("wrap",kwargs));self.assertIs(sock,raw)
                if phase=="wrap":stopped[0]=True
                return wrapped
            def handshake():
                self.assertIs(connection.sock,wrapped);events.append(("handshake",None))
                if phase=="handshake":stopped[0]=True
            raw.connect.side_effect=tcp;wrapped.do_handshake.side_effect=handshake
            policy=self.policy();connection=https.NumericHTTPS(policy,deadline_at=time.time()+10,stopped=lambda:stopped[0])
            policy["address"]="8.8.8.8"  # caller cannot mutate the admitted copy
            context=mock.Mock(wrap_socket=wrap);connection._context=context
            with mock.patch.object(https.socket,"getaddrinfo",side_effect=AssertionError("DNS in active request")),mock.patch.object(https.socket,"socket",return_value=raw) as create:
                if phase is None:connection.connect()
                else:
                    with self.assertRaises(TimeoutError):connection.connect()
                self.assertEqual(create.call_count,1);self.assertEqual(raw.connect.call_count,1)
                self.assertEqual(events[0],("tcp",("1.1.1.1",443)))
                if phase!="tcp":self.assertEqual(events[1][1],{"server_hostname":https.HOST,"do_handshake_on_connect":False})
                if phase=="wrap":wrapped.do_handshake.assert_not_called()
                self.assertEqual(connection.auto_open,0)
            connection.close()

    def test_owned_resolver_is_bounded_reaped_and_has_no_credentials(self):
        # These helpers replace the DNS call, not the process/deadline/ownership boundary.
        with tempfile.TemporaryDirectory(prefix="fleet-dns-preparation-") as temp:
            root=Path(temp)
            code="import json,os,sys;assert set(os.environ).issubset({'PATH','LC_CTYPE','__CF_USER_TEXT_ENCODING'});print(json.dumps({'addresses':[[2,'1.1.1.1']],'owner':sys.argv[2]}))"
            with mock.patch.object(https,"RESOLVER",code):policy=https.prepare(root/"ok")
            self.assertEqual(https.validate(policy),policy)
            self.assertTrue(json.loads((root/"ok/cleanup.json").read_text())["reaped"])
            with mock.patch.object(https,"RESOLVER","import time;time.sleep(60)"):
                started=time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):https.prepare(root/"blocked",timeout=.15)
                self.assertLess(time.monotonic()-started,2)
            cleanup=json.loads((root/"blocked/cleanup.json").read_text())
            self.assertTrue(cleanup["reaped"]);self.assertTrue(cleanup["terminated"])
            self.assertFalse((root/"blocked/policy.json").exists())
            with self.assertRaises(FileExistsError):https.prepare(root/"blocked")

    def test_real_tls_checks_san_before_sending_any_credential(self):
        original_socket=socket.socket
        for hostname in (https.HOST,"wrong.invalid"):
            with self.subTest(hostname=hostname),tempfile.TemporaryDirectory(prefix="fleet-tls-preparation-") as temp:
                root=Path(temp);cert=root/"cert.pem";key=root/"key.pem"
                generated=subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-keyout",str(key),"-out",str(cert),"-subj","/CN="+hostname,"-addext","subjectAltName=DNS:"+hostname],capture_output=True,timeout=10)
                self.assertEqual(generated.returncode,0,generated.stderr)
                server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_context.load_cert_chain(cert,key)
                listener=original_socket();listener.bind(("127.0.0.1",0));listener.listen(1);listener.settimeout(3)
                address=listener.getsockname();received=[];errors=[]
                def serve():
                    try:
                        peer,_=listener.accept();peer.settimeout(2)
                        with server_context.wrap_socket(peer,server_side=True) as tls:
                            body=b""
                            while b"\r\n\r\n" not in body:body+=tls.recv(4096)
                            while len(body.partition(b"\r\n\r\n")[2])<2:body+=tls.recv(4096)
                            received.append(body)
                            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                    except (OSError,ssl.SSLError) as exc:errors.append(type(exc).__name__)
                thread=threading.Thread(target=serve);thread.start();connections=[]
                class RoutedSocket(original_socket):
                    def connect(self,target):
                        connections.append(target)
                        return super().connect(address)
                connection=https.NumericHTTPS(self.policy(),deadline_at=time.time()+2,stopped=lambda:False)
                connection._context=ssl.create_default_context(cafile=str(cert))
                try:
                    with mock.patch.object(https.socket,"socket",RoutedSocket),mock.patch.object(https.socket,"getaddrinfo",side_effect=AssertionError("unexpected DNS")):
                        if hostname==https.HOST:
                            self.assertEqual(connection.exchange(b"{}",{"Authorization":"Bearer SYNTHETIC_CANARY"}),{"http_status":200,"body":b"{}"})
                            with self.assertRaises(ValueError):connection.connect()
                        else:
                            with self.assertRaises(ssl.SSLCertVerificationError):connection.exchange(b"{}",{"Authorization":"Bearer SYNTHETIC_CANARY"})
                    self.assertEqual(connections,[("1.1.1.1",443)])
                finally:connection.close();thread.join(4);listener.close()
                self.assertFalse(thread.is_alive())
                if hostname==https.HOST:self.assertIn(b"SYNTHETIC_CANARY",received[0])
                else:self.assertEqual(received,[]);self.assertTrue(errors)

    def test_connection_close_response_transfer_cannot_extend_total_deadline(self):
        client,peer=socket.socketpair();client.settimeout(1);peer.settimeout(1)
        connection=https.NumericHTTPS(self.policy(),deadline_at=time.time()+.12,stopped=lambda:False)
        def connect():connection.sock=client;connection.active_socket=client
        def serve():
            try:
                body=b""
                while b"\r\n\r\n" not in body:body+=peer.recv(4096)
                peer.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 8\r\n\r\n")
                for _ in range(8):peer.sendall(b"x");time.sleep(.035)
            except OSError:pass
            finally:peer.close()
        thread=threading.Thread(target=serve);thread.start();start=time.monotonic()
        try:
            with mock.patch.object(connection,"connect",connect):
                with self.assertRaises((TimeoutError,OSError,https.http.client.HTTPException)):
                    connection.exchange(b"{}")
            self.assertLess(time.monotonic()-start,.25)
            retained=connection.retained_outcome()
            self.assertEqual(retained["http_status"],200)
            self.assertGreater(len(retained["body"]),0)
            self.assertLess(len(retained["body"]),8)
            self.assertFalse(retained["response_complete"])
            self.assertTrue(retained["transport_closed"])
            retained["body"]=b"forged"
            self.assertNotEqual(connection.retained_outcome()["body"],b"forged")
            with self.assertRaisesRegex(ValueError,"cannot be repeated"):connection.exchange(b"{}")
        finally:connection.abort();thread.join(2)

    def test_civil_clock_rollback_cannot_extend_dns_or_http(self):
        real_time=time.time;rolled=[False];clock=lambda:real_time()-(5 if rolled[0] else 0)
        with tempfile.TemporaryDirectory(prefix="fleet-dns-clock-") as temp:
            root=Path(temp)/"dns";publish=https.sandbox.publish
            def record(root,name,value):
                publish(root,name,value)
                if name=="process.json":rolled[0]=True
            started=time.monotonic()
            with mock.patch.object(https,"RESOLVER","import time;time.sleep(.5)"),mock.patch.object(https.time,"time",clock),mock.patch.object(https.sandbox,"publish",record):
                with self.assertRaises(subprocess.TimeoutExpired):https.prepare(root,timeout=.1)
            self.assertLess(time.monotonic()-started,.3)
            self.assertTrue(json.loads((root/"cleanup.json").read_text())["terminated"])
        rolled[0]=False;policy=self.policy();policy["resolved_at"]-=60
        client,peer=socket.socketpair();client.settimeout(1);peer.settimeout(1)
        connection=https.NumericHTTPS(policy,deadline_at=time.time()+.12,stopped=lambda:False)
        def connect():connection.sock=client;connection.active_socket=client
        def serve():
            try:
                peer.recv(4096);rolled[0]=True
                peer.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 8\r\n\r\n")
                for _ in range(8):peer.sendall(b"x");time.sleep(.035)
            except OSError:pass
            finally:peer.close()
        thread=threading.Thread(target=serve);thread.start();started=time.monotonic()
        try:
            with mock.patch.object(connection,"connect",connect),mock.patch.object(https.time,"time",clock):
                with self.assertRaises((TimeoutError,OSError,https.http.client.HTTPException)):connection.exchange(b"{}")
            self.assertLess(time.monotonic()-started,.25)
        finally:connection.abort();thread.join(2)

    def test_guardian_error_during_tls_transfer_forbids_handshake_and_http(self):
        main_thread=threading.current_thread();failed=threading.Event();raw=mock.MagicMock();wrapped=mock.MagicMock()
        def stopped():
            if threading.current_thread() is not main_thread:
                failed.set();raise OSError("revocation observation unavailable")
            return False
        connection=https.NumericHTTPS(self.policy(),deadline_at=time.time()+2,stopped=stopped)
        def wrap(*args,**kwargs):
            self.assertTrue(failed.wait(1));return wrapped
        connection._context=mock.Mock(wrap_socket=wrap)
        with mock.patch.object(https.socket,"socket",return_value=raw):
            with self.assertRaisesRegex(RuntimeError,"guardian failed"):connection.exchange(b"{}")
        wrapped.do_handshake.assert_not_called();self.assertFalse(connection.http_started)

    def test_partial_timeout_and_cleanup_failure_keep_both_causes(self):
        connection=https.NumericHTTPS(self.policy(),deadline_at=time.time()+2,stopped=lambda:False)
        response=mock.Mock(status=200,length=100)
        response.read1.side_effect=[b"prefix",TimeoutError("read failed")]
        with mock.patch.object(connection,"connect"),mock.patch.object(connection,"request"),mock.patch.object(connection,"getresponse",return_value=response),mock.patch.object(connection,"abort",side_effect=OSError("close failed")):
            with self.assertRaises(OSError):connection.exchange(b"{}")
        observed=connection.retained_outcome()
        self.assertEqual(observed["body"],b"prefix")
        self.assertEqual(observed["transport_error"],"TimeoutError")
        self.assertEqual(observed["cleanup_errors"],["OSError"])
        self.assertEqual(observed["guardian_errors"],[])
        self.assertFalse(observed["transport_closed"])
        self.assertFalse(observed["response_complete"])
        self.assertEqual(observed["observed_body_bytes"],6)
        self.assertFalse(observed["body_truncated"])

    def test_connect_and_early_close_failures_are_retained(self):
        connection=https.NumericHTTPS(self.policy(),deadline_at=time.time()+2,stopped=lambda:False)
        raw=mock.Mock();raw.connect.side_effect=TimeoutError("connect")
        with mock.patch.object(https.socket,"socket",return_value=raw),mock.patch.object(connection,"close",side_effect=[OSError("early cleanup"),None]):
            with self.assertRaises(OSError):connection.exchange(b"{}")
        observed=connection.retained_outcome()
        self.assertEqual(observed["transport_error"],"TimeoutError")
        self.assertEqual(observed["cleanup_errors"],["OSError"])
        self.assertFalse(observed["transport_closed"])

    def test_interrupted_join_still_closes_response_and_retains_failure(self):
        connection=https.NumericHTTPS(self.policy(),deadline_at=time.time()+2,stopped=lambda:False)
        response=mock.Mock(status=200,length=0);response.read1.return_value=b""
        watcher=mock.Mock();watcher.join.side_effect=KeyboardInterrupt()
        with mock.patch.object(https.threading,"Thread",return_value=watcher),mock.patch.object(connection,"connect"),mock.patch.object(connection,"request"),mock.patch.object(connection,"getresponse",return_value=response):
            with self.assertRaises(KeyboardInterrupt):connection.exchange(b"{}")
        response.close.assert_called_once()
        observed=connection.retained_outcome()
        self.assertEqual(observed["cleanup_errors"],["KeyboardInterrupt"])
        self.assertFalse(observed["transport_closed"])
        self.assertFalse(observed["response_complete"])


if __name__=="__main__":unittest.main()
