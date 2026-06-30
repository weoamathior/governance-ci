#!/usr/bin/env python3
"""
Standards/engine version-drift report.

Governance coverage erodes silently when repos pin to old policy. This report makes that
visible: for a given pinning variable (default GOVERNANCE_STANDARDS_REF), it resolves every
onboarded product repo's EFFECTIVE ref — the repo-level Actions variable if set, otherwise
the org-level default — and flags the ones that diverge.

Why this matters (and why we do NOT instead force everyone onto a moving tag): the org
default points at an immutable tag; most repos inherit it; a dormant repo with no override
picks up the current default the moment it's next touched. So a divergent repo is one
someone DELIBERATELY pinned (a canary ahead, or a frozen laggard). This report surfaces
those so they stay intentional rather than forgotten.

The classification is deliberately ordering-agnostic: tags are a mix (`std-2026.06`,
`v2.0-preview`, a SHA), so we don't guess "ahead vs behind" — we flag divergence and let a
human read it. The gh/network calls (gather) are not unit-tested; classify/render are pure.
"""

import argparse
import json
import os
import subprocess
import sys


# --- pure logic (unit-tested) --------------------------------------------------

def classify(default_ref, repos):
    """repos: list of {"name", "ref", "source"} where source is "org-default" or
    "repo-override". Returns rows annotated with a status."""
    rows = []
    for r in repos:
        if r["source"] == "org-default":
            status = "default"            # inherits the org default — healthy
        elif r["ref"] == default_ref:
            status = "redundant-override"  # pins the same value — clean it up
        else:
            status = "divergent"           # canary ahead or frozen laggard — eyeball it
        rows.append({"name": r["name"], "ref": r["ref"],
                     "source": r["source"], "status": status})
    return rows


def summarize(rows):
    out = {"default": 0, "redundant-override": 0, "divergent": 0}
    for r in rows:
        out[r["status"]] += 1
    return out


def render(default_ref, var_name, rows):
    counts = summarize(rows)
    lines = [f"# Governance version-drift report — `{var_name}`", "",
             f"**Org default:** `{default_ref or '(unset)'}`  ·  "
             f"**Repos:** {len(rows)}  ·  "
             f"inherit default: {counts['default']}  ·  "
             f"divergent: {counts['divergent']}  ·  "
             f"redundant overrides: {counts['redundant-override']}", ""]
    divergent = [r for r in rows if r["status"] == "divergent"]
    redundant = [r for r in rows if r["status"] == "redundant-override"]
    if divergent:
        lines += ["## ⚠️ Divergent (deliberately pinned — confirm still intended)",
                  "| Repo | Effective ref |", "|---|---|"]
        lines += [f"| {r['name']} | `{r['ref']}` |" for r in sorted(divergent, key=lambda x: x["name"])]
        lines.append("")
    if redundant:
        lines += ["## 🧹 Redundant overrides (pin equals the default — safe to delete)",
                  "| Repo | Pinned ref |", "|---|---|"]
        lines += [f"| {r['name']} | `{r['ref']}` |" for r in sorted(redundant, key=lambda x: x["name"])]
        lines.append("")
    if not divergent and not redundant:
        lines += ["✅ Every repo inherits the org default. No drift.", ""]
    return "\n".join(lines)


# --- gh I/O (not unit-tested) --------------------------------------------------

def gh_json(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    return (json.loads(r.stdout) if r.returncode == 0 else None)


def org_default(org, var):
    v = gh_json(f"orgs/{org}/actions/variables/{var}")
    return v["value"] if v else None


def product_repos(org, caller_path):
    """Onboarded = the repo contains the governance caller workflow."""
    repos, page = [], 1
    while True:
        chunk = gh_json(f"orgs/{org}/repos?per_page=100&page={page}") or []
        for repo in chunk:
            if gh_json(f"repos/{org}/{repo['name']}/contents/{caller_path}"):
                repos.append(repo["name"])
        if len(chunk) < 100:
            return repos
        page += 1


def effective_ref(org, repo, var, default_ref):
    v = gh_json(f"repos/{org}/{repo}/actions/variables/{var}")
    if v:
        return {"name": repo, "ref": v["value"], "source": "repo-override"}
    return {"name": repo, "ref": default_ref, "source": "org-default"}


def gather(org, var, caller_path):
    default_ref = org_default(org, var)
    repos = [effective_ref(org, r, var, default_ref) for r in product_repos(org, caller_path)]
    return default_ref, repos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default=os.environ.get("GITHUB_REPOSITORY_OWNER"))
    ap.add_argument("--var", default="GOVERNANCE_STANDARDS_REF")
    ap.add_argument("--caller-path", default=".github/workflows/governance.yml")
    ap.add_argument("--repos-json", help="skip discovery; read [{name,ref,source}] from this file")
    ap.add_argument("--default-ref", help="org default ref (with --repos-json)")
    ap.add_argument("--out", help="write the report here (also printed)")
    a = ap.parse_args()

    if a.repos_json:
        repos = json.load(open(a.repos_json))
        default_ref = a.default_ref
    else:
        if not a.org:
            sys.exit("need --org (or GITHUB_REPOSITORY_OWNER)")
        default_ref, repos = gather(a.org, a.var, a.caller_path)

    rows = classify(default_ref, repos)
    report = render(default_ref, a.var, rows)
    print(report)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(report)


if __name__ == "__main__":
    main()
