"""Match shell commands against the rule pack. Pure functions, no I/O beyond reading the rule file.

A command line is split with shlex (quotes respected) into simple commands at ``&&``, ``||``,
``;``, ``|``, ``&`` and newlines. Each simple command that runs ``aws`` is read as
``aws [global options] <service> <operation> [options]``. ``bash -c '...'`` / ``sh -c '...'``
are looked into, so wrapping a create in a shell doesn't hide it.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .yamlish import load_yaml

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"

# AWS CLI global options that take no value. Every other global ``--x`` takes one value.
_GLOBAL_BOOL = {
    "--debug", "--no-verify-ssl", "--no-paginate", "--no-sign-request", "--no-cli-pager",
    "--cli-auto-prompt", "--no-cli-auto-prompt", "--version",
}
_GLOBAL_WITH_VALUE = {
    "--endpoint-url", "--output", "--query", "--profile", "--region", "--ca-bundle",
    "--cli-read-timeout", "--cli-connect-timeout", "--cli-binary-format", "--color",
}
# Operation options that are switches (no value). Anything else starting with -- takes values
# until the next option.
_OP_BOOL = {
    "--dry-run", "--no-dry-run", "--force", "--skip-final-snapshot", "--no-skip-final-snapshot",
    "--multi-az", "--no-multi-az", "--publicly-accessible", "--no-publicly-accessible",
    "--storage-encrypted", "--no-storage-encrypted", "--ebs-optimized", "--no-ebs-optimized",
    "--associate-public-ip-address", "--no-associate-public-ip-address", "--encrypted",
    "--no-encrypted", "--recursive", "--delete-automated-backups", "--no-delete-automated-backups",
    "--generate-cli-skeleton", "--deletion-protection", "--no-deletion-protection",
    "--apply-immediately", "--no-apply-immediately", "--quiet", "--only-show-errors",
}
_SEPARATORS = {"&&", "||", ";", "|", "&", "\n", ";;", "|&", "(", ")"}
_WRAPPERS = {"sudo", "time", "env", "command", "exec", "nohup", "timeout", "xargs", "builtin"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class Match:
    """One billable create or one delete found in a command."""

    action: str                       # "create" | "delete"
    key: str                          # rule key, e.g. "ec2:create"
    kind: str                         # ec2 | ebs | eip | nat | elb | rds | s3
    rule: dict = field(repr=False)
    flags: dict = field(default_factory=dict, repr=False)
    positionals: list = field(default_factory=list, repr=False)
    region: str = ""
    segment: str = ""                 # the simple command this came from

    @property
    def service(self) -> str:
        return self.key.split(":", 1)[0]

    def flag(self, name: str) -> Optional[str]:
        vals = self.flags.get(name)
        return vals[0] if vals else None

    def flag_all(self, name: str) -> list[str]:
        out: list[str] = []
        for v in self.flags.get(name, []) or []:
            # --instance-ids i-1 i-2, or i-1,i-2 (shorthand list)
            out.extend(p for p in v.split(",") if p)
        return out

    def target_ids(self) -> list[str]:
        """For deletes (and id-from-arg creates): the resource IDs named in the command."""
        rule = self.rule
        if rule.get("positional") == "s3_uri" or rule.get("id_positional") == "s3_uri":
            for p in self.positionals:
                if p.startswith("s3://"):
                    name = p[len("s3://"):].strip("/").split("/", 1)[0]
                    return [name] if name else []
            return []
        flag = rule.get("flag") or rule.get("id_flag")
        if flag:
            return self.flag_all(flag)
        return []


@lru_cache(maxsize=8)
def load_rules(path: str | None = None) -> dict:
    p = Path(path) if path else RULES_DIR / "aws.yaml"
    data = load_yaml(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "creates" not in data or "deletes" not in data:
        raise ValueError(f"rule pack {p} is missing creates/deletes")
    return data


def split_simple_commands(command: str) -> list[list[str]]:
    """Split a command line into simple commands (lists of words). Unparseable quoting falls back
    to a plain whitespace split so a stray quote can't hide an ``aws`` create."""
    lex = shlex.shlex(command.replace("\r", ""), posix=True, punctuation_chars=";&|()\n")
    lex.whitespace = " \t"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        tokens = re.split(r"[ \t]+", command.replace("\r", ""))
        tokens = [t for part in tokens for t in re.split(r"(&&|\|\||;|\||\n)", part) if t]
    out: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS or (tok and set(tok) <= set(";&|()\n")):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _strip_prefix(words: list[str]) -> list[str]:
    """Drop env assignments and wrappers (sudo, time, env, timeout 30, ...) before the program."""
    i = 0
    while i < len(words):
        w = words[i]
        if _ENV_ASSIGN.match(w):
            i += 1
            continue
        base = w.rsplit("/", 1)[-1]
        if base in _WRAPPERS:
            i += 1
            # skip wrapper options and timeout's duration
            while i < len(words) and (words[i].startswith("-") or (base == "timeout" and re.match(r"^\d", words[i]))):
                i += 1
            continue
        break
    return words[i:]


