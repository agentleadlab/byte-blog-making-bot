"""Ryte talking to Rook and Nova as the one whose job the setup QC is.

"i want ryte to be smarter than rook and nova, i want him to communicate his
needs to them to be able to fulfill his duty to me ... he doesnt have to use
templated message. if he thinks rook and nova did wrong, he has to let him
know - but ryte has to make sure that he is actually right and not confused,
so he has to ask them or the team to make sure".

Two things, both by Claude, both held to what Ryte actually read:

- `compose` writes the question: what Ryte sees, what it thinks is wrong and
  why, the exact figures, and what it needs from them - asking where it isn't
  sure rather than telling.
- `read_reply` reads what they answered against what Ryte sees: right as it
  is, fixed, taken to the team, waiting on the client, can't, or still wrong -
  and, when Ryte still thinks it's wrong or the answer doesn't add up, the
  one follow-up it should send.

Neither invents anything: every figure comes from the facts given. When
Claude can't be reached, the caller falls back to the plain question.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("wilbyte.rytevoice")

#: Who Ryte is, to Claude.
_WHO = (
    "You are Ryte, the setup QC lead at Agent Lead Lab (a lead-generation company). Rook and Nova are the team's "
    "AI assistants: Rook takes orders from clients, Nova sets them up on the hub (the system that delivers the "
    "leads) and pulls aged leads. Your job - and your pay depends on it - is that every paid order is set up exactly "
    "as it was paid for, and that nobody is missed. You answer to Franklin. You check Rook and Nova's work against "
    "the payment, Rook's setup and the hub.\n\n"
    "How you talk to Rook and Nova: like a sharp, respectful colleague who owns the outcome. Direct, specific, no "
    "filler, no greetings, no emojis, no bullet-point walls - a few short sentences. Quote the exact figures that "
    "disagree (counts, states, lead types, names, row numbers). Say plainly what you believe is wrong and why. "
    "When the facts could have an explanation you can't see (a renewal on the same row, states the client changed, "
    "a friend paying, a second product on the invoice), ASK instead of accusing - you must be right, not loud. "
    "Ask for exactly what you need to close it: a fix on the hub, a confirmation, or who has it. Say what you'll do "
    "next if it stays open (take it to Therese and Nicole). Never invent a fact that isn't in what you're given.")

#: How long a written question or reading may be.
MOST_CHARS = 1100


def _client(config):
    from anthropic import Anthropic

    config.secrets.require("anthropic_api_key")
    return Anthropic(api_key=config.secrets.anthropic_api_key)


def _ask_claude(config, system: str, user: str, tool: dict) -> dict | None:
    """One tool call to Claude - the careful model, then the quick one, each
    tried again when Claude's only busy. None when it couldn't be had."""
    from .bot.jobs import ORDER_MODEL, ORDER_READ_RETRY_WAITS, claude_busy

    try:
        client = _client(config)
    except Exception:
        return None
    for model in dict.fromkeys((getattr(getattr(config, "copy", None), "model", "") or ORDER_MODEL, ORDER_MODEL)):
        for wait in (0, *ORDER_READ_RETRY_WAITS):
            if wait:
                time.sleep(wait)
            try:
                response = client.messages.create(
                    model=model, max_tokens=900, system=system, tools=[tool],
                    tool_choice={"type": "tool", "name": tool["name"]},
                    messages=[{"role": "user", "content": user}])
            except Exception as exc:
                log.warning("Ryte's voice: %s failed", model, exc_info=True)
                if not claude_busy(exc):
                    break
                continue
            for block in getattr(response, "content", None) or []:
                if getattr(block, "type", "") == "tool_use":
                    return dict(getattr(block, "input", None) or {})
            break
    return None


def facts(result, *, invoice: str = "", amount: str = "", talk: list | None = None) -> dict:
    """What Ryte read for one payment, plainly, for Claude - nothing else."""
    from .rookqc import rook_setup

    paid = getattr(result, "paid", None) or getattr(result, "wanted", None)
    hub = getattr(result, "hub", None)
    rook = rook_setup(getattr(result, "setup_text", "")) if getattr(result, "setup_text", "") else None
    said = {
        "client": getattr(result, "client", "") or "",
        "payment": None if paid is None else {
            "amount": amount or getattr(paid, "amount", "") or "", "invoice": invoice,
            "lead_type": getattr(paid, "lead_type", "") or "", "count": getattr(paid, "count", None),
            "states": list(getattr(paid, "states", []) or []),
            "all_states_but": list(getattr(paid, "left_out", []) or []),
            "email": getattr(paid, "email", "") or ""},
        "rook_setup": None if rook is None else {
            "name": rook.name, "lead_type": rook.lead_type, "count": rook.count, "states": list(rook.states or []),
            "email": rook.email},
        "hub_row": None if hub is None else {
            "name": getattr(hub, "name", ""), "row": str(getattr(hub, "id", "") or ""),
            "lead_type": getattr(hub, "lead_type", ""), "status": getattr(hub, "status", ""),
            "ordered": getattr(hub, "ordered", None), "delivered": getattr(hub, "delivered", None),
            "states": list(getattr(hub, "states", ()) or ()), "has_sheet": bool(getattr(hub, "sheet_url", "")),
            "start": str(getattr(hub, "start_date", "") or "")},
        "what_ryte_sees_wrong": list(getattr(result, "problems", []) or []),
        "handed_to_the_team": list(dict.fromkeys(getattr(result, "todo", []) or [])),
        "notes": list(getattr(result, "notes", []) or [])[:8],
    }
    if talk:
        said["thread"] = [{"who": who, "said": " ".join(str(text).split())[:500]} for who, text in talk[-14:]]
    return said


_QUESTION = {
    "name": "ask", "description": "Ryte's message to Rook and Nova about this payment.",
    "input_schema": {"type": "object", "properties": {
        "message": {"type": "string", "description": (
            "The message, without the @ tags (they're added). A few short sentences: what you see, what you "
            "believe is wrong and why with the exact figures, and exactly what you need from them. Ask where an "
            "explanation you can't see is possible.")}},
        "required": ["message"]}}


def compose(config, said: dict) -> str:
    """Ryte's question for this payment, in its own words. "" when Claude
    couldn't write it - the caller falls back to the plain one."""
    got = _ask_claude(config, _WHO, "Write your message to Rook and Nova about this payment. What you read:\n"
                      + json.dumps(said, default=str, indent=1), _QUESTION)
    message = " ".join(str((got or {}).get("message") or "").split()) if got else ""
    return message[:MOST_CHARS]


