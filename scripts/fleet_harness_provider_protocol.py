"""Versioned DeepSeek projection; original provider bytes remain immutable.

v2 normalizes null tool-message content. v3 additionally removes an optional
tool-call index only when every index is an exact integer matching array order.
The pinned budget selects replay semantics, never the current module default.
"""
import copy

LEGACY_VERSION="deepseek-mini-wire-v2"
INDEXED_VERSION="deepseek-mini-wire-v3"
THINKING_32K_VERSION="deepseek-mini-wire-v4"
VERSION=INDEXED_VERSION
VERSIONS=frozenset((LEGACY_VERSION,INDEXED_VERSION,THINKING_32K_VERSION))


def validate_version(version):
    if not isinstance(version,str) or version not in VERSIONS:
        raise ValueError("unknown DeepSeek wire projection")
    return version


def messages(native, *, version=VERSION):
    validate_version(version)
    result=[]
    for message in native:
        allowed={"role","content","tool_calls","tool_call_id"}
        if message["role"]=="assistant":
            allowed.add("reasoning_content")
            if message.get("tool_calls") and not isinstance(message.get("reasoning_content"),str):
                raise ValueError("thinking tool response lacks replayable reasoning_content")
        result.append({k:copy.deepcopy(v) for k,v in message.items() if k in allowed})
    return result


def normalize(original, *, version=VERSION):
    validate_version(version)
    value=copy.deepcopy(original)
    if isinstance(value,dict) and isinstance(value.get("choices"),list):
        for choice in value["choices"]:
            message=choice.get("message") if isinstance(choice,dict) else None
            if isinstance(message,dict) and message.get("content") is None and message.get("tool_calls"):
                message["content"]=""
            if version in (INDEXED_VERSION,THINKING_32K_VERSION) and isinstance(message,dict):
                calls=message.get("tool_calls")
                if isinstance(calls,list) and any(isinstance(call,dict) and "index" in call for call in calls):
                    # Never sort, invent an ID, or hide a partially indexed batch.
                    # Mini still validates IDs, names, arguments and unknown keys.
                    for position,call in enumerate(calls):
                        if not isinstance(call,dict) or type(call.get("index")) is not int or call["index"]!=position:
                            raise ValueError("ambiguous provider tool-call index")
                    for call in calls:del call["index"]
    return value


def payload(native,tools, *, version=VERSION):
    return {"model":"deepseek-flash","max_tokens":32768 if version==THINKING_32K_VERSION else 8192,"stream":False,"messages":messages(native,version=version),
        "tools":copy.deepcopy(tools),"thinking":{"type":"enabled"},"reasoning_effort":"max"}
