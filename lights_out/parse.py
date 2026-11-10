"""Read what a matched command actually created or deleted, from the terminal tool's result.

Evidence only: an ID is recorded when it is in the CLI's output (JSON first, then the rule's ID
pattern for ``--output text`` / ``--query``). A create that ran but shows no ID is an *unknown*,
which blocks an "all clear" on the receipt.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .matcher import Match


@dataclass
class Outcome:
    status: str                     # "created" | "deleted" | "unknown" | "failed" | "not_run"
    ids: list[str] = field(default_factory=list)
    reason: str = ""


def terminal_result(result: Any) -> tuple[str, Optional[int], str]:
    """(output, exit_code, error) from the terminal tool's JSON result string."""
    if isinstance(result, dict):
        data = result
    else:
        try:
            data = json.loads(result) if isinstance(result, str) else {}
        except (ValueError, TypeError):
            return (str(result or ""), None, "")
    if not isinstance(data, dict):
        return (str(result or ""), None, "")
    out = data.get("output")
    code = data.get("exit_code")
    err = data.get("error") or ""
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return (out if isinstance(out, str) else json.dumps(out) if out is not None else "", code, str(err))


def json_documents(text: str) -> list[Any]:
    """Every top-level JSON object/array in *text* (outputs of chained commands are concatenated)."""
    docs: list[Any] = []
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        j = min([p for p in (text.find("{", i), text.find("[", i)) if p != -1], default=-1)
        if j == -1:
            break
        try:
            obj, end = dec.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(obj, (dict, list)):
            docs.append(obj)
        i = end
    return docs


def walk_path(obj: Any, path: str) -> list[str]:
    """``Instances[].InstanceId`` / ``NatGateway.NatGatewayId`` / ``Location`` -> string values."""
    cur: list[Any] = [obj]
    for part in path.split("."):
        many = part.endswith("[]")
        name = part[:-2] if many else part
        nxt: list[Any] = []
        for c in cur:
            if isinstance(c, dict) and name in c:
                v = c[name]
                if many and isinstance(v, list):
                    nxt.extend(v)
                elif not many:
                    nxt.append(v)
        cur = nxt
    return [str(v) for v in cur if isinstance(v, (str, int)) and str(v)]


def _dedupe(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def created_ids(m: Match, output: str) -> list[str]:
    rule = m.rule
    ids: list[str] = []
    docs = json_documents(output)
    path = rule.get("ids")
    if path:
        for d in docs:
            ids.extend(walk_path(d, path))
    if rule.get("id_from_arg"):
        named = m.target_ids()
        if not named:
            return []
        name = named[0]
        if path and ids:
            # s3api create-bucket prints {"Location": "/name"} (us-east-1) or a URL with the name
            return [name] if any(name in i for i in ids) else []
        pat = rule.get("success_pattern")
        if pat and re.search(pat + r"(?:s3://)?" + re.escape(name) + r"/?\s*$", output, re.M):
            return [name]
        return []
    if not ids and rule.get("id_pattern"):
        ids = re.findall(rule["id_pattern"], output)
    if not ids and rule.get("id_flag") and docs:
        # e.g. rds: the identifier is echoed back in the JSON under another key
        named = m.target_ids()
        if named and any(named[0] in json.dumps(d) for d in docs):
            ids = named[:1]
    return _dedupe(ids)


def outcome(m: Match, result: Any) -> Outcome:
    output, code, err = terminal_result(result)
    if m.action == "create":
        ids = created_ids(m, output)
        if ids:
            return Outcome("created", ids)
        if code not in (0, None):
            return Outcome("failed", [], f"exit code {code}")
        if code is None and not output.strip():
            return Outcome("not_run", [], err or "no output")
        return Outcome("unknown", [], "ran, but no resource ID in the output")
    # delete
    if code == 0:
        return Outcome("deleted", m.target_ids())
    return Outcome("failed", [], f"exit code {code}" if code is not None else (err or "no exit code"))
