"""Tests for tvtracker/matching.py."""

from tvtracker import matching


def test_normalize():
    assert matching.normalize("Marvel's Agents of S.H.I.E.L.D.") == \
        "marvel s agents of s h i e l d"
    assert matching.normalize("Law & Order") == "law and order"
    assert matching.normalize("  Amélie  ") == "amelie"
    assert matching.normalize("The 100") == "the 100"


def test_similarity_and_classify():
    assert matching.similarity("Severance", "severance") == 1.0
    assert matching.similarity("Severance", "") == 0.0
    assert matching.classify(0.95) == "matched"
    assert matching.classify(0.92) == "matched"
    assert matching.classify(0.85) == "ambiguous"
    assert matching.classify(0.5) == "unmatched"


def test_best_match_picks_highest():
    key, score, status = matching.best_match(
        "Game of Thrones",
        [(1, "Game of Thrones"), (2, "Game of Thrones: Conquest & Rebellion")])
    assert key == 1
    assert score == 1.0
    assert status == "matched"


def test_best_match_near_tie_demotes_to_ambiguous():
    key, score, status = matching.best_match(
        "Twin Detectives", [(1, "Twin Detectives"), (2, "Twin Detectives!")])
    assert status == "ambiguous"  # two near-identical titles need a human


def test_best_match_empty_candidates():
    assert matching.best_match("Anything", []) == (None, 0.0, "unmatched")