#: What an answer can come to.
VERDICTS = ("right_as_is", "fixed", "with_team", "waiting_client", "cant", "still_wrong", "unclear")

_READING = {
    "name": "read", "description": "What Rook, Nova or the team answered, read against what Ryte sees.",
    "input_schema": {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS), "description": (
            "right_as_is: they explain it's correct as it is and the explanation holds up against the facts. "
            "fixed: they say it's fixed AND the hub row in the facts now shows it fixed. "
            "with_team: they've taken it to Therese / Nicole / Faith / the team. "
            "waiting_client: they're waiting on the client's answer before they can set it up. "
            "cant: they say they can't set it up. "
            "still_wrong: they answered but what they say doesn't match the facts (e.g. 'done' but the hub doesn't "
            "show it), or they missed the point - Ryte should follow up. "
            "unclear: nothing that answers it yet.")},
        "reason": {"type": "string", "description": "One sentence: why, citing their words and the facts."},
        "follow_up": {"type": "string", "description": (
            "Only for still_wrong: Ryte's follow-up to them, without @ tags - what still doesn't add up, the exact "
            "figures, and what's needed. Empty otherwise.")}},
        "required": ["verdict", "reason", "follow_up"]}}


def read_reply(config, said: dict, asked: str, replies: list) -> dict:
    """What their answers come to: {"verdict", "reason", "follow_up"}. {}
    when Claude couldn't read them."""
    body = ("You asked Rook and Nova about this payment, and they (or the team) answered. Read the answers against "
            "what you see and decide what they come to. Be strict: 'done' only counts as fixed if the hub row "
            "below shows it. What you read:\n" + json.dumps(said, default=str, indent=1)
            + "\n\nWhat you asked:\n" + str(asked)[:1500]
            + "\n\nTheir answers, oldest first:\n"
            + "\n".join(f"- {who}: {' '.join(str(text).split())[:700]}" for who, text in replies[-8:]))
    got = _ask_claude(config, _WHO, body, _READING)
    if not got or got.get("verdict") not in VERDICTS:
        return {}
    return {"verdict": got["verdict"], "reason": " ".join(str(got.get("reason") or "").split())[:300],
            "follow_up": " ".join(str(got.get("follow_up") or "").split())[:MOST_CHARS]}


def compose_chase(config, said: dict, question: str, whom: list) -> str:
    """Ryte to the teammates Nova asked, when nobody's answered: what's held
    up on it, and what's needed. "" when Claude couldn't write it."""
    body = (f"Nova asked {', '.join(whom)} a question about this payment and nobody has answered. The setup is "
            "held up on it. Write your message to them (people on the team, not bots): a few short sentences - "
            "what's waiting on their answer, the exact figures, and the one thing you need from them. No greeting. "
            f"Nova's question: {question}\n\nWhat you read:\n" + json.dumps(said, default=str, indent=1))
    got = _ask_claude(config, _WHO, body, _QUESTION)
    return " ".join(str((got or {}).get("message") or "").split())[:MOST_CHARS] if got else ""
