"""Unit tests for run_evals.py — the scoring/aggregation/regression logic.

These cover the math that decides whether a prompt/standards change passes the gate.
The subprocess that actually runs evaluate.py (run_once) is not tested here; it is
exercised for real by the live harness in CI.
"""
import run_evals as rv


# --- score ---------------------------------------------------------------------

def test_score_expect_passes_at_threshold():
    fx = {"expect": [{"standardId": "STD-001"}], "passThreshold": 2}
    results = [{"STD-001": "required"}, {"STD-001": "required"}, {}]
    ok, checks = rv.score(fx, results)
    assert ok is True
    assert checks[0]["count"] == 2 and checks[0]["passed"] is True


def test_score_expect_fails_below_threshold():
    fx = {"expect": [{"standardId": "STD-001"}], "passThreshold": 3}
    results = [{"STD-001": "required"}, {}, {}]
    ok, _ = rv.score(fx, results)
    assert ok is False


def test_score_expect_threshold_defaults_to_runs():
    fx = {"expect": [{"standardId": "STD-001"}]}  # no passThreshold -> must fire every run
    assert rv.score(fx, [{"STD-001": "x"}, {"STD-001": "x"}])[0] is True
    assert rv.score(fx, [{"STD-001": "x"}, {}])[0] is False


def test_score_expect_severity_must_match():
    fx = {"expect": [{"standardId": "STD-001", "severity": "required"}], "passThreshold": 2}
    fired_wrong_sev = [{"STD-001": "advisory"}, {"STD-001": "advisory"}]
    assert rv.score(fx, fired_wrong_sev)[0] is False
    fired_right_sev = [{"STD-001": "required"}, {"STD-001": "required"}]
    assert rv.score(fx, fired_right_sev)[0] is True


def test_score_forbid_within_tolerance():
    fx = {"expectNot": ["STD-002"], "tolerance": 1}
    assert rv.score(fx, [{"STD-002": "x"}, {}, {}])[0] is True   # 1 violation <= 1
    assert rv.score(fx, [{"STD-002": "x"}, {"STD-002": "x"}, {}])[0] is False  # 2 > 1


def test_score_forbid_tolerance_defaults_to_zero():
    fx = {"expectNot": ["STD-002"]}
    assert rv.score(fx, [{}, {}])[0] is True
    assert rv.score(fx, [{"STD-002": "x"}, {}])[0] is False


# --- aggregate -----------------------------------------------------------------

def test_aggregate_computes_recall_and_fp_rate():
    # Two should-flag fixtures (one passes, one fails) -> recall 0.5;
    # two should-not-flag checks (one clean, one violating) -> fpRate 0.5.
    fx_pass = {"expect": [{"standardId": "STD-001"}], "passThreshold": 1}
    fx_fail = {"expect": [{"standardId": "STD-001"}], "passThreshold": 1}
    scored = [
        (fx_pass, *rv.score(fx_pass, [{"STD-001": "required"}])),
        (fx_fail, *rv.score(fx_fail, [{}])),
        ({"expectNot": ["STD-002"]}, *rv.score({"expectNot": ["STD-002"]}, [{}])),
        ({"expectNot": ["STD-002"]}, *rv.score({"expectNot": ["STD-002"]}, [{"STD-002": "x"}])),
    ]
    m = rv.aggregate(scored)
    assert m["STD-001"]["recall"] == 0.5
    assert m["STD-002"]["fpRate"] == 0.5


# --- compare_to_baseline -------------------------------------------------------

def test_compare_flags_recall_regression():
    metrics = {"STD-001": {"recall": 0.5, "fpRate": 0.0}}
    baseline = {"perStandard": {"STD-001": {"recall": 1.0, "fpRate": 0.0}}}
    regs = rv.compare_to_baseline(metrics, baseline)
    assert any("recall" in r for r in regs)


def test_compare_flags_fp_rate_regression():
    metrics = {"STD-001": {"recall": 1.0, "fpRate": 0.25}}
    baseline = {"perStandard": {"STD-001": {"recall": 1.0, "fpRate": 0.0}}}
    regs = rv.compare_to_baseline(metrics, baseline)
    assert any("false-positive" in r for r in regs)


def test_compare_no_regression_when_equal():
    metrics = {"STD-001": {"recall": 1.0, "fpRate": 0.0}}
    baseline = {"perStandard": {"STD-001": {"recall": 1.0, "fpRate": 0.0}}}
    assert rv.compare_to_baseline(metrics, baseline) == []


def test_compare_skips_none_metrics():
    metrics = {"STD-001": {"recall": None, "fpRate": None}}
    baseline = {"perStandard": {"STD-001": {"recall": 1.0, "fpRate": 0.0}}}
    assert rv.compare_to_baseline(metrics, baseline) == []


# --- fmt -----------------------------------------------------------------------

def test_fmt():
    assert rv.fmt(None) == "—"
    assert rv.fmt(0.5) == "0.50"
    assert rv.fmt(1.0) == "1.00"
