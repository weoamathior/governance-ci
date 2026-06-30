"""Unit tests for governance_state.py — the gate logic and sticky-comment state.

This is the highest-risk module: it decides whether the merge gate is green and who/what
flips it. The gh-API helpers are monkeypatched out; everything tested here is the pure
decision logic plus the render/parse round-trip that carries dismissals across pushes.
"""
import json
import types

import governance_state as gs


def _finding(std="STD-001", sev="required", disposition="open", file="src/foo.py",
             stable_key="k1", **extra):
    f = {
        "standardId": std,
        "severity": sev,
        "disposition": disposition,
        "file": file,
        "lineRange": {"start": 10, "end": 12},
        "description": "desc",
        "stableKey": stable_key,
        "dismissalReason": None,
    }
    f.update(extra)
    return f


def _dismissed(std="STD-001", reason="had a reason", by="alice", **kw):
    return _finding(
        std=std, disposition="dismissed",
        dismissalReason=reason,
        dismissal={"dismissedBy": by, "dismisserRole": "owner", "reason": reason,
                   "timestamp": "2026-01-01T00:00:00+00:00", "atCommitSha": "abc",
                   "standardsRef": "v1"},
        **kw)


# --- compute_gate --------------------------------------------------------------

def test_gate_green_when_no_findings():
    state, desc = gs.compute_gate([])
    assert state == "success"


def test_gate_red_on_open_required():
    state, desc = gs.compute_gate([_finding(sev="required")])
    assert state == "failure"
    assert "1 open required" in desc


def test_gate_red_on_open_escalate():
    state, _ = gs.compute_gate([_finding(sev="escalate")])
    assert state == "failure"


def test_gate_green_on_open_advisory_only():
    state, desc = gs.compute_gate([_finding(sev="advisory")])
    assert state == "success"
    assert "no open blocking" in desc


def test_gate_green_when_blocking_finding_is_dismissed():
    state, _ = gs.compute_gate([_dismissed(sev="required")])
    assert state == "success"


def test_gate_counts_only_open_blocking():
    findings = [_finding(sev="required"), _dismissed(sev="required", stable_key="k2")]
    state, desc = gs.compute_gate(findings)
    assert state == "failure"
    assert "1 open required" in desc


def test_gate_description_lists_sorted_severities():
    findings = [_finding(sev="required", stable_key="a"),
                _finding(sev="escalate", stable_key="b")]
    state, desc = gs.compute_gate(findings)
    assert state == "failure"
    assert "escalate/required" in desc  # sorted set join
    assert "2 open" in desc


# --- md_cell -------------------------------------------------------------------

def test_md_cell_escapes_pipes_and_flattens_newlines():
    assert gs.md_cell("a|b") == "a\\|b"
    assert gs.md_cell("line1\nline2\r\n") == "line1 line2  "


# --- render / parse_data round trip -------------------------------------------

def test_render_parse_round_trip_preserves_state():
    state = {"headSha": "deadbeef",
             "findings": [_finding(), _dismissed(std="STD-002", stable_key="k2")]}
    body = gs.render(state)
    assert gs.MARKER in body
    assert gs.parse_data(body) == state


def test_render_header_counts_open_and_dismissed():
    state = {"headSha": "s", "findings": [_finding(), _dismissed(stable_key="k2")]}
    body = gs.render(state)
    assert "1 open / 1 dismissed" in body


def test_render_no_findings_is_clean_and_green():
    body = gs.render({"headSha": "s", "findings": []})
    assert "No findings" in body
    assert gs.parse_data(body) == {"headSha": "s", "findings": []}


def test_parse_data_survives_arrow_sequence_in_reason():
    # A dismissal reason containing the literal data-close terminator must not truncate
    # the hidden block — that is the whole reason it is base64-encoded.
    state = {"headSha": "s", "findings": [_dismissed(reason="see ticket --> done")]}
    body = gs.render(state)
    assert gs.parse_data(body) == state


def test_parse_data_returns_none_without_block():
    assert gs.parse_data("just a normal comment, no data block") is None


def test_parse_data_returns_none_on_malformed_block():
    bad = f"{gs.DATA_OPEN}\nnot-valid-base64-!!!\n{gs.DATA_CLOSE}"
    assert gs.parse_data(bad) is None


# --- cmd_publish carry-forward -------------------------------------------------

