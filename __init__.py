"""Lights Out for Hermes Agent: nothing billable starts without your yes and its price, and every
session ends with a receipt of what is still running.

This file only wires Hermes's public plugin surfaces (hooks, one slash command, two tools) to the
deterministic core in ``lights_out/``. It never answers an approval, never patches Hermes, and
holds no secrets.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from .lights_out.service import LightsOut

log = logging.getLogger("lights-out")
PLUGIN = "lights-out"

LIGHTS_STATUS_SCHEMA = {
    "name": "lights_status",
    "description": (
        "Read-only. Lights Out's ledger of cloud resources created through the AWS CLI in this and "
        "earlier sessions: what is still running, its $/month, what is kept on purpose until a date, "
        "creates whose ID couldn't be read, and the command that turns each one off. Use it before "
        "telling the user that nothing is left running."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}
LIGHTS_KEEP_SCHEMA = {
    "name": "lights_keep",
    "description": (
        "Propose keeping a running cloud resource on purpose until a date, so it isn't flagged as a "
        "leftover (for example a demo that must stay up through judging). This is only a proposal: "
        "the user is asked to approve it, with the cost by that date. Use it only when the user asked "
        "for the resource to stay up. 'until' is a date (2026-12-15) or a length (10d, 2w, 2mo); "
        "the keep ends at 23:59 that day in the user's time zone."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Resource ID from lights_status, e.g. i-0abc..."},
            "until": {"type": "string", "description": "2026-12-15, eod 2026-12-15, 10d, 2w or 2mo"},
            "why": {"type": "string", "description": "Short reason the user gave, e.g. 'judging ends'"},
        },
        "required": ["id", "until"],
    },
}

HELP = """Lights Out
  /lights                      receipt: what's running, kept, unknown, and the $/month
  /lights out [yes]            turn off everything still running that isn't kept (asks first)
  /lights keep <id> <when> [why]   keep one on purpose: 2026-12-15, eod 2026-12-15, 10d, 2w, 2mo
  /lights unkeep <id>          remove a keep
  /lights check                compare the ledger with the cloud (read-only aws describe calls)
  /lights status               the ledger as JSON (same as the lights_status tool)"""


def _ledger_path() -> Path:
    override = os.environ.get("LIGHTS_OUT_LEDGER", "").strip()   # a file path, for tests; not a secret
    if override:
        return Path(override)
    from plugins.plugin_storage import plugin_data_dir   # <HERMES_HOME>/plugin-data/lights-out/, per profile
    return plugin_data_dir(PLUGIN) / "ledger.jsonl"


def register(ctx) -> None:
    def cfg(key: str, default: Any) -> Any:
        try:
            val = ctx.get_config(key, default=default)
        except Exception:
            return default
        return default if val is None else val

    def lo() -> LightsOut:
        return LightsOut(_ledger_path(), tz_name=str(cfg("timezone", "") or ""),
                         style=str(cfg("receipt_style", "bill")))

    def session_of(kw: dict) -> str:
        return str(kw.get("session_id") or kw.get("conversation_id") or kw.get("task_id") or "")

    def use_color(platform: str) -> bool:
        mode = str(cfg("color", "auto")).lower()
        return mode == "always" or (mode == "auto" and platform in ("cli", ""))

    def run_terminal(command: str) -> Any:
        # Hermes's own terminal tool: its guards and the user's approval rules apply as usual.
        return ctx.dispatch_tool("terminal", {"command": command})

    # -- hooks -------------------------------------------------------------------------------
    def pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kw) -> Optional[dict]:
        args = args or {}
        if tool_name == "terminal":
            return lo().pre_terminal(args.get("command"))
        if tool_name == "lights_keep":
            return lo().pre_keep(args)
        return None

    def post_tool_call(tool_name: str = "", args: Optional[dict] = None, result: Any = None, **kw) -> None:
        if tool_name != "terminal":
            return None
        try:
            lo().post_terminal((args or {}).get("command"), result, session_of(kw), run_terminal=run_terminal)
        except Exception:
            log.exception("lights-out: could not record a terminal call")
        return None

    def summary_llm(facts: str) -> str:
        res = ctx.llm.complete(
            messages=[
                {"role": "system", "content": (
                    "You write one short plain-English sentence (under 25 words) for a cloud bill receipt. "
                    "Use only the facts given. Do not add numbers that are not in the facts. "
                    "If all_clear is false, do not say everything is off.")},
                {"role": "user", "content": facts},
            ],
            max_tokens=80, temperature=0.2, purpose="lights-out.receipt-sentence",
        )
        return getattr(res, "text", "") or ""

    def on_session_finalize(session_id: Optional[str] = None, platform: str = "", **kw) -> Optional[str]:
        if not session_id:
            return None
        try:
            svc = lo()
            if not svc.touched(session_id):
                return None
            sentence = svc.summary_sentence(summary_llm if cfg("summary_sentence", True) else None)
            return svc.receipt(color=use_color(platform), sentence=sentence, session=session_id)
        except Exception:
            log.exception("lights-out: receipt failed")
            return "Lights Out: the receipt could not be built this time; run /lights to see the ledger."

    _notices: dict[str, str] = {}
    _posted: dict[str, int] = {}     # session -> ledger size when its receipt was last posted

    def on_session_end(session_id: str = "", **kw) -> None:
        """After a turn that changed the ledger, post the receipt under Hermes's reply. (Hermes's CLI
        detaches on /exit and the gateway drops finalize text, so this is what a CLI user sees.)"""
        if not session_id:
            return None
        try:
            path = _ledger_path()
            size = path.stat().st_size if path.exists() else 0
            if size == _posted.get(session_id, 0):
                return None
            _posted[session_id] = size
            svc = lo()
            if svc.touched(session_id):
                ctx.post_message(svc.receipt(color=str(cfg("color", "auto")).lower() == "always",
                                             session=session_id))
        except Exception:
            log.exception("lights-out: per-turn receipt failed")
        return None

    def on_session_start(session_id: str = "", **kw) -> None:
        try:
            note = lo().notice(session_id)
        except Exception:
            log.exception("lights-out: session notice failed")
            return None
        if note:
            _notices[session_id] = note
            try:
                ctx.post_message(note)
            except Exception:
                pass
        return None

    def pre_llm_call(session_id: str = "", **kw) -> Optional[dict]:
        note = _notices.pop(session_id, "")
        if not note:
            return None
        return {"context": note + " Tell the user about this before anything else."}

    # -- tools -------------------------------------------------------------------------------
    def lights_status(args: dict, **kw) -> str:
        try:
            return json.dumps(lo().status())
        except Exception as e:
            return json.dumps({"error": f"lights_status failed: {type(e).__name__}: {e}"})

    def lights_keep(args: dict, **kw) -> str:
        # Runs only after pre_tool_call sent the proposal to Hermes's approval gate and a person said yes.
        try:
            msg = lo().keep(str(args.get("id", "")), str(args.get("until", "")), str(args.get("why", "") or ""),
                            by="approved proposal from Hermes", session=session_of(kw))
            return json.dumps({"result": msg} if not msg.startswith("Lights Out:") else {"error": msg})
        except Exception as e:
            return json.dumps({"error": f"lights_keep failed: {type(e).__name__}: {e}"})

    # -- /lights -----------------------------------------------------------------------------
    def lights_command(raw_args: str = "", **kw) -> str:
        session = session_of(kw)
        parts = (raw_args or "").split()
        sub = parts[0].lower() if parts else ""
        svc = lo()
        try:
            if sub in ("", "receipt", "ledger"):
                return svc.receipt(color=use_color(str(kw.get("platform", ""))), session=session)
            if sub == "status":
                return json.dumps(svc.status(), indent=2)
            if sub == "out":
                confirmed = len(parts) > 1 and parts[1].lower() in ("yes", "y")
                return svc.lights_out(run_terminal, ctx.ask_choice, confirmed=confirmed, session=session)
            if sub == "keep":
                if len(parts) < 3:
                    return "Usage: /lights keep <id> <2026-12-15 | eod 2026-12-15 | 10d | 2w | 2mo> [why]"
                rid, rest = parts[1], parts[2:]
                when = " ".join(rest[:2]) if rest[0].lower() == "eod" and len(rest) > 1 else rest[0]
                why = " ".join(rest[2:] if rest[0].lower() == "eod" else rest[1:])
                return svc.keep(rid, when, why, by="you (/lights keep)", session=session)
            if sub == "unkeep" and len(parts) > 1:
                return svc.unkeep(parts[1], session)
            if sub == "check":
                return svc.check(run_terminal, session=session)
            return HELP
        except Exception as e:
            log.exception("lights-out: /lights failed")
            return f"Lights Out: /lights {sub} failed ({type(e).__name__}: {e}). The ledger is unchanged."

    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_tool(name="lights_status", toolset="lights-out", schema=LIGHTS_STATUS_SCHEMA,
                      handler=lights_status, description="Lights Out ledger (read-only)", emoji="💡")
    ctx.register_tool(name="lights_keep", toolset="lights-out", schema=LIGHTS_KEEP_SCHEMA,
                      handler=lights_keep, description="Propose keeping a resource until a date (asks the user)",
                      emoji="💡")
    ctx.register_command("lights", handler=lights_command,
                         description="Cloud resources Hermes started: receipt, turn off, keep, check",
                         args_hint="[out|keep|unkeep|check|status]")
