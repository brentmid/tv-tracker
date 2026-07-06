"""Name matching for the importer's fallback paths.

Primary show resolution is ID-based (TVmaze lookup by TheTVDB id) and never
comes through here. This module handles the leftovers: shows TVmaze can't
resolve by id, and all movies (the export carries no movie ids).

Thresholds (from the approved plan): similarity >= 0.92 auto-matches,
0.75-0.92 lands in staging as ambiguous, below is unmatched.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

AUTO_THRESHOLD = 0.92
AMBIGUOUS_THRESHOLD = 0.75


def normalize(name: str) -> str:
    """Fold case/accents/punctuation so cosmetic differences don't count.

    "Marvel's Agents of S.H.I.E.L.D." ~ "marvels agents of shield"
    """
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.casefold().replace("&", " and ")
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return " ".join(name.split())


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def classify(score: float) -> str:
    """'matched' | 'ambiguous' | 'unmatched' for a similarity score."""
    if score >= AUTO_THRESHOLD:
        return "matched"
    if score >= AMBIGUOUS_THRESHOLD:
        return "ambiguous"
    return "unmatched"


def best_match(name: str, candidates: list[tuple[object, str]]):
    """Pick the most similar candidate.

    candidates: (key, candidate_name) pairs — key is whatever the caller
    wants back (a TVmaze show dict, a TMDB id, ...).
    Returns (key, score, status); (None, 0.0, 'unmatched') if no candidates.

    A second candidate scoring within 0.05 of an auto-match demotes the
    result to ambiguous — two near-identical titles (remakes, reboots)
    should get a human decision, not a coin flip.
    """
    scored = sorted(
        ((key, similarity(name, cand)) for key, cand in candidates),
        key=lambda pair: -pair[1],
    )
    if not scored:
        return None, 0.0, "unmatched"
    key, score = scored[0]
    status = classify(score)
    if (status == "matched" and len(scored) > 1
            and scored[1][1] >= score - 0.05):
        status = "ambiguous"
    return key, score, status
