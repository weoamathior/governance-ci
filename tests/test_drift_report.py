"""Unit tests for drift_report.py — the pure classify/summarize/render logic.

The gh-querying (gather/product_repos/effective_ref) is integration and not unit-tested.
"""
import drift_report as dr


def test_classify_default_inherited():
    rows = dr.classify("std-2026.06", [{"name": "a", "ref": "std-2026.06", "source": "org-default"}])
    assert rows[0]["status"] == "default"


def test_classify_redundant_override():
    # repo pins the SAME value as the default -> redundant, should be cleaned up
    rows = dr.classify("std-2026.06", [{"name": "a", "ref": "std-2026.06", "source": "repo-override"}])
    assert rows[0]["status"] == "redundant-override"


def test_classify_divergent_override():
    # repo pins a DIFFERENT value -> canary or laggard, flag it
    rows = dr.classify("std-2026.06", [{"name": "a", "ref": "std-2025.11", "source": "repo-override"}])
    assert rows[0]["status"] == "divergent"


def test_summarize_counts():
    rows = dr.classify("D", [
        {"name": "a", "ref": "D", "source": "org-default"},
        {"name": "b", "ref": "D", "source": "org-default"},
        {"name": "c", "ref": "D", "source": "repo-override"},     # redundant
        {"name": "d", "ref": "X", "source": "repo-override"},     # divergent
    ])
    assert dr.summarize(rows) == {"default": 2, "redundant-override": 1, "divergent": 1}


def test_render_clean_when_all_default():
    rows = dr.classify("D", [{"name": "a", "ref": "D", "source": "org-default"}])
    report = dr.render("D", "GOVERNANCE_STANDARDS_REF", rows)
    assert "No drift" in report
    assert "Divergent" not in report


def test_render_lists_divergent_and_redundant_sections():
    rows = dr.classify("D", [
        {"name": "laggard", "ref": "old", "source": "repo-override"},
        {"name": "tidy", "ref": "D", "source": "repo-override"},
    ])
    report = dr.render("D", "GOVERNANCE_STANDARDS_REF", rows)
    assert "Divergent" in report and "laggard" in report and "`old`" in report
    assert "Redundant overrides" in report and "tidy" in report
    assert "Org default:** `D`" in report
