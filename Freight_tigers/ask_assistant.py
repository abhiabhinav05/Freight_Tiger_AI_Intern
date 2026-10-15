"""Plain-English, deterministic Q&A over the generated route-week CSV and the context notes.

Answers are assembled only from the checked output rows and the source notes, so the
assistant cannot invent a cause. Questions about notes use the same TF-IDF index as the build.
"""
import argparse
import calendar
import csv
import re

from watcher import config
from watcher.notes import load_notes
from watcher.retrieval import NoteIndex

RESULTS = config.OUTPUT
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})


def search_notes(question: str) -> str:
    hits = [(score, n) for score, n in NoteIndex(load_notes(config.NOTES)).search(question) if score > 0][:3]
    if not hits:
        return "No context note matches that question."
    return "\n".join(f"{n.note_id} ({n.date.isoformat()}, {n.applies_to}): {n.text}" for _, n in hits)


def answer(question: str) -> str:
    if not RESULTS.exists():
        return "I don't have results yet. Run `python build_assistant.py` first."
    with RESULTS.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    q = question.lower()
    routes = sorted({r["route"] for r in rows}, key=lambda route: (-len(route), route))
    route = next((r for r in routes if r.lower() in q), None)
    if not route:
        if "note" in q:
            return search_notes(question)
        return "I couldn't identify a route in that question. Include a route such as `Chennai-Bangalore`."

    month = next((number for name, number in MONTHS.items()
                  if re.search(rf"\b{re.escape(name)}\b", q)), None)
    year_match = re.search(r"\b(2024|2025)\b", q)
    year = year_match.group(1) if year_match else None
    selected = [r for r in rows if r["route"] == route]
    if month:
        selected = [r for r in selected if int(r["week_of"][5:7]) == month
                    and (year is None or r["week_of"][:4] == year)]
    elif year:
        selected = [r for r in selected if r["week_of"].startswith(year)]
    if not selected:
        when = f"{calendar.month_name[month]} {year or ''}".strip() if month else (year or "the requested period")
        return f"I found no shipment weeks for {route} in {when}."

    # Explain every elevated result, including rises that a valid note justified.
    elevated = [r for r in selected if r["flagged"] in {"Yes", "No (justified)"}]
    flagged = [r for r in elevated if r["flagged"] == "Yes"]
    justified = [r for r in elevated if r["flagged"] == "No (justified)"]
    if elevated:
        focus = sorted(elevated, key=lambda r: r["week_of"])
    else:
        focus = [max(selected, key=lambda r: float(r["cost_per_tonne_km"]))]
    title = f"{route}"
    if month:
        title += f" in {calendar.month_name[month]}{f' {year}' if year else ''}"
    elif year:
        title += f" in {year}"
    if elevated:
        lines = [f"{title}: {len(elevated)} elevated week(s); {len(justified)} explained by notes, {len(flagged)} flagged for review."]
        for r in focus:
            lines.append(f"Week of {r['week_of']}: INR {r['cost_per_tonne_km']} per tonne-km; "
                         f"{r['vs_own_history']}; {r['vs_similar_routes']}. {r['reason']}")
        return "\n".join(lines)
    r = focus[0]
    return (f"{title}: no week was flagged. The highest weekly rate was INR {r['cost_per_tonne_km']} "
            f"per tonne-km in the week of {r['week_of']}; {r['vs_own_history']}; "
            f"{r['vs_similar_routes']}. {r['reason']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="*", help="Question, including a route and optionally a month/year")
    args = parser.parse_args()
    if args.question:
        print(answer(" ".join(args.question)))
        return
    print("Ask about a route and optionally a month/year (for example: Why did Chennai-Bangalore get pricier in March 2025?).")
    print("Type `quit` to exit.")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in {"quit", "exit"}:
            break
        print("Assistant: " + answer(question))


if __name__ == "__main__":
    main()