def test_publish_carries_forward_dismissal_by_stable_key(tmp_path, monkeypatch):
    prior_state = {"headSha": "old", "findings": [_dismissed(stable_key="K", reason="carry me")]}
    monkeypatch.setattr(gs, "find_sticky", lambda pr: {"id": 1, "body": gs.render(prior_state)})

    captured = {}
    monkeypatch.setattr(gs, "upsert_sticky", lambda pr, st: captured.update(state=st))
    monkeypatch.setattr(gs, "set_gate", lambda sha, f, url=None: ("success", "ok"))

    new = [_finding(stable_key="K", disposition="open"),       # should inherit dismissal
           _finding(std="STD-002", stable_key="Z", disposition="open")]  # stays open
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(new))

    args = types.SimpleNamespace(pr="7", sha="newsha", findings=str(findings_path), target_url=None)
    gs.cmd_publish(args)

    out = {f["stableKey"]: f for f in captured["state"]["findings"]}
    assert out["K"]["disposition"] == "dismissed"
    assert out["K"]["dismissalReason"] == "carry me"
    assert out["K"]["dismissal"]["dismissedBy"] == "alice"
    assert out["Z"]["disposition"] == "open"
    assert captured["state"]["headSha"] == "newsha"


def test_publish_no_prior_sticky_leaves_findings_open(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "find_sticky", lambda pr: None)
    captured = {}
    monkeypatch.setattr(gs, "upsert_sticky", lambda pr, st: captured.update(state=st))
    monkeypatch.setattr(gs, "set_gate", lambda sha, f, url=None: ("failure", "blocked"))

    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps([_finding(stable_key="K")]))
    args = types.SimpleNamespace(pr="7", sha="s", findings=str(findings_path), target_url=None)
    gs.cmd_publish(args)

    assert captured["state"]["findings"][0]["disposition"] == "open"


# --- cmd_dismiss ---------------------------------------------------------------

def _dismiss_args(**kw):
    base = dict(pr="7", sha="headsha", standard="STD-001", file=None,
                reason="approved in follow-up", actor="alice", role="owner")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _wire_dismiss(monkeypatch, state):
    monkeypatch.setattr(gs, "find_sticky", lambda pr: {"id": 1, "body": gs.render(state)})
    cap = {"comments": []}
    monkeypatch.setattr(gs, "upsert_sticky", lambda pr, st: cap.update(state=st))
    monkeypatch.setattr(gs, "set_gate", lambda sha, f: cap.update(gate_sha=sha) or ("success", "ok"))
    monkeypatch.setattr(gs, "comment", lambda pr, body: cap["comments"].append(body))
    return cap


def test_dismiss_marks_matching_open_finding_with_provenance(monkeypatch):
    state = {"headSha": "old", "findings": [
        _finding(std="STD-001", stable_key="a"),
        _finding(std="STD-002", stable_key="b"),
    ]}
    cap = _wire_dismiss(monkeypatch, state)
    gs.cmd_dismiss(_dismiss_args())

    by_std = {f["standardId"]: f for f in cap["state"]["findings"]}
    assert by_std["STD-001"]["disposition"] == "dismissed"
    assert by_std["STD-001"]["dismissal"]["dismissedBy"] == "alice"
    assert by_std["STD-001"]["dismissalReason"] == "approved in follow-up"
    assert by_std["STD-002"]["disposition"] == "open"  # untouched
    # Gate is set on the CURRENT head SHA passed by the workflow, not the stored one.
    assert cap["gate_sha"] == "headsha"
    assert "Dismissed 1" in cap["comments"][0]


def test_dismiss_respects_file_filter(monkeypatch):
    state = {"headSha": "old", "findings": [_finding(std="STD-001", file="src/foo.py")]}
    cap = _wire_dismiss(monkeypatch, state)
    gs.cmd_dismiss(_dismiss_args(file="src/other.py"))  # different file -> no match

    assert state["findings"][0]["disposition"] == "open"
    assert "No open" in cap["comments"][0]
    assert "gate_sha" not in cap  # gate not touched when nothing matched


def test_dismiss_ignores_already_dismissed(monkeypatch):
    state = {"headSha": "old", "findings": [_dismissed(std="STD-001")]}
    cap = _wire_dismiss(monkeypatch, state)
    gs.cmd_dismiss(_dismiss_args())
    assert "No open" in cap["comments"][0]
