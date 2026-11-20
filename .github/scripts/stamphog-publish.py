#!/usr/bin/env python3
import importlib.util
import json
import re
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "shadow_gate", Path(__file__).resolve().parents[1] / "merge-gate" / "gate.py"
)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

MARKER = "<!-- stamphog-sticky-review -->"
PUBLISHER = "github-actions[bot]"


def render_receipt(result, head):
    verdict = result.get("final_verdict")
    if not isinstance(verdict, str) or not re.fullmatch(r"[A-Z]+(?:[-_][A-Z]+)*", verdict):
        raise ValueError("StampHog did not return a recognized verdict format")
    verdict = verdict.replace("-", "_")
    body = str(result.get("review_body") or "").replace(MARKER, "")
    return (
        f"{MARKER}\nStampHog reviewed `{head}` — verdict: **{verdict}**\n\n"
        f"Advisory shadow review; no approval or merge was performed.\n\n{body}"
    )


def publish(result, repo, pr, expected_head):
    if repo != GATE.FORK or pr <= 0 or not GATE.HEAD_RE.fullmatch(expected_head):
        raise ValueError("invalid shadow review target")
    if (result.get("repo"), result.get("pr_number"), result.get("head_sha")) != (repo, pr, expected_head):
        raise ValueError("verdict does not match the admitted PR and head")
    path = f"repos/{repo}/pulls/{pr}"
    if GATE.eligible_head(GATE.gh("GET", path), repo) != expected_head:
        return
    comments = GATE.comment_pages(repo, pr)
    if GATE.finalized_record(comments, expected_head) is None:
        return
    existing = [
        comment for comment in comments
        if (comment.get("user") or {}).get("login") == PUBLISHER
        and (comment.get("body") or "").startswith(f"{MARKER}\n")
    ]
    if len(existing) > 1:
        raise ValueError("ambiguous StampHog receipt; inspect the bot's comments")
    body = render_receipt(result, expected_head)
    if GATE.eligible_head(GATE.gh("GET", path), repo) != expected_head:
        return
    if not existing:
        GATE.gh("POST", f"repos/{repo}/issues/{pr}/comments", {"body": body})
    elif existing[0]["body"] != body:
        GATE.gh("PATCH", f"repos/{repo}/issues/comments/{existing[0]['id']}", {"body": body})


if __name__ == "__main__":
    result_path, repository, number, head = sys.argv[1:]
    publish(json.loads(Path(result_path).read_text()), repository, int(number), head)
