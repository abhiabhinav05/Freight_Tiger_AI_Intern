"""Paths, thresholds, and output contract. Every tunable decision lives here."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIPMENTS = ROOT / "data" / "shipment_records.csv"
NOTES = ROOT / "data" / "context_notes.csv"
TEMPLATE = ROOT / "data" / "sample_output_format_v2.csv"
LABELED_CASES = ROOT / "data" / "labeled_cases.csv"
LABELED_NOTES = ROOT / "data" / "labeled_notes.csv"
OUTPUT = ROOT / "output" / "shipment_cost_alerts.csv"
COST_LOG = ROOT / "logs" / "run_cost_log.txt"
REPRO_LOG = ROOT / "logs" / "reproducibility_check.txt"
LLM_CACHE = ROOT / "cache" / "llm_cache.json"

OUTPUT_COLUMNS = [
    "route", "week_of", "cost_per_tonne_km", "vs_own_history",
    "vs_similar_routes", "flagged", "matched_note_id", "reason",
]

# Baseline definitions fixed by the brief.
HISTORY_WEEKS = 8

# Alert rule (our choice; the brief gives no cutoff):
#   flag when the cost is rising (above its own trailing average) AND it is
#   >= OWN_THRESHOLD above that average, or >= PEER_THRESHOLD above same-type peers.
# The peer band is wider because Short has only two routes and Mumbai-Pune ran
# 10-16% above Delhi-Jaipur all through 2024 with flat costs - a structural gap.
OWN_THRESHOLD = 0.10
PEER_THRESHOLD = 0.20

# A note's window must fall within this many days of a week to be retrieved
# as nearby context for an unexplained alert.
NEARBY_DAYS = 14
