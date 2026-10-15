# Shipping Cost Watcher

Spots routes whose freight cost per tonne-km is rising for no stated reason, and explains every verdict in plain English, citing the context note behind it. It is pure Python 3.10+ standard library: no packages, no network, no API key for the default run.

## Run it

Requires Python 3.10 or newer; nothing to install. Open a terminal in the project folder (if `python` is not recognised on Windows, use `py` instead):

```powershell
cd path\to\Freight_tigers
```

**1. Build the report**

```powershell
python build_assistant.py
```

Writes `output/shipment_cost_alerts.csv` (the submission) and `logs/run_cost_log.txt`. Expected output: `Wrote 728 route-weeks (16 flagged, 4 justified) ...`. Use `--output path\to\file.csv` to write the CSV somewhere else.

**2. Verify it**

```powershell
python verify_output.py
```

Prints 8 `PASS` lines (contract, recomputation, labeled notes, labeled cases, citations, LLM guardrails, Q&A, reproducibility) and writes `logs/reproducibility_check.txt`. It stops at the first failed check.

**3. Ask questions**

```powershell
python ask_assistant.py
```

Name a route and optionally a month/year; type `quit` to exit. Examples:

- `Why did Chennai-Bangalore get pricier in March 2025?`
- `Why was Mumbai-Pune flagged in September 2025?`
- `Which notes mention flooding?`

Or ask one question directly: `python ask_assistant.py "Why did Ahmedabad-Mumbai get pricier in January 2025?"`

**Optional: LLM mode** (open-source model through any OpenAI-compatible endpoint; not needed for the submitted output):

```powershell
ollama pull llama3.1:8b                    # local, free; default endpoint http://localhost:11434/v1
python build_assistant.py --llm
# or Groq free tier:
$env:LLM_BASE_URL="https://api.groq.com/openai/v1"; $env:LLM_MODEL="llama-3.1-8b-instant"; $env:LLM_API_KEY="..."
$env:LLM_PRICE_IN_PER_M="0.05"; $env:LLM_PRICE_OUT_PER_M="0.08"   # published USD rates, for the cost log
python build_assistant.py --llm
```

After an `--llm` run, run `python build_assistant.py` once more so the submitted CSV and cost log come from the default run.

## Layout

| File | Role |
|---|---|
| `watcher/config.py` | Paths, thresholds, output columns: every tunable choice in one place |
| `watcher/costs.py` | Weekly rate per route, own-history and peer baselines, alert rule |
| `watcher/notes.py` | Reads each note into facts (scope, cost effect, window, magnitude) and decides if a note justifies a rise |
| `watcher/retrieval.py` | TF-IDF vector index over notes with a route/date pre-filter (the RAG step) |
| `watcher/explain.py` | Reason text; optional LLM note check and rewording, with guardrails |
| `watcher/llm.py` | Minimal OpenAI-compatible client: temperature 0, disk cache, token accounting |
| `build_assistant.py` | CLI that builds the CSV and cost log |
| `ask_assistant.py` | Plain-English Q&A over the findings and notes (stretch goal) |
| `verify_output.py` | Evaluation harness |
| `data/labeled_cases.csv`, `data/labeled_notes.csv` | Hand-labeled answers the harness scores against |

## How it works

**1. Weekly cost.** Route = `origin-destination`; its `route_type` comes from the data, and the code checks it is consistent. Weeks run Monday to Sunday and `week_of` is the Monday. Rate = `sum(freight_cost_inr) / sum(quantity_tonnes x distance_km)` over the route-week's shipments. That ratio of sums weighs each shipment by the work done.

**2. Baselines (exactly as the brief defines them).**
- `vs_own_history`: the mean of the route's weekly rates over the 8 calendar weeks strictly before this week. When fewer than 8 prior weeks exist, every available prior week is used, with no padding or extrapolation. This only affects each route's first 8 weeks (Jan-Feb 2024). The very first week has no history and shows `N/A - no prior weeks for this route`. The data has no gaps (all 7 routes x 104 weeks), so "8 calendar weeks" and "last 8 weeks with data" are the same thing here.
- `vs_similar_routes`: the mean rate that week of the other routes with the same `route_type`, excluding the route itself.

**3. Alert rule (our choice; the brief sets no cutoff).** A route-week is an alert when its cost is **rising** (above its own trailing average) and either:
- it is at least **10% above its own history**, or
- it is at least **20% above similar routes**.

Why the "rising" requirement and the wider peer band: the Short group has only two routes. Mumbai-Pune ran 10-16% above Delhi-Jaipur all through 2024 while its own cost was flat. With a plain 10%-vs-either rule, 67 of 90 alerts were Mumbai-Pune weeks less than 10% above its own history, mostly that structural gap. The peer test still catches what the own-history average hides: Mumbai-Pune's slow 2025 creep, from about INR 3.4 to 4.5 per tonne-km, keeps it only 5-9% above a rolling average that drifts up with it, but takes it 20-39% above its peer. The thresholds live in `watcher/config.py`.

**4. Retrieval (RAG).** For each alert, notes are first filtered to those whose scope is this route or "All Routes" and whose validity window overlaps the week. A note for another route or another period can never be retrieved. The survivors are ranked by cosine similarity in a TF-IDF vector space against a query built from the route and cost-increase vocabulary. With only 10 notes the metadata filter does most of the work. The ranking decides which note is cited when several qualify, which nearby notes are quoted for unexplained alerts, and it powers note search in Q&A. It is in-process and deterministic, so it uses no tokens.

