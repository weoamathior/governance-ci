# governance-ci

The central engine and CI library for the AI governance pipeline. Every product repo and
the standards repo consume from here, pinned to a version — so the logic lives in **one
place**, not copied across the fleet.

This is the scaffold of the enterprise central repo described in `SCALING-ARCHITECTURE.md`.

## The three repos

| Repo | Holds | Consumed by |
|---|---|---|
| **governance-ci** (this) | The single evaluator engine + the reusable workflows | everyone |
| **governance-standards** | The policy (standards, prompt, schema, rules) **and its tests** (the evals corpus + baseline) | the eval gate |
| each **product repo** (×N) | A ~20-line caller workflow. Nothing else. | — |

## Layout

```
governance-ci/
├── scripts/                       # THE SINGLE ENGINE — one copy, period
│   ├── evaluate.py                #   the evaluator (LLM call -> schema-conforming findings)
│   ├── governance_state.py        #   sticky comment + governance/gate status (publish / dismiss)
│   ├── audit_record.py            #   merge-time audit record
│   └── run_evals.py               #   the regression harness (reads the corpus from standards)
├── .github/workflows/             # REUSABLE workflows (workflow_call)
│   ├── pr-evaluation.yml          #   evaluate a PR  (called by product repos)
│   ├── dismiss-finding.yml        #   handle DISMISS (called by product repos)
│   ├── audit-log.yml              #   write the audit record (called by product repos)
│   └── standards-evals.yml        #   run the corpus (called by the standards repo)
└── examples/                      # thin callers to copy into consuming repos
    ├── app-governance.yml         #   -> each product repo's .github/workflows/governance.yml
    └── standards-evals.yml        #   -> the standards repo's .github/workflows/evals.yml
```

## How a product repo consumes it

Drop `examples/app-governance.yml` in as `.github/workflows/governance.yml`. That ~20-line
file (pinned to `@v1`) is the entire per-repo footprint — it routes each event to the right
reusable workflow here. Config comes from org-level secrets/variables via `secrets: inherit`.

## How the standards repo consumes it

Drop `examples/standards-evals.yml` in as `.github/workflows/evals.yml`. On any policy PR it
calls `standards-evals.yml` here, which runs the corpus (in the standards repo) through the
engine (here). **App PRs are gated by the standards; standards PRs are gated by the evals.**

## The single-engine point

`evaluate.py` lives here once and is used by both the production pipeline *and* the
regression suite — so the corpus tests exactly what runs in production; there is no drift.
This resolves the POC's interim state, where the app repo and the standards repo each
carried their own copy. (Migration: delete those copies and point both at this repo.)

## Mechanism note

The reusable workflows fetch the engine by checking out **this repo at the same ref the
caller pinned** — derived from `github.workflow_ref` (`${GITHUB_WORKFLOW_REF##*@}`) — into
`.ci`, then run `python3 .ci/scripts/...`. This keeps the scripts in one repo, pulled on
demand, versioned. Composite actions are an equivalent alternative (they auto-fetch their
own files on `uses:`); this scaffold uses the self-checkout pattern because it avoids the
same-repo-action self-reference wrinkle and keeps the orchestration in one readable place.

## Versioning & rollout

- **Tag releases** (`v1`, `v1.1`, …). Callers pin `@v1`; the reusable workflows fetch the
  matching engine version. Promote a change by moving the `v1` tag or bumping callers.
- **Promote deliberately.** A change here can reach every consuming repo at once — so run
  the standards regression corpus before moving the tag. One-place control, automated safety
  net. (The corpus is the gate; see `governance-standards/evals/`.)
- **Onboard a repo** = add the one caller file + grant org-secret access + add it to the org
  ruleset requiring `governance/gate`. All three are API-automatable.

## Config (set once at the org)

| Setting | Kind |
|---|---|
| `LLM_API_KEY` | org **secret** |
| `EVALUATOR_MODEL`, `GOVERNANCE_STANDARDS_REF` | org **variables** |
| `GOVERNANCE_CI_REPO` / `GOVERNANCE_CI_REF` (optional) | org **variables** — engine `"owner/name"` and ref; default `<owner>/governance-ci` + `v1` |
| `GOVERNANCE_STANDARDS_REPO` (optional, `"owner/name"`) | org **variable** — set only if the standards repo is renamed; defaults to `<owner>/governance-standards` |
| Require `governance/gate` before merge | org **ruleset** (target repos by name pattern) |

## Renaming the repos

Every cross-repo reference is **config, not code** — a `…_REPO` org variable with a sensible
default — so a rename is a variable change, not a code hunt:

- **Engine repo** → set org var `GOVERNANCE_CI_REPO` (`"owner/name"`); `GOVERNANCE_CI_REF` pins
  its version (default `v1`). No workflow edits. (We do *not* self-derive these from
  `github.job_workflow_ref` — it comes back **empty** on this runner, which silently fell the
  checkout back to the caller repo; config vars are the reliable mechanism.)
- **Standards repo** → set org var `GOVERNANCE_STANDARDS_REPO`. No workflow edits.
- **Caller `uses:` lines are the one unavoidable literal.** GitHub forbids variables in
  `uses:`, so each consuming repo's caller (`examples/app-governance.yml` and the standards
  repo's `examples/standards-evals.yml`) names this engine repo literally. Those `>>> RENAME`
  lines are the entire code-level swap surface when you rename the engine.

## Testing

Two layers, deliberately separate:

- **Engine unit tests** (`tests/`, run by `.github/workflows/tests.yml`) — fast, hermetic
  pytest over the deterministic logic: gate computation, dismissal matching + provenance,
  dismissal carry-forward by `stableKey`, the sticky-comment render/parse round-trip (incl.
  the base64 `-->` defense), audit-record building, and the evals scoring/regression math.
  No network, no `gh`, no API key. Run locally with `python -m pytest tests/ -q`.
- **Standards regression corpus** (`governance-standards/evals/`) — the *live* model test:
  does the evaluator still flag the right things. Slow, paid, non-deterministic; it gates a
  prompt/standards change, not an engine change.

The unit tests cover the engine's decision logic so a refactor can't silently open the gate;
the corpus covers model behavior. The LLM call in `evaluate.py` and the `gh`/network helpers
are intentionally out of unit scope (covered by the corpus and by real PR runs).

## Status: scaffold

Real and consolidated: the four engine scripts (proven in the POC) and the four reusable
workflows (ported from the proven app/standards workflows, with script paths repointed to
`.ci/scripts/`). To make it live: create the `your-org/governance-ci` repo, push this, tag
`v1`, set the org secrets/variables, and switch the product + standards repos to the thin
callers in `examples/`. The authority check in `dismiss-finding.yml` is still the owner-only
POC stub — wire the team-membership tiers (`DISMISSAL-GOVERNANCE.md`) when the repos are in
an org with teams.
