"""Plain-English reasons, plus the two optional LLM steps and the guardrails around them.

The verdict (flag, matched note) is always decided by code in notes.py. The LLM may only
(1) veto a note it reads differently - which can make a row MORE cautious, never less - and
(2) reword an alert's reason; a rewording that changes facts is rejected for the template.
"""
from __future__ import annotations

import json
import re

from .costs import RouteWeek, own_text, peer_text
from .llm import LLMClient
from .notes import Note, Verdict, check_note

NOTE_ID_RE = re.compile(r"\bN\d{3}\b")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def flagged_value(rw: RouteWeek, verdict: Verdict) -> str:
    if not rw.alert:
        return "No"
    return "No (justified)" if verdict.note else "Yes"


def _quote(note: Note) -> str:
    return f'note {note.note_id} ({note.date.isoformat()}, {note.applies_to}): "{note.text}"'


def _rise(rw: RouteWeek) -> str:
    parts = []
    if rw.own_elevated:
        parts.append(f"{rw.own_delta:+.1%} vs its own {rw.own_weeks}-week average")
    if rw.peer_elevated:
        parts.append(f"{rw.peer_delta:+.1%} vs similar-length routes")
    text = f"INR {rw.rate:.2f} per tonne-km is " + " and ".join(parts)
    if not rw.own_elevated:
        text += f", while still rising ({rw.own_delta:+.1%} vs its own {rw.own_weeks}-week average)"
    return text


def template_reason(rw: RouteWeek, verdict: Verdict, nearby: list[Note]) -> str:
    if not rw.alert:
        if rw.own_delta is None:
            return "Not flagged: first week on record for this route, so there is no history to show a rise."
        if not rw.rising and rw.peer_delta is not None and rw.peer_delta > 0:
            return (f"Not flagged: {peer_text(rw)}, but {own_text(rw)}, so the cost is not rising; "
                    "this route normally runs above its peers.")
        return f"Not flagged: within the normal range ({own_text(rw)}; {peer_text(rw)})."

    if verdict.note:
        n = verdict.note
        return (f"Justified: {_rise(rw)}. This is explained by {_quote(n)} "
                f"It applies to this route over {n.window_basis}, which includes the week of "
                f"{rw.week.isoformat()}, so the rise is not flagged.")
    if verdict.partial:
        p = verdict.partial
        return (f"Flagged for review: {_rise(rw)}. The closest cost note, {_quote(p)} "
                f"is relevant but {verdict.partial_reason}.")
    if nearby:
        checked = " ".join(f"{_quote(n)} {check_note(n, rw)[1]}." for n in nearby)
        return f"Flagged for review: {_rise(rw)}. No note justifies it. Nearby notes checked: {checked}"
    return (f"Flagged for review: {_rise(rw)}. No context note covers this route around this week, "
            "so the rise is unexplained.")


# --- Optional LLM step 1: second reading of each note -------------------------------------

NOTE_SYSTEM = ("You classify logistics notes. Answer with JSON only: "
               '{"raises_costs": true|false}. raises_costs is true only if the note says freight '
               "costs, rates, prices or surcharges went UP because of the event. Notes saying costs "
               "were unaffected, stable, normal, absorbed, or not relevant are false.")


def llm_note_disputes(client: LLMClient, notes: list[Note]) -> dict[str, str]:
    """Return {note_id: explanation} for notes where the LLM disagrees with the rule reading."""
    disputes = {}
    for note in notes:
        answer = client.chat(NOTE_SYSTEM, f"Note: {note.text}", max_tokens=30)
        m = re.search(r"\{.*?\}", answer, re.S)
        try:
            llm_raises = bool(json.loads(m.group(0))["raises_costs"]) if m else None
        except (ValueError, KeyError):
            llm_raises = None
        if llm_raises is None:
            disputes[note.note_id] = f"unparseable LLM answer: {answer[:80]!r}"
        elif llm_raises != note.raises_costs:
            disputes[note.note_id] = f"rules say raises_costs={note.raises_costs}, LLM says {llm_raises}"
    return disputes


# --- Optional LLM step 2: reword alert reasons, with a fact guardrail ---------------------

REWRITE_SYSTEM = ("You explain freight cost alerts to an operations manager in at most two plain "
                  "sentences. Use only the facts given. Do not add causes, numbers, dates or note "
                  "IDs that are not in the facts. Keep the verdict exactly as given, and if a note "
                  "is cited, name its ID and date.")


def guardrail_ok(text: str, draft: str, flagged: str, note: Note | None) -> bool:
    if not text or len(text) > 700:
        return False
    if set(NOTE_ID_RE.findall(text)) - set(NOTE_ID_RE.findall(draft)):
        return False                                    # invented or wrong note
    if not set(NUMBER_RE.findall(text)) <= set(NUMBER_RE.findall(draft)):
        return False                                    # invented number or date
    lower = text.lower()
    if note and (note.note_id not in text or note.date.isoformat() not in text):
        return False                                    # justified rows must cite id + date
    if flagged == "Yes" and "review" not in lower and "flag" not in lower:
        return False
    if flagged == "No (justified)" and ("unexplained" in lower or "review" in lower):
        return False
    return True


def llm_reword(client: LLMClient, rw: RouteWeek, flagged: str, note: Note | None, draft: str) -> str:
    facts = (f"Route: {rw.route}\nWeek of: {rw.week.isoformat()}\nVerdict: {flagged}\n"
             f"Draft explanation (all facts you may use): {draft}")
    text = client.chat(REWRITE_SYSTEM, facts, max_tokens=160)
    if guardrail_ok(text, draft, flagged, note):
        return text
    client.usage.errors.append(f"rewording rejected for {rw.route} {rw.week}: kept template")
    return draft