**5. Does a note really justify the rise?** Each note is parsed from its own text, with nothing hard-coded per note ID:
- *Cost effect*: phrases tying the event to higher costs ("higher trip costs", "surcharge", "pushing up transportation costs") count. Phrases saying costs were unaffected or normal ("not significantly affected", "without a rate change", "remained stable", "returned to normal", "not part of this dataset") override them.
- *Window*: a stated range ("from Feb 24 to Mar 8") is used as written. "Starting this week" marks a lasting change, which counts until the new level fills the 8-week baseline. Anything else covers the Monday-Sunday week of the note date.
- *Magnitude*: a stated percentage ("roughly 5-7%") caps how much of the rise the note can explain.

A note justifies an alert only if it raises costs, covers this route and week, explains enough of the rise (rise minus the stated maximum effect drops below the threshold), and, for an All Routes note, the alert is not about being above peers, because the peers faced the same change. If any check fails, the row stays `Yes`. A wrong "justified" scores worse than "couldn't find one".

The results on this data:

| Route, week | Verdict | Why |
|---|---|---|
| Ahmedabad-Mumbai 2025-01-20 | justified, N002 | festival surcharge, that corridor, that week |
| Chennai-Bangalore 2025-02-24, 03-03 | justified, N001 | flood Feb 24 - Mar 8 |
| Chennai-Bangalore 2025-03-10, 03-17 | flagged | roads reopened Mar 9; N009 says "returned to normal", which is not a cost reason |
| Mumbai-Delhi 2025-05-12 | justified, N003 | +11.6% vs history; the diesel rise of up to 7% leaves under 10% |
| Delhi-Jaipur 2024-11-11, 11-18 | flagged | +35% / +28% spike, no note at all |
| Mumbai-Pune, 12 weeks Jun-Dec 2025 | flagged | creep vs peer; N006 ("demand stable") cannot justify it |

**6. Output.** `output/shipment_cost_alerts.csv` follows `sample_output_format_v2.csv` column for column. `flagged` is `Yes`, `No (justified)` or `No`. `matched_note_id` is set only on `No (justified)` rows. Every justified reason quotes the note's ID, date, scope, full text and the window that covers the week. Flagged reasons quote any nearby route-relevant note and say why it does not count.

## Where the AI is, and how it is kept honest

- **Retrieval** (always on): the TF-IDF vector index above.
- **LLM, optional (`--llm`)**, two narrow jobs:
  1. *Second reading of each note* (10 calls): does this note say costs went up? If the model disagrees with the rule reading, the note is marked disputed and **cannot justify anything**. The LLM can make the output more cautious, never less.
  2. *Rewording alert reasons* (20 calls, alert rows only): the model gets the draft reason as its only facts. A guardrail rejects any rewording that adds a note ID or a number or date that is not in the draft, drops the cited note's ID or date, or contradicts the verdict. A rejected rewording falls back to the template, and the cost log counts rejections.

Verdicts, matched notes and all numbers are always computed by code, so the LLM cannot move a flag except by vetoing a note. Calls use `temperature 0` and `seed 0`, and every response is cached in `cache/llm_cache.json` by a hash of (model, prompt). A rerun replays the cache and is byte-identical; the harness tests this. The submitted CSV comes from the deterministic run.

## How I know it is right (`verify_output.py`)

1. **Contract:** exact column names and order, unique route-weeks, Monday dates, note ID present if and only if the row is justified.
2. **Independent recomputation:** separate code recomputes all 728 rates, both baselines, the formatted percentages and every alert flag from raw shipments.
3. **Labeled notes (10/10):** expected cost effect and window for every note.
4. **Labeled cases (15):** hand-labeled route-weeks covering each justification, the flood window edge, wrong-route and non-causal notes, a non-alert that must not cite N003, structural peer gap, first week.
5. **Citation guardrail:** every cited note raises costs, covers the route and the week, and is quoted with its ID, date and text.
6. **LLM guardrails, offline:** a fake endpoint checks that faithful rewordings are kept, a rewording inventing "N099" and "50%" is rejected on all 20 rows, an LLM veto of N002 turns that row into `Yes`, and a cached rerun makes 0 live calls with identical output.
7. **Q&A:** answers cite the right notes; unknown routes get a clarifying reply.
8. **Reproducibility:** runs the CLI 3 times into fresh files, diffs them and compares SHA-256 hashes. The result is written to `logs/reproducibility_check.txt`.

## Cost of running it

| Mode | LLM calls | Input tokens | Output tokens | Cost |
|---|---|---|---|---|
| Default (submitted output) | 0 | 0 | 0 | USD 0 |
| `--llm`, one full run, estimated | 30 (10 note checks + 20 rewordings) | ~5,200 | ~1,500-2,100 | ~USD 0.0004 on Groq Llama-3.1-8B (USD 0.05 / 0.08 per 1M); USD 0 on local Ollama |

`logs/run_cost_log.txt` records the actual counts for whichever mode last ran. The `--llm` figures above are estimates from the exact prompts at ~3.6 characters per token; this submission was not run against a live model. A cached rerun still reports full-run tokens, plus how many calls were live versus replayed.

## Limits and trade-offs

- The 10% and 20% thresholds and the "rising" requirement are judgment calls. Lowering the peer band to 15% adds 7 alerts (27 instead of 20), including 3 flat-cost Mumbai-Pune weeks from 2024.
- The note parser is phrase-based and tuned to this note style. A new note phrased differently might be read as non-causal. That fails safe (flagged, not falsely justified), and the optional LLM second reading is there to catch it.
- Treating "starting this week" as lasting 8 weeks is an interpretation. It only changes one row (Mumbai-Delhi 2025-05-12).
- Q&A is retrieval over the computed findings, not free-form chat, so it cannot invent a cause.

See [WALKTHROUGH.md](WALKTHROUGH.md) for the 10-minute demo script.
