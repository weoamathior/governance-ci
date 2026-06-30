#!/usr/bin/env python3
"""
Governance state: the sticky PR comment (canonical findings state) and the
`governance/gate` commit status (the mutable merge gate).

The sticky comment is a single bot comment per PR identified by a marker. It carries a
human-readable table plus a hidden JSON block (the canonical state). Two subcommands
operate on it, and both run inside GitHub Actions where `gh` is authenticated:

  publish  Called by the evaluation workflow after evaluate.py produces findings.json.
           Reads the prior sticky comment, CARRIES FORWARD any dismissal whose stableKey
           matches a new finding (so a dismissal survives a re-evaluation as long as the
           offending content is unchanged), upserts the sticky comment, and sets the gate.

  dismiss  Called by the dismissal workflow when someone comments `DISMISS STD-XXX: ...`.
           Marks the matching OPEN findings dismissed with provenance, upserts the sticky
           comment, recomputes the gate, and posts a confirmation.

The gate is a commit status (context `governance/gate`) rather than a job exit code,
because a comment-triggered workflow cannot change the evaluation job's pass/fail — only
a status both workflows can write.
"""

import argparse
import base64
import datetime
import json
import os
import subprocess
import sys

MARKER = "ai-governance:findings v1"
DATA_OPEN = "<!-- ai-governance:data"
DATA_CLOSE = "-->"
GATE_CONTEXT = "governance/gate"
BLOCKING = ("required", "escalate")


def repo():
    return os.environ["GITHUB_REPOSITORY"]  # "owner/name"


def gh_get(path):
    r = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True)
    return json.loads(r.stdout)


def gh_write(method, path, payload):
    # JSON via stdin avoids all shell/escaping issues with comment bodies.
    subprocess.run(["gh", "api", "--method", method, path, "--input", "-"],
                   input=json.dumps(payload), text=True, check=True, capture_output=True)


def list_comments(pr):
    # Paginate: the sticky comment is posted early, so on a busy PR it would fall
    # off a single 100-comment page and be missed (creating a duplicate sticky).
    out, page = [], 1
    while True:
        chunk = gh_get(f"repos/{repo()}/issues/{pr}/comments?per_page=100&page={page}")
        out.extend(chunk)
        if len(chunk) < 100:
            return out
        page += 1


def find_sticky(pr):
    for c in list_comments(pr):
        if MARKER in (c.get("body") or ""):
            return c
    return None


def parse_data(body):
    """Pull the canonical state JSON out of the hidden data block."""
    i = body.find(DATA_OPEN)
    if i == -1:
        return None
    j = body.find(DATA_CLOSE, i)
    if j == -1:
        return None
    raw = body[i + len(DATA_OPEN):j].strip()
    # The data block is base64 so that no stored string (a dismissal reason, an LLM
    # description) containing the literal "-->" can truncate it.
    try:
        return json.loads(base64.b64decode(raw))
    except (json.JSONDecodeError, ValueError):
        return None


def compute_gate(findings):
    blocking = [f for f in findings
                if f.get("disposition") == "open" and f.get("severity") in BLOCKING]
    if blocking:
        sev = "/".join(sorted({f["severity"] for f in blocking}))
        return "failure", f"blocked — {len(blocking)} open {sev} finding(s)"
    return "success", "no open blocking findings"


def md_cell(s):
    """Escape a value for safe inclusion in a Markdown table cell (user-supplied
    reasons could otherwise inject pipes or extra rows)."""
    return str(s).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render(state):
    findings = state.get("findings", [])
    n_open = sum(1 for f in findings if f.get("disposition") == "open")
    n_dis = sum(1 for f in findings if f.get("disposition") == "dismissed")
    out = [f"<!-- {MARKER} -->",
           f"### 🛡️ AI Governance — {n_open} open / {n_dis} dismissed", ""]
    if findings:
        out += ["| Standard | Severity | Location | Disposition |", "|---|---|---|---|"]
        for f in findings:
            loc = f.get("file") or "—"
            lr = f.get("lineRange")
            if lr and lr.get("start"):
                loc += f":{lr['start']}"
            if f.get("disposition") == "dismissed":
                d = f.get("dismissal") or {}
                disp = f'⚪ dismissed — "{d.get("reason", "")}" (@{d.get("dismissedBy", "?")})'
            else:
                disp = "🔴 open"
            out.append(f"| {f['standardId']} | {f['severity']} | {md_cell(loc)} | {md_cell(disp)} |")
        out.append("")
    else:
        out += ["No findings. ✅", ""]
    gstate, gdesc = compute_gate(findings)
    out.append(f"<sub>Gate: {'✅' if gstate == 'success' else '❌'} {gdesc} · "
               f"Dismiss with `DISMISS STD-XXX: your reason`</sub>")
    blob = base64.b64encode(json.dumps(state).encode("utf-8")).decode("ascii")
    out += ["", DATA_OPEN, blob, DATA_CLOSE]
    return "\n".join(out)


