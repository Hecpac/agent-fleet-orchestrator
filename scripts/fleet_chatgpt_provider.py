"""CONTROL-owned ChatGPT subscription egress; credentials never enter the guest.

The fixed worker only performs Responses HTTP. It cannot evaluate generated code,
follow redirects or choose an endpoint from a request. Local fixtures have a
different frozen transport identity and always use synthetic credentials.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_json
import fleet_herdr_inference as inference

ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
TRANSPORT = "chatgpt-subscription-https-v1"
FIXTURE = "chatgpt-subscription-loopback-v1"
SYNTHETIC_TOKEN = "SYNTHETIC_SUBSCRIPTION_TOKEN"
SYNTHETIC_ACCOUNT = "synthetic-account"


# Diagnostic vocabulary is CONTROL-owned. Never serialize exception strings,
# upstream bodies/headers or arbitrary SSE values across the worker boundary.
PHASES = {'credentials', 'request', 'http', 'sse', 'worker', 'ipc'}
CODES = {'unavailable', 'http_status', 'content_type', 'invalid_response',
         'timeout', 'tls', 'connection', 'closed', 'unsupported_event', 'event_sequence',
         'terminal_output', 'terminal_usage', 'credential_echo', 'stream_limit', 'upstream_incomplete'}


def diagnostic(phase, code, http_status=None):
    value = {'schema': 'fleet.subscription.failure.v1', 'phase': phase,
             'code': code, 'http_status': http_status}
    validate_diagnostic(value)
    return value


def validate_diagnostic(value):
    check(type(value) is dict and set(value) == {'schema', 'phase', 'code', 'http_status'}
          and value['schema'] == 'fleet.subscription.failure.v1'
          and value['phase'] in PHASES and value['code'] in CODES
          and (value['http_status'] is None or type(value['http_status']) is int
               and 100 <= value['http_status'] <= 599), 'invalid subscription diagnostic')
    return value


class SubscriptionFailure(inference.InferenceError):
    def __init__(self, value):
        self.diagnostic = validate_diagnostic(value)
        super().__init__('subscription ' + value['phase'] + ': ' + value['code'])


def check(value, message):
    if not value:
        raise inference.InferenceError(message)


def descriptor(account, transport):
    return {"kind": "chatgpt-subscription", "transport": transport,
        "endpoint": ENDPOINT, "account_sha256": hashlib.sha256(account.encode()).hexdigest()}


def validate_descriptor(value):
    check(type(value) is dict and set(value) == {"kind", "transport", "endpoint", "account_sha256"},
          "subscription provider fields")
    check(value["kind"] == "chatgpt-subscription" and value["endpoint"] == ENDPOINT
          and value["transport"] in {TRANSPORT, FIXTURE}, "subscription provider route")
    check(type(value["account_sha256"]) is str and re.fullmatch('[0-9a-f]{64}', value["account_sha256"]),
          "subscription account binding")


def execution(policy):
    return "REMOTE" if policy.get('provider', {}).get('transport') == TRANSPORT else "SIMULATED"


def _private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
              and stat.S_IMODE(info.st_mode) & 0o077 == 0 and info.st_size <= 65536,
              "subscription credential file is not private")
        raw = os.read(fd, 65537)
        check(len(raw) <= 65536, "subscription credential file size")
        return fleet_json.loads(raw)
    finally:
        os.close(fd)


def _claims(token):
    try:
        part = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    except (IndexError, ValueError, UnicodeError):
        return {}


def _credentials(home):
    # No env API keys, keyring fallback, OAuth exchange or credential writes.
    # Codex owns refresh. A changed account or expired token fails before send.
    try:
        config_path = home/'config.toml'
        config = tomllib.loads(config_path.read_text()) if config_path.is_file() else {}
        check(config.get('cli_auth_credentials_store', 'file') == 'file'
              and config.get('forced_login_method') in {None, 'chatgpt'}
              and config.get('chatgpt_base_url', 'https://chatgpt.com/backend-api').rstrip('/')
                  == 'https://chatgpt.com/backend-api', 'subscription configuration is unsupported')
        record = _private_file(home/'auth.json')
        check(type(record) is dict and record.get('auth_mode') in {None, 'chatgpt'},
              'ChatGPT login is required')
        check(record.get('auth_mode') == 'chatgpt' or not record.get('OPENAI_API_KEY'),
              'ambiguous legacy authentication')
        tokens = record.get('tokens')
        check(type(tokens) is dict, 'ChatGPT credentials unavailable')
        access = tokens.get('access_token')
        check(type(access) is str and 0 < len(access) <= 16384 and re.fullmatch(r'[A-Za-z0-9._~\-]+', access),
              'ChatGPT access token unavailable')
        account = tokens.get('account_id')
        if account is None:
            account = _claims(tokens.get('id_token', '')).get('https://api.openai.com/auth', {}).get('chatgpt_account_id')
        check(type(account) is str and re.fullmatch(r'[A-Za-z0-9_\-]{1,256}', account),
              'ChatGPT account unavailable')
        check(config.get('forced_chatgpt_workspace_id') in {None, account}, 'ChatGPT workspace differs')
        expiry = _claims(access).get('exp')
        check(expiry is None or type(expiry) in {int, float} and expiry > time.time()+30,
              'ChatGPT credential expired; refresh through Codex')
        return access, account
    except inference.InferenceError:
        raise
    except Exception:
        raise inference.InferenceError('ChatGPT credentials unavailable') from None


def preflight(home):
    _, account = _credentials(Path(home).resolve(strict=True))
    return {'authentication': 'chatgpt', 'provider': descriptor(account, TRANSPORT),
        'credential_status': 'loaded_locally', 'server_authentication': 'NOT_VERIFIED',
        'network_requests': 0}


def _sse(stream, body, model, limit, secret):
    import fleet_codex_responses as responses
    values, data, declared, size = [], [], None, 0
    while True:
        line = stream.readline(65537)
        size += len(line)
        check(line and len(line) <= 65536 and size <= 2*1024*1024, 'subscription stream incomplete or oversized')
        check(secret.encode() not in line, 'subscription credential echoed by upstream')
        line = line.rstrip(b'\r\n')
        if line.startswith(b':'):
            continue
        if line:
            name, sep, value = line.partition(b':')
            check(sep and name in {b'event', b'data'}, 'subscription SSE field')
            if value.startswith(b' '): value = value[1:]
            if name == b'event':
                check(declared is None, 'duplicate subscription event field')
                declared = value.decode('ascii')
            else:
                data.append(value)
            continue
        if not data:
            check(declared is None, 'subscription event without data')
            continue
        raw = b'\n'.join(data)
        check(secret.encode() not in raw, 'subscription credential echoed by upstream')
        event = fleet_json.loads(raw)
        check(type(event) is dict and (declared is None or declared == event.get('type')), 'subscription event differs')
        values.append(event); data, declared = [], None
        check(len(values) <= 1024, 'too many subscription events')
        if event.get('type') in {'error', 'response.failed', 'response.incomplete'}:
            raise inference.InferenceError('subscription did not complete')
        if event.get('type') == 'response.completed':
            bundle = fleet_json.canonical_bytes(values)
            check(secret.encode() not in bundle, 'subscription credential echoed by upstream')
            responses.events(bundle, body, model, limit)
            return bundle, event['response']['usage']['output_tokens']


def _worker(spec, envelope):
    import fleet_codex_responses as responses
    provider = spec['provider']; validate_descriptor(provider)
    body = fleet_json.loads(envelope['input'])
    responses.request(envelope['input'].encode(), {'model':envelope['model'],
        'effort':envelope['effort'], 'max_output_tokens':envelope['max_output_tokens']})
    if provider['transport'] == TRANSPORT:
        access, account = _credentials(Path(spec['home']))
        connection = http.client.HTTPSConnection('chatgpt.com', 443, timeout=spec['timeout'],
            context=ssl.create_default_context())
    else:
        access, account = SYNTHETIC_TOKEN, SYNTHETIC_ACCOUNT
        check(type(spec.get('port')) is int and 0 < spec['port'] < 65536, 'fixture port')
        connection = http.client.HTTPConnection('127.0.0.1', spec['port'], timeout=spec['timeout'])
    check(descriptor(account, provider['transport']) == provider, 'subscription account changed')
    headers = {'Authorization':'Bearer '+access, 'ChatGPT-Account-ID':account,
        'Content-Type':'application/json', 'Accept':'text/event-stream', 'Connection':'close',
        'User-Agent':'AgentFleet-Codex-Bridge/1', 'originator':'codex_cli_rs',
        'X-Client-Request-ID':envelope['request_id']}
    phase = 'request'
    try:
        connection.request('POST', '/backend-api/codex/responses',
            body=fleet_json.canonical_bytes(body), headers=headers)
        phase = 'http'
        response = connection.getresponse()
        if response.status != 200:
            raise SubscriptionFailure(diagnostic(phase, 'http_status', response.status))
        if response.getheader('Content-Type', '').split(';')[0].strip() != 'text/event-stream':
            raise SubscriptionFailure(diagnostic(phase, 'content_type', response.status))
        phase = 'sse'
        bundle, used = _sse(response, body, envelope['model'], envelope['max_output_tokens'], access)
        return fleet_json.canonical_bytes({**{k:envelope[k] for k in ('policy_id','request_id','request_sha256','model')},
            'output':bundle.decode(), 'output_tokens':used})
    except SubscriptionFailure:
        raise
    except Exception as exc:
        code = ('timeout' if isinstance(exc, TimeoutError) else
                'tls' if isinstance(exc, ssl.SSLError) else
                'connection' if isinstance(exc, (OSError, http.client.HTTPException)) else
                'invalid_response' if phase == 'sse' else 'unavailable')
        safe_codes = {'unsupported Responses event':'unsupported_event',
            'Responses event sequence':'event_sequence', 'Responses terminal output differs':'terminal_output',
            'Responses terminal usage':'terminal_usage',
            'subscription credential echoed by upstream':'credential_echo',
            'subscription stream incomplete or oversized':'stream_limit',
            'subscription did not complete':'upstream_incomplete'}
        if isinstance(exc, inference.InferenceError): code=safe_codes.get(str(exc),code)
        raise SubscriptionFailure(diagnostic(phase, code)) from None
    finally:
        connection.close()


class ChatGPTProvider:
    """One bounded owned egress child per request, never a general URL proxy."""
    def __init__(self, home):
        self.home = Path(home).resolve(strict=True)
        self._binding = preflight(self.home)['provider']
        self.port = None
        self.closed = False
        self.child = None
        self.last_diagnostic = None

    @classmethod
    def fixture(cls, port):
        check(type(port) is int and 0 < port < 65536, 'fixture port')
        obj = cls.__new__(cls)
        obj.home = None; obj.port = port; obj.closed = False; obj.child = None; obj.last_diagnostic = None
        obj._binding = descriptor(SYNTHETIC_ACCOUNT, FIXTURE)
        return obj

    @property
    def binding(self):
        return dict(self._binding)

    def ready(self):
        check(not self.closed, 'subscription transport closed')
        if self.home is not None:
            check(preflight(self.home)['provider'] == self._binding, 'subscription account changed')

    def close(self):
        self.closed = True
        if self.child is not None:
            if self.child.poll() is None: self.child.kill()
            self.child.wait(timeout=3)
            self.child = None

    def exchange(self, raw, deadline, poll):
        self.last_diagnostic = None
        self.ready(); poll()
        parent, peer = socket.socketpair()
        try:
            self.child = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(Path(__file__).resolve()),
                '--worker-fd', str(peer.fileno())], pass_fds=(peer.fileno(),), close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'}, start_new_session=True)
            peer.close()
            spec = {'provider':self.binding, 'home':str(self.home) if self.home is not None else None,
                'port':self.port, 'timeout':max(.001, deadline-time.monotonic())}
            inference.write_frame(parent, fleet_json.canonical_bytes({'spec':spec,'envelope':fleet_json.loads(raw)}), deadline, poll)
            result = inference.read_frame(parent, deadline, poll)
            poll()
            value = fleet_json.loads(result)
            if type(value) is dict and value.get('schema') == 'fleet.subscription.failure.v1':
                self.last_diagnostic = validate_diagnostic(value)
                raise SubscriptionFailure(self.last_diagnostic)
            return result
        except BaseException:
            if self.last_diagnostic is None:
                self.last_diagnostic = diagnostic('ipc', 'closed')
            self.closed = True
            raise
        finally:
            parent.close(); peer.close()
            if self.child is not None:
                if self.child.poll() is None: self.child.kill()
                self.child.wait(timeout=3)
                self.child = None


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--worker-fd':
        with socket.socket(fileno=int(sys.argv[2])) as channel:
            try:
                request = fleet_json.loads(inference.read_frame(channel, time.monotonic()+5))
                duration = request['spec']['timeout']
                check(type(duration) in {int, float} and 0 < duration <= 30, 'egress worker duration')
                # Survives loss of the supervisor: a stalled DNS/TLS/body read
                # cannot keep an orphan credential-bearing worker alive.
                import resource
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
                signal.setitimer(signal.ITIMER_REAL, duration)
                result = _worker(request['spec'], request['envelope'])
                inference.write_frame(channel, result, time.monotonic()+2)
                return 0
            except Exception as exc:
                safe = exc.diagnostic if isinstance(exc, SubscriptionFailure) else diagnostic('worker', 'unavailable')
                try:
                    inference.write_frame(channel, fleet_json.canonical_bytes(safe), time.monotonic()+.2)
                except Exception:
                    pass
                return 1  # No raw provider/credential diagnostics on stdout/stderr.
    raise SystemExit('internal subscription egress worker')


if __name__ == '__main__':
    raise SystemExit(main())
