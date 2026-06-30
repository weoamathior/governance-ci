#!/usr/bin/env python3
"""
Evaluator regression harness.

Runs every fixture in evals/fixtures/ through the real evaluator (or a deterministic
stub), scores the findings against each fixture's labels, aggregates recall and
false-positive rate per standard, and compares against evals/baseline.json — so a
prompt or standards change that regresses behavior is caught before it ships.

Modes:
  --stub  (default)  Deterministic harness self-test. Each fixture supplies its own
                     canned model output (stubFindings); no API key, no cost. This
                     proves the scoring and report plumbing — it does NOT evaluate
                     the model. Use it to see the report shape and to test the harness.
  --live             Calls the real model for each run. THIS is the regression test.

Exit status is non-zero if any fixture fails its labels, or any metric regresses past
tolerance against the baseline — so CI can gate a prompt/standards change on it.

Fixture label semantics:
  expect      list of {standardId, severity?} that MUST fire (recall). A fixture passes
              the check if the standard fired in >= passThreshold of the runs (and the
              severity matched, when given). passThreshold defaults to `runs`.
  expectNot   list of standardIds that must NOT fire (precision). Passes if the standard
              fired in <= tolerance of the runs. tolerance defaults to 0.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-6": (3.0, 15.0), "claude-opus-4-8": (5.0, 25.0)}
EPS = 1e-9

# In governance-ci this harness is the shared engine; the corpus (fixtures + baseline)
# lives in the standards repo and is pointed at via --fixtures-dir / --baseline, and the
# policy via --standards-dir. The constants below are only fallbacks for a local sibling
# layout; CI always passes the paths explicitly.
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
EVALUATE_PY = SCRIPT_DIR / "evaluate.py"
FIXTURES_DIR = SCRIPT_DIR / "fixtures"
BASELINE_PATH = SCRIPT_DIR / "baseline.json"

# Same stub module shape as dry_run.py: returns the fixture's canned findings and
# estimates tokens from prompt length.
FAKE_ANTHROPIC = '''\
import os

class _Block:
    def __init__(self, text):
        self.type = "text"; self.text = text

class _Usage:
    def __init__(self, i, o):
        self.input_tokens = i; self.output_tokens = o

class _Resp:
    def __init__(self, text, i, o):
        self.content = [_Block(text)]; self.usage = _Usage(i, o)

class _Messages:
    def create(self, model, max_tokens, output_config, messages):
        text = os.environ["DRY_RUN_STUB_FINDINGS"]
        prompt = messages[0]["content"]
        return _Resp(text, max(1, len(prompt) // 4), max(1, len(text) // 4))

class Anthropic:
    def __init__(self, *a, **k):
        self.messages = _Messages()
'''


def default_standards_dir():
    # Fallback only — CI passes --standards-dir explicitly (the checked-out standards repo).
    return SCRIPT_DIR


def run_once(fx, mode, model, standards_dir):
    """Run the evaluator on one fixture diff; return {standardId: severity}."""
    with tempfile.TemporaryDirectory() as d:
        w = pathlib.Path(d)
        (w / "prompt_template.md").write_text(
            (standards_dir / "evaluator" / "pr-evaluation-prompt.md").read_text()
        )
        stds = sorted((standards_dir / "standards").glob("STD-*.md"))
        (w / "active_standards.md").write_text("\n".join(s.read_text() for s in stds))
        (w / "pr.diff").write_text("\n".join(fx["diff"]) + "\n")

        env = dict(os.environ, EVALUATOR_MODEL=model, EVALUATOR_VERSION="evalharness",
                   PR_NUMBER="0", COMMIT_SHA="evalfixture")
        if mode == "stub":
            (w / "anthropic.py").write_text(FAKE_ANTHROPIC)
            env["PYTHONPATH"] = str(w)
            env["DRY_RUN_STUB_FINDINGS"] = json.dumps(fx["stubFindings"])
            env.setdefault("ANTHROPIC_API_KEY", "stub")

        p = subprocess.run([sys.executable, str(EVALUATE_PY)], cwd=w, env=env,
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"evaluate.py failed for '{fx['name']}': {p.stderr or p.stdout}")
        findings = json.loads((w / "findings.json").read_text())
    return {f["standardId"]: f["severity"] for f in findings}


def score(fx, results):
    """Return (ok, checks). checks is a list of per-label dicts with pass/fail."""
    runs = len(results)
    pt = fx.get("passThreshold", runs)
    tol = fx.get("tolerance", 0)
    checks = []
    for exp in fx.get("expect", []):
        sid = exp["standardId"]
        hits = sum(1 for r in results if sid in r)
        sev_ok = ("severity" not in exp) or \
                 sum(1 for r in results if r.get(sid) == exp["severity"]) >= pt
        checks.append({"kind": "expect", "sid": sid, "sev": exp.get("severity"),
                       "count": hits, "runs": runs, "passed": hits >= pt and sev_ok})
    for sid in fx.get("expectNot", []):
        viol = sum(1 for r in results if sid in r)
        checks.append({"kind": "forbid", "sid": sid, "count": viol, "runs": runs,
                       "passed": viol <= tol})
    return all(c["passed"] for c in checks), checks


def aggregate(scored):
    """scored: list of (fx, ok, checks). Returns per-standard recall / fp-rate."""
    ps = {}
    for _fx, _ok, checks in scored:
        for c in checks:
            s = ps.setdefault(c["sid"], {"flag": 0, "flagPass": 0, "noflag": 0, "noflagViol": 0})
            if c["kind"] == "expect":
                s["flag"] += 1
                s["flagPass"] += 1 if c["passed"] else 0
            else:
                s["noflag"] += 1
                s["noflagViol"] += 0 if c["passed"] else 1
    metrics = {}
    for sid, s in sorted(ps.items()):
        metrics[sid] = {
            "recall": (s["flagPass"] / s["flag"]) if s["flag"] else None,
            "fpRate": (s["noflagViol"] / s["noflag"]) if s["noflag"] else None,
        }
    return metrics


def compare_to_baseline(metrics, baseline):
    regressions = []
    base = baseline.get("perStandard", {})
    for sid, m in metrics.items():
        b = base.get(sid, {})
        if m["recall"] is not None and b.get("recall") is not None and m["recall"] < b["recall"] - EPS:
            regressions.append(f"{sid}: recall {b['recall']:.2f} → {m['recall']:.2f}")
        if m["fpRate"] is not None and b.get("fpRate") is not None and m["fpRate"] > b["fpRate"] + EPS:
            regressions.append(f"{sid}: false-positive rate {b['fpRate']:.2f} → {m['fpRate']:.2f}")
    return regressions


def fmt(x):
    return "—" if x is None else f"{x:.2f}"


def build_report(mode, model, scored, metrics, regressions):
    passed = sum(1 for _f, ok, _c in scored if ok)
    total = len(scored)
    lines = []
    tag = "STUB — harness self-test, NOT a model evaluation" if mode == "stub" else f"LIVE — model: {model}"
    lines.append(f"# Evaluator regression report")
    lines.append(f"_{tag}_\n")
    lines.append(f"**Fixtures passed: {passed}/{total}**\n")

    lines.append("## Per-standard")
    lines.append("| Standard | Recall (should-flag) | False-pos rate (should-not-flag) |")
    lines.append("|---|---|---|")
    for sid, m in metrics.items():
        lines.append(f"| {sid} | {fmt(m['recall'])} | {fmt(m['fpRate'])} |")
    lines.append("")

    lines.append("## Per-fixture")
    lines.append("| Fixture | Intent | Result | Checks |")
    lines.append("|---|---|---|---|")
    for fx, ok, checks in scored:
        cs = "; ".join(
            f"{'✓' if c['passed'] else '✗'} "
            f"{'expect' if c['kind']=='expect' else 'forbid'} {c['sid']}"
            f"{(' '+c['sev']) if c.get('sev') else ''} ({c['count']}/{c['runs']})"
            for c in checks) or "—"
        lines.append(f"| {fx['name']} | {fx['intent']} | {'✅' if ok else '❌'} | {cs} |")
    lines.append("")

    lines.append("## Regressions vs baseline")
    if regressions:
        for r in regressions:
            lines.append(f"- ⚠️ {r}")
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="call the real model (default: stub)")
    ap.add_argument("--model", default=os.environ.get("EVALUATOR_MODEL", "claude-haiku-4-5"))
    ap.add_argument("--standards-dir", default=None, help="path to a standards checkout")
    ap.add_argument("--update-baseline", action="store_true", help="write current metrics to baseline.json")
    ap.add_argument("--out", default=None, help="write <PREFIX>.md and <PREFIX>.metrics.json")
    ap.add_argument("--filter", default=None, help="only run fixtures whose filename contains this substring")
    ap.add_argument("--runs", type=int, default=None, help="override each fixture's run count")
    ap.add_argument("--fixtures-dir", default=None, help="corpus dir (defaults beside this script; CI points at the standards checkout)")
    ap.add_argument("--baseline", default=None, help="baseline.json path (defaults beside this script)")
    args = ap.parse_args()

    mode = "live" if args.live else "stub"
    standards_dir = pathlib.Path(args.standards_dir) if args.standards_dir else default_standards_dir()
    fixtures_dir = pathlib.Path(args.fixtures_dir) if args.fixtures_dir else FIXTURES_DIR
    baseline_path = pathlib.Path(args.baseline) if args.baseline else BASELINE_PATH
    if not standards_dir.is_dir():
        sys.exit(f"standards dir not found: {standards_dir}")
    if args.live and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("--live requires ANTHROPIC_API_KEY")

    fixtures = []
    for p in sorted(fixtures_dir.glob("*.json")):
        fx = json.loads(p.read_text())
        fx["_stem"] = p.stem
        fixtures.append(fx)
    if args.filter:
        fixtures = [fx for fx in fixtures if args.filter in fx["_stem"]]
    if not fixtures:
        sys.exit(f"no fixtures matched in {fixtures_dir}")

    scored = []
    for fx in fixtures:
        # Always run the fixture's full run count so passThreshold/tolerance mean the
        # same thing in both modes. Stub runs are local and deterministic, so this is
        # cheap; live runs exercise real run-to-run variation.
        runs = args.runs or fx.get("runs", 3)
        results = [run_once(fx, mode, args.model, standards_dir) for _ in range(runs)]
        ok, checks = score(fx, results)
        scored.append((fx, ok, checks))

    metrics = aggregate(scored)
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    regressions = compare_to_baseline(metrics, baseline)
    report = build_report(mode, args.model, scored, metrics, regressions)
    print(report)

    if args.out:
        pathlib.Path(args.out + ".md").write_text(report)
        pathlib.Path(args.out + ".metrics.json").write_text(
            json.dumps({"perStandard": metrics}, indent=2))

    if args.update_baseline:
        baseline_path.write_text(json.dumps({"perStandard": metrics}, indent=2) + "\n")
        print(f"baseline updated: {baseline_path}")
        return

    failed = sum(1 for _f, ok, _c in scored if not ok)
    if failed or regressions:
        sys.exit(f"FAIL: {failed} fixture(s) failed, {len(regressions)} regression(s)")
    print("PASS: all fixtures green, no regressions.")


if __name__ == "__main__":
    main()