def upsert_sticky(pr, state):
    body = render(state)
    existing = find_sticky(pr)
    if existing:
        gh_write("PATCH", f"repos/{repo()}/issues/comments/{existing['id']}", {"body": body})
    else:
        gh_write("POST", f"repos/{repo()}/issues/{pr}/comments", {"body": body})


def set_gate(sha, findings, target_url=None):
    gstate, gdesc = compute_gate(findings)
    payload = {"state": gstate, "context": GATE_CONTEXT, "description": gdesc[:140]}
    if target_url:
        payload["target_url"] = target_url
    gh_write("POST", f"repos/{repo()}/statuses/{sha}", payload)
    return gstate, gdesc


def comment(pr, body):
    gh_write("POST", f"repos/{repo()}/issues/{pr}/comments", {"body": body})


def cmd_publish(a):
    new = json.load(open(a.findings))

    # Carry forward dismissals from the prior sticky comment, keyed by stableKey.
    prior = find_sticky(a.pr)
    prior_dismissed = {}
    if prior:
        ps = parse_data(prior["body"]) or {}
        for f in ps.get("findings", []):
            if f.get("disposition") == "dismissed" and f.get("stableKey"):
                prior_dismissed[f["stableKey"]] = f
    carried = 0
    for f in new:
        k = f.get("stableKey")
        if k and k in prior_dismissed:
            src = prior_dismissed[k]
            f["disposition"] = "dismissed"
            f["dismissalReason"] = src.get("dismissalReason")
            f["dismissal"] = src.get("dismissal")
            carried += 1

    state = {"headSha": a.sha, "findings": new}
    upsert_sticky(a.pr, state)
    gstate, gdesc = set_gate(a.sha, new, a.target_url)
    print(f"published {len(new)} finding(s); carried {carried} dismissal(s); "
          f"gate={gstate} ({gdesc})")


def cmd_dismiss(a):
    sticky = find_sticky(a.pr)
    if not sticky:
        sys.exit("no governance sticky comment on this PR")
    state = parse_data(sticky["body"])
    if not state:
        sys.exit("sticky comment has no parseable data block")

    findings = state.get("findings", [])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    matched = 0
    for f in findings:
        if f.get("disposition") != "open":
            continue
        if f.get("standardId") != a.standard:
            continue
        if a.file and (f.get("file") or "") != a.file:
            continue
        f["disposition"] = "dismissed"
        f["dismissalReason"] = a.reason
        f["dismissal"] = {
            "dismissedBy": a.actor,
            "dismisserRole": a.role,
            "reason": a.reason,
            "timestamp": now,
            "atCommitSha": state.get("headSha"),
            "standardsRef": os.environ.get("EVALUATOR_VERSION", ""),
        }
        matched += 1

    gstate = "unchanged"
    if matched:
        upsert_sticky(a.pr, state)
        # Set the gate on the CURRENT head passed in by the workflow, not on the
        # sticky's stored headSha — that could be stale if a commit was pushed after
        # the last evaluation, and branch protection checks the current head's status.
        gstate, _ = set_gate(a.sha, findings)
        comment(a.pr, f"✅ Dismissed {matched} open **{a.standard}** finding(s) "
                      f"(@{a.actor}, role: {a.role}). Reason: _{a.reason}_. "
                      f"Gate is now **{gstate}**.")
    else:
        comment(a.pr, f"⚠️ No open **{a.standard}** finding to dismiss on this PR.")
    print(f"dismiss {a.standard}: matched {matched}, gate={gstate}")


def main():
    p = argparse.ArgumentParser(description="Governance sticky comment + gate status")
    sub = p.add_subparsers(dest="cmd", required=True)

    pub = sub.add_parser("publish", help="upsert sticky + set gate after evaluation")
    pub.add_argument("--pr", required=True)
    pub.add_argument("--sha", required=True)
    pub.add_argument("--findings", required=True)
    pub.add_argument("--target-url", default=None)
    pub.set_defaults(func=cmd_publish)

    dis = sub.add_parser("dismiss", help="mark findings dismissed + recompute gate")
    dis.add_argument("--pr", required=True)
    dis.add_argument("--sha", required=True, help="current PR head SHA to set the gate on")
    dis.add_argument("--standard", required=True)
    dis.add_argument("--file", default=None)
    dis.add_argument("--reason", required=True)
    dis.add_argument("--actor", required=True)
    dis.add_argument("--role", default="unknown")
    dis.set_defaults(func=cmd_dismiss)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
