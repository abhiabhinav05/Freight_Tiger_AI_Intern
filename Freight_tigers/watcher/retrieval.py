"""Small in-memory vector index over the context notes (TF-IDF + cosine similarity).

Retrieval is hybrid: a metadata pre-filter (route scope and date window) guarantees that a
note for another route or another period can never be retrieved; the vector score then
ranks the survivors by how strongly they talk about this route and about cost changes.
No external service or package is needed, and scores are deterministic.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, timedelta

from .notes import Note

TOKEN_RE = re.compile(r"[a-z0-9]+")
COST_QUERY = "higher freight trip costs surcharge prices rose rates increased disruption detours"


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


class NoteIndex:
    def __init__(self, notes: list[Note]):
        self.notes = notes
        docs = [tokens(f"{n.applies_to} {n.text}") for n in notes]
        df = Counter(t for doc in docs for t in set(doc))
        self.idf = {t: math.log((1 + len(docs)) / (1 + c)) + 1 for t, c in df.items()}
        self.vectors = [self._vector(doc) for doc in docs]

    def _vector(self, toks: list[str]) -> dict[str, float]:
        tf = Counter(t for t in toks if t in self.idf)
        vec = {t: c * self.idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def search(self, query: str, route: str | None = None, week: date | None = None,
               slack_days: int = 0) -> list[tuple[float, Note]]:
        """Rank notes by cosine similarity, after filtering on route scope and date window."""
        q = self._vector(tokens(query))
        hits = []
        for note, vec in zip(self.notes, self.vectors):
            if route and not note.applies_to_route(route):
                continue
            if week and not (note.start - timedelta(days=slack_days) <= week + timedelta(days=6)
                             and note.end + timedelta(days=slack_days) >= week):
                continue
            score = sum(w * vec.get(t, 0.0) for t, w in q.items())
            hits.append((round(score, 9), note))
        return sorted(hits, key=lambda h: (-h[0], h[1].note_id))

    def for_route_week(self, route: str, week: date, slack_days: int = 0) -> list[Note]:
        query = f"{route.replace('-', ' ')} {COST_QUERY}"
        return [note for _, note in self.search(query, route, week, slack_days)]

    def nearby(self, route: str, week: date, slack_days: int) -> list[Note]:
        """Route-relevant notes whose window lies within `slack_days` of the week, closest first."""
        def gap(note: Note) -> int:
            if note.overlaps(week):
                return 0
            if note.end < week:
                return (week - note.end).days
            return (note.start - (week + timedelta(days=6))).days
        query = f"{route.replace('-', ' ')} {COST_QUERY}"
        hits = self.search(query, route, week, slack_days)
        return [note for score, note in sorted(hits, key=lambda h: (gap(h[1]), -h[0], h[1].note_id))]
