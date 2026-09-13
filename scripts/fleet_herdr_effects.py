"""Fail-closed admission at the native capability handoff, not an OS sandbox.

No installed adapter proves mediation of every native effect path. Therefore
there are currently no native execution grants. A prompt, manifest, permission
attestation or observer HELLO cannot open this gate. CONTROL may still observe
and stop its already-owned resources through the exact operations below.
"""
from __future__ import annotations


class EffectMediationDenied(RuntimeError):
    """Native effects have no complete enforcing adapter."""


def require_native_mediation() -> None:
    # Deliberately no environment switch, request field or callback grant.
    # Enabling execution requires an enforcing adapter and new boundary tests.
    raise EffectMediationDenied(
        "native execution denied before effect: mediation unavailable for "
        "filesystem, network, processes, credentials"
    )


def require_control_operation(command: list[str], *, executable: str, native_executable: str, session: str) -> None:
    """Allow only the backend's bounded observation/stop command forms.

    The backend remains responsible for ownership, identity and quiescence.
    This gate is called by the real subprocess transport. Injected transports
    are trusted test/embedding code, not an agent-facing extension point.
    """
    if not isinstance(command, list) or not all(isinstance(x, str) and "\0" not in x for x in command):
        require_native_mediation()
    # Exact diagnostic invocation of the trusted CLI, with no agent input.
    # Existing recovery/cancellation preflight checks require its version.
    if command == [native_executable, "--version"]:
        return
    if command[:3] != [executable, "--session", session]:
        require_native_mediation()
    args = command[3:]
    if args == ["--version"]:
        return
    # Reject option injection and extra arguments, even for read operations.
    def identifier(value: str) -> bool:
        return bool(value) and not value.startswith("-") and not any(c.isspace() for c in value)

    if (len(args) == 3 and args[:2] in (["workspace", "get"], ["workspace", "close"],
                                      ["pane", "get"], ["agent", "get"])
            and identifier(args[2])):
        return
    if len(args) == 4 and args[:3] == ["pane", "process-info", "--pane"] and identifier(args[3]):
        return
    if len(args) >= 3 and args[0] == "agent" and identifier(args[2]):
        if args[1] == "read" and args[3:] == ["--source", "visible"]:
            return
        if args[1] == "send-keys" and args[3:] == ["ctrl+c"]:
            return
        if args[1] == "wait":
            tail = args[3:]
            if tail[:6] == ["--until", "idle", "--until", "done", "--until", "blocked"]:
                tail = tail[6:]
            if (len(tail) == 2 and tail[0] == "--timeout" and tail[1].isascii()
                    and tail[1].isdigit() and len(tail[1]) <= 6 and 0 < int(tail[1]) <= 300000):
                return
    # Includes start, prompt, workspace/pane creation (starts shells), and
    # every unknown operation. --version above is trusted diagnostic code.
    require_native_mediation()
