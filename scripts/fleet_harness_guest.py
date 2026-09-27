"""Untrusted-side D1/D2 adapter. Contains no tests, expected values or verdicts.

Executed ONLY inside the owned sandbox. The host never imports a candidate.
All output is untrusted RPC; only CONTROL can evaluate or accept it.
"""
import copy
from collections import UserDict
import dataclasses
from enum import IntEnum
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import sys

sys.path.insert(0, "/candidate")


class IntChild(int):
    pass


class Generation(IntEnum):
    ZERO = 0
    ONE = 1


def typed(value):
    if type(value) is not dict or set(value) != {"constructor", "value"}:
        return value
    if value["constructor"] == "int_subclass":
        return IntChild(value["value"])
    if value["constructor"] == "int_enum":
        return Generation(value["value"])
    raise ValueError("unsupported typed constructor")


def receipt(value):
    if value is None:
        return None
    if not dataclasses.is_dataclass(value) or {f.name for f in dataclasses.fields(value)} != {"path", "sha256", "generation"}:
        raise TypeError("Receipt dataclass fields differ")
    if type(value.path) is not str or type(value.sha256) is not str or type(value.generation) is not int:
        raise TypeError("Receipt field types differ")
    return {"path": value.path, "sha256": value.sha256, "generation": value.generation}


def main():
    ledger = None
    last_returned = None
    root = Path("/work/root")
    for line in sys.stdin.buffer:
        if len(line) > 65537:
            return
        request = json.loads(line)
        value, error, after, rows = None, None, None, None
        previous = resource.getrlimit(resource.RLIMIT_FSIZE)
        try:
            operation = request["op"]
            if operation == "reset":
                from ledger import ReceiptLedger
                if root.exists():
                    shutil.rmtree(root)
                root.mkdir()
                ledger = ReceiptLedger(root)
            elif operation == "record":
                if request.get("partial_write"):
                    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
                    resource.setrlimit(resource.RLIMIT_FSIZE, (2, previous[1]))
                args = request["args"]
                last_returned = ledger.record(args[0], typed(args[1]), args[2], args[3])
                value = receipt(last_returned)
            elif operation == "mutate_returned":
                value = {}
                for name, replacement in (("path", "altered"), ("sha256", "0"*64), ("generation", 999)):
                    try:
                        setattr(last_returned, name, replacement)
                        value[name] = False
                    except (AttributeError, TypeError):
                        value[name] = True
            elif operation == "latest":
                value = receipt(ledger.latest(request["run"]))
            elif operation == "alter":
                # Only fixed enum setup operations; never executes supplied code.
                target = root / request["path"]
                if request["action"] == "unlink":
                    target.unlink()
                elif request["action"] == "write":
                    target.write_text(request["content"])
                elif request["action"] == "parent_file":
                    shutil.rmtree(target); target.write_text("parent replaced")
                elif request["action"] == "symlink":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(request["target"])
                else:
                    raise ValueError("unsupported setup")
            elif operation == "usage":
                from report import summarize_usage
                rows = copy.deepcopy(request["records"])
                if request.get("mappings"):
                    rows = [UserDict(row) for row in rows]
                admitted = request["admitted"][:]
                value = summarize_usage(iter(rows), request["mission"], iter(admitted))
            else:
                raise ValueError("unsupported operation")
        except BaseException as exc:
            error = type(exc).__name__
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, previous)
            if rows is not None:
                after = [dict(row) for row in rows]
        sys.stdout.write(json.dumps({"id": request["id"], "value": value, "error": error,
                                    "input_after": after}, ensure_ascii=True, allow_nan=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
