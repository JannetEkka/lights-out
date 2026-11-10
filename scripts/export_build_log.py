"""Export the build session's chat log (Claude Code JSONL) to docs/build-log.md.

Raw and in order: every user message and every assistant reply verbatim, every tool call with its
full input. Tool results longer than RESULT_MAX characters are cut, with a marker saying how much
was cut. Hidden reasoning blocks are not part of the chat and are left out. Personal email
addresses are replaced with [email]. Nothing is reordered or paraphrased.

Usage: python scripts/export_build_log.py <session.jsonl> > docs/build-log.md
"""
import json
import re
import sys

RESULT_MAX = 4000
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?!anthropic\.com|users\.noreply)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


LOCAL_PART = re.compile(r"jannet\\?\.ekka", re.I)


def scrub(s: str) -> str:
    return LOCAL_PART.sub("[email]", EMAIL.sub("[email]", s))


def fence(s: str) -> str:
    n = 3
    while "`" * n in s:
        n += 1
    return "`" * n


def block(s: str, lang: str = "") -> str:
    f = fence(s)
    return f"{f}{lang}\n{s}\n{f}"


def main(path: str) -> None:
    out = ["# Lights Out: build session chat log", "",
           "Raw export of the build session (https://claude.ai/code/session_016VDQom3X84652goidUewwo), "
           "from the build prompt on, in order. See scripts/export_build_log.py for exactly what is "
           "included and what is cut.", ""]
    seen = set()
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") not in ("user", "assistant") or r.get("isMeta"):
            continue
        uid = r.get("uuid")
        if uid in seen:
            continue
        seen.add(uid)
        msg = r.get("message") or {}
        ts = (r.get("timestamp") or "")[:19].replace("T", " ")
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for c in content or []:
            t = c.get("type")
            if t == "text" and c.get("text", "").strip():
                who = "User / harness message" if r["type"] == "user" else "Claude"
                out += [f"### {who} · {ts} UTC", "", scrub(c["text"]), ""]
            elif t == "tool_use":
                out += [f"**Tool call** `{c.get('name')}` · {ts} UTC", "",
                        block(scrub(json.dumps(c.get("input"), indent=1, ensure_ascii=False)), "json"), ""]
            elif t == "tool_result":
                res = c.get("content")
                if isinstance(res, list):
                    res = "\n".join(x.get("text", "[" + x.get("type", "item") + "]") for x in res)
                res = scrub(str(res or ""))
                if len(res) > RESULT_MAX:
                    res = res[:RESULT_MAX] + f"\n[... {len(res) - RESULT_MAX} more characters cut in this export]"
                out += ["Result:", "", block(res), ""]
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1])
