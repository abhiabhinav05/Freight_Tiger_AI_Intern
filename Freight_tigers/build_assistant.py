#!/usr/bin/env python3
"""Build the route-week cost report: rates, baselines, flags, cited notes, and reasons.

    python build_assistant.py            # deterministic, no LLM (the submitted output)
    python build_assistant.py --llm      # also use an OpenAI-compatible LLM (see README)
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from watcher import config
from watcher.costs import load_shipments, own_text, peer_text, read_csv, route_weeks
from watcher.explain import flagged_value, llm_note_disputes, llm_reword, template_reason
from watcher.llm import LLMClient, LLMUnavailable
from watcher.notes import Verdict, judge, load_notes, mark_disputed
from watcher.retrieval import NoteIndex


def check_template() -> None:
    headers = list(read_csv(config.TEMPLATE)[0])
    if headers != config.OUTPUT_COLUMNS:
        raise ValueError(f"Template columns {headers} differ from the contract {config.OUTPUT_COLUMNS}")


def build(client: LLMClient | None = None) -> tuple[list[dict[str, str]], dict]:
    check_template()
    shipments = load_shipments(config.SHIPMENTS)
    notes = load_notes(config.NOTES)
    disputes = llm_note_disputes(client, notes) if client else {}
    notes = mark_disputed(notes, set(disputes))
    index = NoteIndex(notes)

    rows = []
    for rw in route_weeks(shipments):
        # Notes are only consulted for alerts; matched_note_id stays blank otherwise.
        verdict = judge(rw, index.for_route_week(rw.route, rw.week)) if rw.alert else Verdict(None, None)
        nearby = []
        if rw.alert and not verdict.note and not verdict.partial:
            nearby = index.nearby(rw.route, rw.week, config.NEARBY_DAYS)[:2]
        flagged = flagged_value(rw, verdict)
        reason = template_reason(rw, verdict, nearby)
        if client and rw.alert:
            reason = llm_reword(client, rw, flagged, verdict.note, reason)
        rows.append({
            "route": rw.route,
            "week_of": rw.week.isoformat(),
            "cost_per_tonne_km": f"{rw.rate:.2f}",
            "vs_own_history": own_text(rw),
            "vs_similar_routes": peer_text(rw),
            "flagged": flagged,
            "matched_note_id": verdict.note.note_id if verdict.note else "",
            "reason": reason,
        })
    stats = {"shipments": len(shipments), "notes": len(notes), "disputes": disputes}
    return rows, stats


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=config.OUTPUT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_cost_log(rows: list[dict[str, str]], stats: dict, client: LLMClient | None) -> None:
    counts = {v: sum(r["flagged"] == v for r in rows) for v in ("Yes", "No (justified)", "No")}
    lines = [
        f"Input shipments: {stats['shipments']}",
        f"Context notes: {stats['notes']}",
        f"Output route-weeks: {len(rows)}",
        f"Flagged Yes: {counts['Yes']}, No (justified): {counts['No (justified)']}, No: {counts['No']}",
        "Retrieval: in-process TF-IDF index, no API calls, no tokens.",
    ]
    if client is None:
        lines += ["Mode: deterministic (no LLM)", "LLM calls: 0", "Input tokens: 0",
                  "Output tokens: 0", "Model cost: USD 0.00",
                  "Randomness: none; all calculations and decisions are deterministic."]
    else:
        u = client.usage
        lines += [
            f"Mode: LLM-assisted, model={client.model} at {client.base_url}",
            f"LLM calls: {u.calls} ({u.live_calls} live, {u.cached_calls} replayed from cache)",
            f"Input tokens (full run): {u.input_tokens}",
            f"Output tokens (full run): {u.output_tokens}",
            f"Model cost at USD {client.price_in_per_m}/{client.price_out_per_m} per 1M in/out: "
            f"USD {client.cost_usd:.4f}",
            "Randomness: temperature 0, seed 0, responses cached by prompt hash; verdicts are "
            "decided by code, the LLM can only veto a note or reword a reason.",
            f"Notes disputed by LLM (cannot justify): {stats['disputes'] or 'none'}",
            f"Rewordings rejected by guardrail: {len(u.errors)}",
        ]
    config.COST_LOG.parent.mkdir(parents=True, exist_ok=True)
    config.COST_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=config.OUTPUT)
    parser.add_argument("--llm", action="store_true", help="use the LLM configured by LLM_* env vars")
    parser.add_argument("--no-log", action="store_true", help="do not overwrite the cost log")
    args = parser.parse_args()

    client = LLMClient.from_env(config.LLM_CACHE) if args.llm else None
    try:
        rows, stats = build(client)
    except LLMUnavailable as e:
        sys.exit(f"{e}\nStart Ollama (or set LLM_BASE_URL/LLM_MODEL/LLM_API_KEY), or run without --llm.")
    write_csv(rows, args.output)
    if not args.no_log:
        write_cost_log(rows, stats, client)
    flagged = sum(r["flagged"] == "Yes" for r in rows)
    justified = sum(r["flagged"] == "No (justified)" for r in rows)
    print(f"Wrote {len(rows)} route-weeks ({flagged} flagged, {justified} justified) to {args.output}")


if __name__ == "__main__":
    main()
