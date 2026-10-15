"""Evaluation harness: contract, independent recomputation, labeled sets, guardrails, reproducibility.

    python verify_output.py

Exits non-zero on the first failed check. Writes logs/reproducibility_check.txt.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from ask_assistant import answer  # noqa: E402
from build_assistant import build  # noqa: E402
from watcher import config  # noqa: E402
from watcher.llm import LLMClient  # noqa: E402
from watcher.notes import load_notes  # noqa: E402


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def check_contract(rows: list[dict[str, str]]) -> None:
    assert list(rows[0]) == config.OUTPUT_COLUMNS, list(rows[0])
    assert list(read(config.TEMPLATE)[0]) == config.OUTPUT_COLUMNS
    assert len({(r["route"], r["week_of"]) for r in rows}) == len(rows), "duplicate route-week"
    for r in rows:
        assert date.fromisoformat(r["week_of"]).weekday() == 0, r
        assert r["flagged"] in {"Yes", "No", "No (justified)"}, r
        assert bool(r["matched_note_id"]) == (r["flagged"] == "No (justified)"), r
        assert r["reason"], r


def check_recomputed(rows: list[dict[str, str]]) -> None:
    """Rebuild every rate, baseline and flag from raw shipments with separate code."""
    totals = defaultdict(lambda: [0.0, 0.0])
    rtype = {}
    for s in read(config.SHIPMENTS):
        route = f"{s['origin']}-{s['destination']}"
        d = date.fromisoformat(s["shipment_date"])
        totals[(route, d - timedelta(days=d.weekday()))][0] += float(s["freight_cost_inr"])
        totals[(route, d - timedelta(days=d.weekday()))][1] += float(s["quantity_tonnes"]) * float(s["distance_km"])
        rtype[route] = s["route_type"]
    rates = {k: c / tk for k, (c, tk) in totals.items()}
    out = {(r["route"], date.fromisoformat(r["week_of"])): r for r in rows}
    assert set(out) == set(rates), "output route-weeks differ from source route-weeks"

    for (route, week), rate in rates.items():
        r = out[(route, week)]
        hist = [rates[(route, week - timedelta(weeks=i))] for i in range(1, 9) if (route, week - timedelta(weeks=i)) in rates]
        peers = [v for (o, w), v in rates.items() if w == week and o != route and rtype[o] == rtype[route]]
        own = (rate / (sum(hist) / len(hist)) - 1) if hist else None
        peer = (rate / (sum(peers) / len(peers)) - 1) if peers else None
        assert r["cost_per_tonne_km"] == f"{rate:.2f}", (route, week)
        assert r["vs_own_history"] == (f"{own * 100:+.1f}% vs this route's past average" if own is not None
                                       else "N/A - no prior weeks for this route"), (route, week)
        assert r["vs_similar_routes"] == (f"{peer * 100:+.1f}% vs similar-length routes this week" if peer is not None
                                          else "N/A - no other routes of this type this week"), (route, week)
        rising = own is not None and own > 0
        alert = (own is not None and own >= config.OWN_THRESHOLD) or (rising and peer is not None and peer >= config.PEER_THRESHOLD)
        assert (r["flagged"] != "No") == alert, (route, week, r["flagged"], own, peer)


def check_labeled_notes() -> int:
    notes = {n.note_id: n for n in load_notes(config.NOTES)}
    labels = read(config.LABELED_NOTES)
    for lab in labels:
        n = notes[lab["note_id"]]
        got = (str(n.raises_costs), n.start.isoformat(), n.end.isoformat())
        want = (lab["expected_raises_costs"], lab["expected_start"], lab["expected_end"])
        assert got == want, (lab["note_id"], got, want, lab["why"])
    return len(labels)


def check_labeled_cases(rows: list[dict[str, str]]) -> int:
    out = {(r["route"], r["week_of"]): r for r in rows}
    labels = read(config.LABELED_CASES)
    for lab in labels:
        r = out[(lab["route"], lab["week_of"])]
        got = (r["flagged"], r["matched_note_id"])
        assert got == (lab["expected_flagged"], lab["expected_note_id"]), (lab, got)
    return len(labels)


def check_citations(rows: list[dict[str, str]]) -> int:
    """Every cited note must raise costs, cover the route and the week, and be quoted with its date."""
    notes = {n.note_id: n for n in load_notes(config.NOTES)}
    cited = [r for r in rows if r["matched_note_id"]]
    for r in cited:
        n = notes[r["matched_note_id"]]
        week = date.fromisoformat(r["week_of"])
        assert n.raises_costs and n.applies_to_route(r["route"]) and n.overlaps(week), r
        assert n.note_id in r["reason"] and n.date.isoformat() in r["reason"] and n.text in r["reason"], r
    for r in rows:
        if r["flagged"] == "Yes":
            assert "Flagged for review" in r["reason"], r
    return len(cited)


class FakeLLM(LLMClient):
    """Stands in for an endpoint so the LLM path and its guardrails can be tested offline."""

    def __init__(self, cache_path: Path, note_answers: dict[str, str], reword):
        super().__init__("http://fake", "fake-model", "", cache_path)
        self.note_answers, self.reword = note_answers, reword

    def _post(self, system, user, max_tokens):
        if user.startswith("Note: "):
            text = next((a for phrase, a in self.note_answers.items() if phrase in user), None)
            text = text or ('{"raises_costs": true}' if any(w in user for w in ("surcharge", "higher trip", "Diesel"))
                            else '{"raises_costs": false}')
        else:
            text = self.reword(user)
        return {"text": text, "input_tokens": len(system + user) // 4, "output_tokens": len(text) // 4}


def check_llm_guardrails(baseline: list[dict[str, str]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # 1. A faithful rewording is accepted; verdicts and IDs are untouched.
        def faithful(user):
            draft = user.split("Draft explanation (all facts you may use): ", 1)[1]
            return draft.split(". ", 1)[0] + "."  if "Flagged" in draft else draft
        client = FakeLLM(Path(tmp) / "a.json", {}, faithful)
        rows, _ = build(client)
        assert [(r["flagged"], r["matched_note_id"]) for r in rows] == \
               [(r["flagged"], r["matched_note_id"]) for r in baseline]
        assert client.usage.live_calls > 0 and not client.usage.errors, client.usage.errors

        # 2. A rewording that invents a note and a number is rejected for the template.
        client = FakeLLM(Path(tmp) / "b.json", {}, lambda u: "Justified by note N099 after a 50% toll hike.")
        rows, _ = build(client)
        assert rows == baseline, "a hallucinated rewording leaked into the output"
        assert len(client.usage.errors) == sum(r["flagged"] != "No" for r in baseline)

        # 3. If the LLM reads N002 (the festival note) differently, the row becomes MORE cautious (flagged), never less.
        client = FakeLLM(Path(tmp) / "c.json", {"festival": '{"raises_costs": false}'}, faithful)
        rows, stats = build(client)
        row = next(r for r in rows if r["route"] == "Ahmedabad-Mumbai" and r["week_of"] == "2025-01-20")
        assert row["flagged"] == "Yes" and not row["matched_note_id"] and "N002" in stats["disputes"]

        # 4. A second run replays the cache: zero live calls, identical output.
        again = FakeLLM(Path(tmp) / "c.json", {"festival": '{"raises_costs": false}'}, faithful)
        assert build(again)[0] == rows and again.usage.live_calls == 0


def check_qa() -> None:
    a = answer("Why did Ahmedabad-Mumbai get pricier in January 2025?")
    assert "N002" in a and "1 explained by notes" in a, a
    a = answer("Why was Mumbai-Pune flagged in September 2025?")
    assert "N006" in a and "flagged for review" in a.lower(), a
    a = answer("Why did Chennai-Bangalore get pricier in March 2025?")
    assert "N001" in a and "2 flagged for review" in a, a
    assert "couldn't identify a route" in answer("Why did an unknown route get expensive?")
    assert "N001" in answer("Which notes mention flooding?")


def check_reproducible(rows: list[dict[str, str]]) -> str:
    """Run the CLI three times into fresh files; all three must be byte-identical."""
    with tempfile.TemporaryDirectory() as tmp:
        outputs = []
        for i in range(3):
            path = Path(tmp) / f"run_{i + 1}.csv"
            subprocess.run([sys.executable, str(ROOT / "build_assistant.py"), "--output", str(path), "--no-log"],
                           check=True, capture_output=True, text=True)
            outputs.append(path.read_bytes())
        hashes = [hashlib.sha256(b).hexdigest() for b in outputs]
        diffs = [list(difflib.unified_diff(outputs[0].decode().splitlines(), b.decode().splitlines(), lineterm=""))
                 for b in outputs[1:]]
        assert read(Path(tmp) / "run_1.csv") == rows
    flags = sum(r["flagged"] == "Yes" for r in rows)
    report = ["Reproducibility check: 3 untouched runs of `python build_assistant.py` on the same input.", ""]
    report += [f"run {i + 1}: sha256 {h}" for i, h in enumerate(hashes)]
    report += ["", f"diff run1 vs run2: {len(diffs[0])} differing lines",
               f"diff run1 vs run3: {len(diffs[1])} differing lines",
               f"rows: {len(rows)}, flagged Yes: {flags}, justified: {sum(r['flagged'] == 'No (justified)' for r in rows)}",
               "Result: " + ("IDENTICAL - flags, numbers, cited notes and wording all match."
                             if len(set(hashes)) == 1 else "MISMATCH")]
    config.REPRO_LOG.parent.mkdir(parents=True, exist_ok=True)
    config.REPRO_LOG.write_text("\n".join(report) + "\n", encoding="utf-8")
    assert len(set(hashes)) == 1, hashes
    return hashes[0]


def main() -> None:
    if not config.OUTPUT.exists():
        sys.exit("Run `python build_assistant.py` first.")
    rows = read(config.OUTPUT)
    built, _ = build()
    assert rows == built, "output/shipment_cost_alerts.csv is stale - rerun build_assistant.py"

    check_contract(rows)
    print(f"PASS contract: {len(rows)} rows, exact columns, Monday weeks, note id only on justified rows")
    check_recomputed(rows)
    print("PASS recomputation: every rate, both baselines and every flag rebuilt independently")
    print(f"PASS labeled notes: {check_labeled_notes()}/10 notes read correctly (effect + window)")
    print(f"PASS labeled cases: {check_labeled_cases(rows)} hand-labeled route-weeks match")
    print(f"PASS citations: {check_citations(rows)} justified rows cite a covering cost note with id, date, text")
    check_llm_guardrails(rows)
    print("PASS LLM guardrails (offline fake): faithful rewording kept, hallucinated note/number rejected, "
          "LLM note veto only adds caution, cache replay identical")
    check_qa()
    print("PASS Q&A: cited answers, missing-route handling, note search")
    print(f"PASS reproducibility: 3 identical runs (sha256 {check_reproducible(rows)[:16]}...), "
          f"see {config.REPRO_LOG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
