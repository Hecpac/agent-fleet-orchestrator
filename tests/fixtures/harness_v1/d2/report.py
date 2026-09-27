def summarize_usage(records, mission_id, admitted_runs):
    admitted = set(admitted_runs)
    selected = {}
    identities = {}
    for record in records:
        if (record.get("mission_id") != mission_id or not isinstance(record.get("run_id"), str)
                or record.get("run_id") not in admitted):
            continue
        run = record["run_id"]
        sequence = record.get("sequence")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("invalid sequence")
        identity = (record.get("provider"), record.get("model"), record.get("variant"))
        if (any(not isinstance(item, str) or not item for item in identity[:2])
                or (identity[2] is not None and (not isinstance(identity[2], str) or not identity[2]))):
            raise ValueError("invalid identity")
        if run in identities and identities[run] != identity:
            raise ValueError("identity changed")
        identities[run] = identity
        selected.setdefault(run, []).append(record)
    grouped = {}
    for run, records in selected.items():
        maximum = max(record["sequence"] for record in records)
        latest = [record for record in records if record["sequence"] == maximum]
        for record in latest:
            for field in ("prompt_tokens", "completion_tokens", "cached_input_tokens"):
                value = record.get(field)
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError("invalid counter")
            if (record.get("prompt_tokens") is not None and record.get("cached_input_tokens") is not None
                    and record["cached_input_tokens"] > record["prompt_tokens"]):
                raise ValueError("cache exceeds prompt")
        if any(record != latest[0] for record in latest[1:]):
            raise ValueError("conflicting maximum")
        grouped.setdefault(identities[run], []).append(latest[0])
    result = []
    for identity, records in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] is not None, item[0][2] or "")):
        known = sum(record.get("prompt_tokens") is not None and record.get("completion_tokens") is not None for record in records)
        complete = known == len(records)
        result.append({"provider": identity[0], "model": identity[1], "variant": identity[2],
            "runs": len(records), "known_runs": known,
            "prompt_tokens": sum(record["prompt_tokens"] for record in records) if complete else None,
            "completion_tokens": sum(record["completion_tokens"] for record in records) if complete else None,
            "cached_input_tokens": sum(record["cached_input_tokens"] for record in records)
                if complete and all(record.get("cached_input_tokens") is not None for record in records) else None})
    return {"total_runs": len(admitted), "observed_runs": len(selected), "missing_runs": len(admitted) - len(selected), "groups": result}
