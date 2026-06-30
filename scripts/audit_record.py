#!/usr/bin/env python3
"""
Write the merge-time governance audit record for a PR to
audit-log/{prNumber}-{mergeSha}.json.

The final, post-dismissal findings are read from the PR's sticky governance comment
(the same canonical state the dismissal loop maintains), so the record reflects exactly
what was open, dismissed, and by whom at merge time. If there is no sticky comment the PR
was auto-approved (or never evaluated); a minimal record is written noting that, with the
matched auto-approve rule when it can be recovered from the auto-approve comment.

This is the durable forensic artifact: it is committed to the repository at merge, and
each finding it contains carries its evaluatorVersion (the pinned standards ref) and, for
dismissed findings, the dismissal provenance (who/why/when).
"""

import argparse
import datetime
import json
import os
import re
import sys

# Reuse the sticky-comment helpers from the shared governance-state module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import governance_state as gs  # noqa: E402

AUTO_APPROVE_RE = re.compile(r"Auto-approved by \*\*(RULE-\d{3})\*\*")


def build_record(pr, merge_sha, merged_at):
    record = {
        "prNumber": int(pr),
        "mergeCommitSha": merge_sha,
        "mergedAt": merged_at,
        "recordedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    sticky = gs.find_sticky(pr)
    if sticky:
        state = gs.parse_data(sticky["body"]) or {}
        record["evaluated"] = True
        record["findings"] = state.get("findings", [])
        return record

    # No sticky comment -> auto-approved or unevaluated. Recover the rule if posted.
    rule = None
    for c in gs.list_comments(pr):
        m = AUTO_APPROVE_RE.search(c.get("body") or "")
        if m:
            rule = m.group(1)
            break
    record["evaluated"] = False
    record["autoApproved"] = rule is not None
    record["autoApproveRule"] = rule
    record["findings"] = []
    record["note"] = (f"Auto-approved by {rule}; no LLM evaluation performed."
                      if rule else "No governance evaluation was recorded for this PR.")
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True)
    ap.add_argument("--merge-sha", required=True)
    ap.add_argument("--merged-at", default="")
    a = ap.parse_args()

    record = build_record(a.pr, a.merge_sha, a.merged_at)

    os.makedirs("audit-log", exist_ok=True)
    path = f"audit-log/{a.pr}-{a.merge_sha}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    print(f"wrote {path} (evaluated={record['evaluated']}, "
          f"findings={len(record['findings'])})")


if __name__ == "__main__":
    main()