def _parse_aws(words: list[str]) -> Optional[tuple[str, str, dict, list, str]]:
    """``aws [globals] service operation [opts]`` -> (service, operation, flags, positionals, region)."""
    i, region = 1, ""
    head: list[str] = []
    flags: dict[str, list[str]] = {}
    positionals: list[str] = []
    n = len(words)
    while i < n:
        w = words[i]
        if w.startswith("--"):
            name, eq, val = w.partition("=")
            if len(head) < 2:
                if name in _GLOBAL_BOOL:
                    i += 1
                    continue
                if eq:
                    if name == "--region":
                        region = val
                    i += 1
                    continue
                if name in _GLOBAL_WITH_VALUE or name not in _OP_BOOL:
                    if name == "--region" and i + 1 < n:
                        region = words[i + 1]
                    i += 2
                    continue
                i += 1
                continue
            # operation options (globals may also appear here; same handling)
            if name in _GLOBAL_BOOL or name in _OP_BOOL:
                flags.setdefault(name, [])
                i += 1
                continue
            if eq:
                flags.setdefault(name, []).append(val)
                if name == "--region":
                    region = val
                i += 1
                continue
            vals: list[str] = []
            i += 1
            while i < n and not words[i].startswith("--"):
                vals.append(words[i])
                i += 1
                if name in _GLOBAL_WITH_VALUE:
                    break
            if name == "--region" and vals:
                region = vals[0]
            if name in _GLOBAL_WITH_VALUE:
                continue
            flags.setdefault(name, []).extend(vals)
            continue
        if len(head) < 2:
            head.append(w)
        else:
            positionals.append(w)
        i += 1
    if len(head) < 2:
        return None
    return head[0], head[1], flags, positionals, region


def _inner_shell_script(words: list[str]) -> Optional[str]:
    if not words:
        return None
    base = words[0].rsplit("/", 1)[-1]
    if base in _SHELLS:
        for j, w in enumerate(words[1:], start=1):
            if w in ("-c", "-lc", "-ec", "-xc") or (w.startswith("-") and w.endswith("c") and len(w) <= 4):
                return words[j + 1] if j + 1 < len(words) else None
    return None


def match_command(command: Any, rules: dict | None = None, *, _depth: int = 0) -> list[Match]:
    """Every billable create and every delete in *command*, in order. ``[]`` = not tracked."""
    if not isinstance(command, str) or "aws" not in command:
        return []
    rules = rules or load_rules()
    found: list[Match] = []
    for words in split_simple_commands(command):
        words = _strip_prefix(words)
        if not words:
            continue
        inner = _inner_shell_script(words)
        if inner is not None and _depth < 3:
            found.extend(match_command(inner, rules, _depth=_depth + 1))
            continue
        prog = words[0].rsplit("/", 1)[-1]
        if prog not in ("aws", "aws.exe", "aws.cmd"):
            continue
        parsed = _parse_aws(words)
        if parsed is None:
            continue
        service, operation, flags, positionals, region = parsed
        for action, section in (("create", "creates"), ("delete", "deletes")):
            for rule in rules[section]:
                if list(rule["op"]) == [service, operation]:
                    if "--generate-cli-skeleton" in flags or "--dry-run" in flags:
                        continue  # nothing is created or deleted
                    found.append(Match(action=action, key=rule["key"], kind=rule["kind"], rule=rule,
                                       flags=flags, positionals=positionals, region=region,
                                       segment=" ".join(words)))
                    break
    return found


def creates_in(command: Any) -> list[Match]:
    return [m for m in match_command(command) if m.action == "create"]
