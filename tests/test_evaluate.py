"""Unit tests for evaluate.py — the deterministic, non-LLM helpers.

The model call in main() is intentionally NOT tested here (it is exercised end-to-end
by the evals regression harness). What matters for correctness of dismissal carry-forward
is the diff parsing and the content-addressed stableKey, which are pure functions.
"""
import hashlib

import evaluate as ev


# --- added_lines_by_file -------------------------------------------------------

def test_added_lines_maps_file_to_added_text_only():
    diff = "\n".join([
        "diff --git a/src/foo.py b/src/foo.py",
        "--- a/src/foo.py",
        "+++ b/src/foo.py",
        "@@ -1,2 +1,3 @@",
        " context line",
        "+added one",
        "+added two",
        "-removed line",
    ])
    added = ev.added_lines_by_file(diff)
    assert added == {"src/foo.py": "added one\nadded two"}


def test_added_lines_strips_b_prefix_and_handles_new_file():
    diff = "\n".join([
        "--- /dev/null",
        "+++ b/src/new.py",
        "@@ -0,0 +1 @@",
        "+brand new",
    ])
    added = ev.added_lines_by_file(diff)
    assert added == {"src/new.py": "brand new"}


def test_added_lines_ignores_deleted_file_target():
    # A deletion has `+++ /dev/null`: cur becomes None, so nothing is recorded for it.
    diff = "\n".join([
        "--- a/old.py",
        "+++ /dev/null",
        "@@ -1 +0,0 @@",
        "-gone",
    ])
    assert ev.added_lines_by_file(diff) == {}


def test_added_lines_does_not_treat_header_plus_lines_as_additions():
    # The `+++ b/...` header starts with '+' but must not be counted as an added line.
    diff = "--- a/x\n+++ b/x\n@@\n+real"
    assert ev.added_lines_by_file(diff) == {"x": "real"}


def test_added_lines_empty_diff():
    assert ev.added_lines_by_file("") == {}


# --- stable_key ----------------------------------------------------------------

def _expected_key(std, file, snippet):
    h = hashlib.sha256()
    h.update((std + "\0" + (file or "") + "\0" + snippet).encode("utf-8"))
    return h.hexdigest()


def test_stable_key_is_content_addressed_and_deterministic():
    added = {"src/foo.py": "added one\nadded two"}
    k1 = ev.stable_key("STD-001", "src/foo.py", added)
    k2 = ev.stable_key("STD-001", "src/foo.py", added)
    assert k1 == k2 == _expected_key("STD-001", "src/foo.py", "added one\nadded two")


def test_stable_key_changes_when_added_content_changes():
    before = ev.stable_key("STD-001", "src/foo.py", {"src/foo.py": "v1"})
    after = ev.stable_key("STD-001", "src/foo.py", {"src/foo.py": "v2"})
    assert before != after  # this is what makes a dismissal stop carrying when code changes


def test_stable_key_same_standard_and_file_collapse():
    # Two findings of the same standard in the same file intentionally share a key.
    added = {"a.py": "x"}
    assert ev.stable_key("STD-001", "a.py", added) == ev.stable_key("STD-001", "a.py", added)


def test_stable_key_differs_by_standard_and_file():
    added = {"a.py": "x", "b.py": "x"}
    assert ev.stable_key("STD-001", "a.py", added) != ev.stable_key("STD-002", "a.py", added)
    assert ev.stable_key("STD-001", "a.py", added) != ev.stable_key("STD-001", "b.py", added)


def test_stable_key_handles_none_file():
    k = ev.stable_key("STD-001", None, {})
    assert k == _expected_key("STD-001", "", "")


# --- detection_schema ----------------------------------------------------------

def test_detection_schema_constrains_severity_and_shape():
    s = ev.detection_schema()
    assert s["additionalProperties"] is False
    assert s["properties"]["severity"]["enum"] == ev.SEVERITIES
    assert set(s["required"]) == {"standardId", "severity", "description", "file", "lineRange"}
