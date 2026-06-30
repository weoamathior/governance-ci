"""Unit tests for audit_record.py — the merge-time forensic record builder.

build_record reads the PR's canonical state (the sticky comment) via governance_state's
helpers, which are monkeypatched here so no network is touched.
"""
import audit_record as ar
import governance_state as gs


def _sticky_for(findings):
    body = gs.render({"headSha": "abc", "findings": findings})
    return {"id": 1, "body": body}


def test_record_from_sticky_captures_final_findings(monkeypatch):
    findings = [{"standardId": "STD-001", "severity": "required", "disposition": "dismissed",
                 "file": "src/foo.py", "lineRange": {"start": 1, "end": 2},
                 "description": "d", "stableKey": "k", "dismissalReason": "ok"}]
    monkeypatch.setattr(ar.gs, "find_sticky", lambda pr: _sticky_for(findings))

    rec = ar.build_record("16", "deadbeef", "2026-06-15T00:00:00Z")
    assert rec["prNumber"] == 16  # coerced to int
    assert rec["mergeCommitSha"] == "deadbeef"
    assert rec["mergedAt"] == "2026-06-15T00:00:00Z"
    assert rec["evaluated"] is True
    assert rec["findings"] == findings
    assert "recordedAt" in rec


def test_record_recovers_auto_approve_rule(monkeypatch):
    monkeypatch.setattr(ar.gs, "find_sticky", lambda pr: None)
    monkeypatch.setattr(ar.gs, "list_comments", lambda pr: [
        {"body": "some chatter"},
        {"body": "✅ Auto-approved by **RULE-002** (no LLM evaluation)."},
    ])
    rec = ar.build_record("9", "sha9", "")
    assert rec["evaluated"] is False
    assert rec["autoApproved"] is True
    assert rec["autoApproveRule"] == "RULE-002"
    assert "RULE-002" in rec["note"]
    assert rec["findings"] == []


def test_record_when_unevaluated_and_not_auto_approved(monkeypatch):
    monkeypatch.setattr(ar.gs, "find_sticky", lambda pr: None)
    monkeypatch.setattr(ar.gs, "list_comments", lambda pr: [{"body": "no governance here"}])
    rec = ar.build_record("3", "sha3", "")
    assert rec["evaluated"] is False
    assert rec["autoApproved"] is False
    assert rec["autoApproveRule"] is None
    assert "No governance evaluation" in rec["note"]


def test_auto_approve_regex_matches_rule_id_only():
    assert ar.AUTO_APPROVE_RE.search("Auto-approved by **RULE-001**").group(1) == "RULE-001"
    assert ar.AUTO_APPROVE_RE.search("Auto-approved by **RULE-12**") is None  # needs 3 digits
    assert ar.AUTO_APPROVE_RE.search("approved by RULE-001") is None  # needs the bold prefix
