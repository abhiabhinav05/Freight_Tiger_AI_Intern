"""Turn free-text context notes into checkable facts, and decide whether a note justifies a rise.

Each note is read into: scope (route or All Routes), effect (does it say costs went UP?),
validity window, and stated magnitude. A justification must pass every check; anything
less leaves the row flagged, because a wrong "justified" is worse than "couldn't find one".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from .config import HISTORY_WEEKS, OWN_THRESHOLD, PEER_THRESHOLD
from .costs import RouteWeek, monday, read_csv

# Wording that says costs were NOT pushed up. Checked first: it overrides any cost word.
NO_INCREASE_CUES = (
    "not significantly affected", "without a rate change", "not part of this dataset",
    "remained stable", "no major disruptions", "no significant disruptions",
    "remained normal", "returned to normal", "improved road conditions",
)
# Wording that ties an event to higher freight costs.
INCREASE_CUES = (
    "higher trip costs", "surcharge", "pushing up transportation costs", "prices rose",
    "costs rose", "costs increased", "rates increased", "rate hike",
)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
RANGE_RE = re.compile(r"from (\w{3})\w* (\d{1,2}) to (\w{3})\w* (\d{1,2})", re.I)
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*%|(\d+(?:\.\d+)?)\s*%")
PERSISTENT_RE = re.compile(r"\bstarting (this week|from)\b", re.I)


@dataclass(frozen=True)
class Note:
    note_id: str
    date: date
    applies_to: str
    text: str
    raises_costs: bool
    start: date
    end: date
    max_pct: float | None       # upper bound of the stated cost effect, as a fraction
    window_basis: str           # how the window was derived, quoted in explanations
    disputed: bool = False      # set when the optional LLM reading disagrees on raises_costs

    @property
    def all_routes(self) -> bool:
        return self.applies_to.lower() == "all routes"

    def applies_to_route(self, route: str) -> bool:
        return self.all_routes or self.applies_to == route

    def overlaps(self, week: date) -> bool:
        return self.start <= week + timedelta(days=6) and self.end >= week


def parse_note(row: dict[str, str]) -> Note:
    text = row["note"].strip()
    lower = text.lower()
    noted = date.fromisoformat(row["date"].strip())
    raises = not any(c in lower for c in NO_INCREASE_CUES) and any(c in lower for c in INCREASE_CUES)

    if m := RANGE_RE.search(text):
        start = date(noted.year, MONTHS[m.group(1).lower()], int(m.group(2)))
        end = date(noted.year, MONTHS[m.group(3).lower()], int(m.group(4)))
        if end < start:
            end = date(noted.year + 1, end.month, end.day)
        basis = f"stated period {start:%b %d} to {end:%b %d}"
    elif PERSISTENT_RE.search(text):
        # A lasting change (e.g. a fuel price rise) keeps explaining the gap to the
        # trailing average until the new level fills the whole 8-week baseline.
        start = monday(noted)
        end = start + timedelta(weeks=HISTORY_WEEKS, days=-1)
        basis = f"lasting change from {start:%b %d}, counted until it is inside the {HISTORY_WEEKS}-week baseline ({end:%b %d})"
    else:
        start = monday(noted)
        end = start + timedelta(days=6)
        basis = f"the week of {start:%b %d}"

    max_pct = None
    if m := PCT_RE.search(text):
        max_pct = float(m.group(2) or m.group(3)) / 100

    return Note(row["note_id"].strip(), noted, row["applies_to"].strip(), text, raises,
                start, end, max_pct, basis)


def load_notes(path: Path) -> list[Note]:
    return [parse_note(row) for row in read_csv(path)]


def mark_disputed(notes: list[Note], disputed_ids: set[str]) -> list[Note]:
    return [replace(n, disputed=True) if n.note_id in disputed_ids else n for n in notes]


@dataclass(frozen=True)
class Verdict:
    note: Note | None               # the note that fully justifies the alert, if any
    partial: Note | None            # a relevant cost note that explains only part of it
    partial_reason: str = ""


def check_note(note: Note, rw: RouteWeek) -> tuple[bool, str]:
    """Can `note` fully explain this alert? Returns (ok, why-not)."""
    if not note.raises_costs or note.disputed:
        return False, "does not describe a cost increase"
    if not note.applies_to_route(rw.route):
        return False, f"is about {note.applies_to}, not this route"
    if not note.overlaps(rw.week):
        return False, f"covers {note.window_basis}, which does not include the week of {rw.week.isoformat()}"
    if note.all_routes and rw.peer_elevated:
        return False, ("applies to all routes, so it cannot explain why this route is above "
                       "similar routes that faced the same change")
    if note.max_pct is not None:
        if rw.own_elevated and rw.own_delta - note.max_pct >= OWN_THRESHOLD:
            return False, (f"reports at most a {note.max_pct:.0%} effect, which leaves most of the "
                           f"{rw.own_delta:+.1%} rise unexplained")
        if rw.peer_elevated and rw.peer_delta - note.max_pct >= PEER_THRESHOLD:
            return False, f"reports at most a {note.max_pct:.0%} effect, too small for the gap to peers"
    return True, ""


def judge(rw: RouteWeek, candidates: list[Note]) -> Verdict:
    """Pick the first retrieved note that passes every check; remember a near-miss for the reason text."""
    partial, partial_reason = None, ""
    for note in candidates:
        ok, why = check_note(note, rw)
        if ok:
            return Verdict(note, None)
        if partial is None and note.raises_costs and not note.disputed \
                and note.applies_to_route(rw.route) and note.overlaps(rw.week):
            partial, partial_reason = note, why
    return Verdict(None, partial, partial_reason)
