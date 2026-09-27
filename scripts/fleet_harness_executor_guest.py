"""Single-command adapter inside the untrusted executor, never the verifier."""
import json
import os
import selectors
import subprocess
import sys
import time


def main():
    request = json.loads(sys.stdin.buffer.readline(65537))
    command, seconds = request["command"], request["seconds"]
    process = subprocess.Popen(["/bin/bash", "-c", command], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    output, exceeded, deadline = b"", False, time.monotonic() + seconds
    os.set_blocking(process.stdout.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map() and time.monotonic() < deadline:
            for key, _ in selector.select(max(0, deadline - time.monotonic())):
                block = os.read(key.fd, 65536)
                if not block:
                    selector.unregister(key.fileobj)
                else:
                    output += block
                    if len(output) > 32768:
                        exceeded = True
                        break
            if exceeded: break
        # EOF and child exit are separate observations. A child may close its
        # streams before finishing, or exit while a descendant retains them.
        timed_out = bool(selector.get_map()) and not exceeded
    if not timed_out and not exceeded:
        try:process.wait(timeout=max(0,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:timed_out=True
    if timed_out or exceeded:
        # Local process group is a convenience only. CONTROL removes the whole
        # exact container before admitting another command or observing writes.
        try: os.killpg(process.pid, 9)
        except ProcessLookupError: pass
    process.wait(timeout=2)
    value = {"output": output[:32768].decode(errors="replace"), "returncode": process.returncode,
             "timed_out": timed_out, "truncated": exceeded}
    print(json.dumps({"id": request["id"], "value": value, "error": None, "input_after": None}), flush=True)


if __name__ == "__main__": main()
