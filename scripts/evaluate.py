#!/usr/bin/env python3
"""
LLM evaluator for the AI-assisted PR-evaluation pipeline.

Reads the context the workflow assembled in Step 4 (the prompt template, the PR
diff, and the concatenated active standards), asks the model to evaluate the diff
against the standards, and writes a schema-conforming findings array to
findings.json for the workflow's later steps to post and persist.

Design notes:

* The model is constrained with **structured outputs** to emit ONLY the detection
  fields it is actually qualified to produce: standardId, severity, description,
  file, lineRange. This guarantees the response parses and matches the expected
  shape — no defensive JSON-repair needed.

* The pipeline (this script) stamps the audit metadata: findingId, evaluatorModel,
  evaluatorVersion, timestamp, prNumber, commitSha, disposition. Those values must
  be authoritative, not model-generated — especially evaluatorVersion, which is the
  pinned standards ref that makes a finding forensically replayable.

* Extended thinking is intentionally OFF. The findings JSON is small, so output
  tokens (the dominant cost lever — see COST-MODEL.md) stay low. Turning thinking
  on would sharpen judgment at materially higher output cost; that is a production
  decision, not a demo default.
"""

import datetime
import hashlib
import json
import os
import sys
import uuid

import anthropic

# The three severities, mirrored from the standards. Kept here so the structured
# output can hard-constrain the model to valid values.
SEVERITIES = ["advisory", "required", "escalate"]


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def added_lines_by_file(diff_text):
    """Map each changed file to the text of its added (+) diff lines.

    Used to give each finding a content-based stableKey, so a dismissal carries
    forward across pushes only while the offending file's added content is unchanged.
    """
    added, cur = {}, None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            cur = None if path == "/dev/null" else path
            if cur is not None:
                added.setdefault(cur, [])
        elif line.startswith("+") and not line.startswith("+++") and cur is not None:
            added.setdefault(cur, []).append(line[1:])
    return {k: "\n".join(v) for k, v in added.items()}


def stable_key(standard_id, file, added_by_file):
    """Content-addressed identity for a finding, stable across unrelated pushes.

    Deliberately hashes the whole file's added lines rather than the finding's
    specific line range. This keeps carry-forward robust to LLM line-number jitter
    (the reported lineRange varies run-to-run), at the cost of one limitation: two
    findings of the SAME standard in the SAME file share a key, so dismissing or
    carrying one carries both. That is an acceptable trade — the dismissal UX already
    coalesces by standard+file — and is preferred over a line-range key that would
    break carry-forward whenever unrelated edits shift line numbers.
    """
    snippet = added_by_file.get(file, "") if file else ""
    digest = hashlib.sha256()
    digest.update((standard_id + "\0" + (file or "") + "\0" + snippet).encode("utf-8"))
    return digest.hexdigest()


def detection_schema():
    """The detection-only slice of finding.schema.json the model is allowed to emit.

    Nullable fields use anyOf (rather than a ["string", "null"] type array) for
    maximum compatibility with the structured-outputs JSON-schema subset.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "standardId": {"type": "string"},
            "severity": {"type": "string", "enum": SEVERITIES},
            "description": {"type": "string"},
            "file": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "lineRange": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                        },
                        "required": ["start", "end"],
                    },
                    {"type": "null"},
                ]
            },
        },
        "required": ["standardId", "severity", "description", "file", "lineRange"],
    }


def main():
    model = os.environ.get("EVALUATOR_MODEL", "claude-haiku-4-5")
    standards_ref = os.environ.get("EVALUATOR_VERSION", "unknown")
    pr_number = int(os.environ.get("PR_NUMBER", "0"))
    commit_sha = os.environ.get("COMMIT_SHA", "")

    prompt_template = read("prompt_template.md")
    pr_diff = read("pr.diff")
    active_standards = read("active_standards.md")

    item_schema = detection_schema()
    # The model returns an object with a single "findings" array — a top-level
    # object is the most broadly supported structured-output shape.
    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"findings": {"type": "array", "items": item_schema}},
        "required": ["findings"],
    }

    # Fill the prompt template's placeholders. {{FINDINGS_SCHEMA}} is given the
    # detection slice so the prompt's described shape matches what structured
    # outputs will actually enforce.
    prompt = (
        prompt_template.replace("{{PR_DIFF}}", pr_diff)
        .replace("{{ACTIVE_STANDARDS}}", active_standards)
        .replace("{{FINDINGS_SCHEMA}}", json.dumps(item_schema, indent=2))
        .replace("{{EVALUATOR_MODEL}}", model)
        .replace("{{EVALUATOR_VERSION}}", standards_ref)
    )

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        output_config={"format": {"type": "json_schema", "schema": output_schema}},
        messages=[{"role": "user", "content": prompt}],
    )

    # output_config guarantees the first text block is valid JSON matching the schema.
    raw = next(b.text for b in response.content if b.type == "text")
    detected = json.loads(raw)["findings"]

    # Stamp the audit metadata the pipeline owns, producing full
    # finding.schema.json-conforming records, plus a content-based stableKey used
    # for dismissal carry-forward across pushes.
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    added = added_lines_by_file(pr_diff)
    findings = [
        {
            "findingId": str(uuid.uuid4()),
            "standardId": d["standardId"],
            "severity": d["severity"],
            "file": d["file"],
            "lineRange": d["lineRange"],
            "description": d["description"],
            "disposition": "open",
            "dismissalReason": None,
            "evaluatorModel": model,
            "evaluatorVersion": standards_ref,
            "timestamp": now,
            "prNumber": pr_number,
            "commitSha": commit_sha,
            "stableKey": stable_key(d["standardId"], d["file"], added),
        }
        for d in detected
    ]

    with open("findings.json", "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2)

    # Emit token usage so real cost can be reconciled against COST-MODEL.md.
    u = response.usage
    print(
        f"evaluator={model} findings={len(findings)} "
        f"input_tokens={u.input_tokens} output_tokens={u.output_tokens}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
