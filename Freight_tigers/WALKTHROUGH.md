# Round 2 walkthrough (10 minutes)

## 0:00-1:00 - What it does

"It groups shipments by route and Monday-start week, computes cost per tonne-km, compares each week with the route's trailing 8-week average and with same-type peers, and flags rises that look out of the ordinary. Before flagging, it retrieves the context notes and checks whether one really justifies the rise. Every verdict comes with a plain-English reason that quotes the note."

## 1:00-2:30 - Run it live

```powershell
python build_assistant.py
python verify_output.py
```

Show: 728 route-weeks, 16 flagged, 4 justified. All eight PASS lines. `logs/run_cost_log.txt` (0 LLM calls, 0 tokens) and `logs/reproducibility_check.txt` (3 identical hashes).

## 2:30-4:00 - The numbers

- Rate = sum of cost / sum of tonnes x km for the route-week.
- Own history = mean of the 8 prior weeks, strictly before this week. Each route's first 8 weeks use whatever history exists, and the first week shows N/A.
- Peers = same route_type, same week, the route itself excluded.
- Alert = rising, and at least 10% above own history or at least 20% above peers.

Point at the trade-off: "A naive 10%-either rule gave 90 alerts. 67 were Mumbai-Pune simply being a pricier Short route than Delhi-Jaipur, with flat costs. Requiring 'rising' and a wider peer band cuts that noise. It still catches the real story: Mumbai-Pune creeping from 3.4 to 4.5 INR/t-km through 2025. The rolling own-average hides that creep because it drifts up too, but the peer comparison shows it."

## 4:00-6:30 - Verdicts that hold up on a closer read

Open the CSV and walk four rows:

1. **Ahmedabad-Mumbai 2025-01-20, justified by N002.** The festival surcharge note names this corridor and this week.
2. **Chennai-Bangalore 2025-03-03 justified, 03-10 flagged.** N001 says the flood ran Feb 24 to Mar 8 and roads reopened Mar 9. The parser reads that range from the text. The week of Mar 10 is out of window, so it is flagged, and the reason says exactly that. N009 ("returned to normal") is quoted but cannot justify a rise.
3. **Mumbai-Delhi 2025-05-12, justified by N003.** The diesel rise started that week and is lasting. The note says 5-7%; the rise is +11.6%, and 11.6 - 7 leaves less than 10%, so it is explained. Contrast: an All Routes note can never explain being above peers, since the peers paid the same diesel price. That is why Chennai-Bangalore 2025-05-05 is not justified by N003 (the old version got this wrong).
4. **Delhi-Jaipur 2024-11-11, flagged.** +35%, no note at all. "Couldn't find one" beats a made-up reason.

## 6:30-7:30 - Where the AI is

- Retrieval: a TF-IDF vector index with a route and date pre-filter, so a wrong-route or wrong-period note can't be retrieved.
- Optional `--llm` (Ollama or Groq, open-source Llama), with two narrow jobs:
  - Second-read each note. A disagreement vetoes that note, which only ever adds caution.
  - Reword alert reasons. The guardrail rejects any new note ID, number or date, or a changed verdict.
- Temperature 0 and a prompt-hash cache, so reruns are identical.
- About 30 calls and ~5k input tokens per run, roughly USD 0.0004 on Groq; free locally.

## 7:30-8:30 - Ask it

```powershell
python ask_assistant.py "Why did Chennai-Bangalore get pricier in March 2025?"
python ask_assistant.py "Why was Mumbai-Pune flagged in September 2025?"
python ask_assistant.py "Which notes mention flooding?"
```

## 8:30-10:00 - How I trusted it, and what I'd change

- The harness recomputes every number with separate code. It scores 10 labeled notes and 15 labeled route-weeks, checks every citation against route and date, and tests the LLM guardrails offline with a fake endpoint that tries to hallucinate "N099" and "50%".
- Limits:
  - The thresholds are judgment calls, and all live in `config.py`.
  - The phrase-based note reader fails safe on unfamiliar wording; the LLM second reading is the backstop.
  - Treating "starting this week" as lasting is an interpretation, and it changes only one row.
