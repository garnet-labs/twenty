#!/usr/bin/env python3
"""merge-gate: reads the Garnet Runtime Review comment on a pull request and posts
what the record says, in the record's own terms, so a reviewer sees the outcome
without opening the fold.

Garnet decides the outcome (nothing new, changed, capture incomplete); this
gate only relays it and names the changed destinations with the execution
chain that reached them. It classifies nothing itself. Missing, pending, stale
or incomplete evidence is undeterminable, never "no change".

Proof of concept. Garnet reports; the reviewer decides.
"""

import argparse
import html
import json
import re
import subprocess

MARKER = "<!-- merge-gate-poc -->"
BOT = "garnet-runtime-review[bot]"
FORK = "garnet-labs/twenty"
HEAD_RE = re.compile(r"[0-9a-f]{40}")
COMMIT_RE = re.compile(r"<!--\s*garnet:commit\s+([0-9a-f]{40})\s*-->")

SUMMARY_RE = re.compile(r"<!--\s*garnet:summary\s+(\{.*?\})\s*-->", re.DOTALL)
EXPLAINER_RE = re.compile(r"<details>\s*<summary><sub>How to read this</sub></summary>.*?</details>", re.DOTALL)
JOB_FOLD_RE = re.compile(r"<details[^>]*>\s*<summary>(.*?)</summary>(.*?)</details>", re.DOTALL)
JOB_NAME_RE = re.compile(r"<b>([^<]+)</b>|<code>([^<]+)</code>")
DIFF_FENCE_RE = re.compile(r"```diff\n(.*?)```", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
DEFANG_RE = re.compile(r"\[\.\]")

DEPENDENCY_FILE_RE = re.compile(
    r"(^|/)(package\.json|pnpm-lock\.yaml|pnpm-workspace\.yaml|yarn\.lock|package-lock\.json|"
    r"pyproject\.toml|uv\.lock|requirements[^/]*\.txt|Cargo\.toml|Cargo\.lock|go\.mod|go\.sum|\.nvmrc)$"
)


def gh(method, path, fields=None):
    cmd = ["gh", "api", "-X", method, path]
    for key, value in (fields or {}).items():
        cmd += ["-f", f"{key}={value}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {method} {path}: {result.stderr[:300]}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def comment_pages(repo, pr):
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/{pr}/comments?per_page=100"],
        capture_output=True, text=True, check=True,
    )
    return [comment for page in json.loads(result.stdout) for comment in page]


def eligible_head(pr, repo):
    head = pr.get("head", {})
    if (
        repo != FORK
        or pr.get("state") != "open"
        or (head.get("repo") or {}).get("full_name") != repo
        or not any(label.get("name") == "stamphog" for label in pr.get("labels", []))
        or not HEAD_RE.fullmatch(head.get("sha", ""))
    ):
        return None
    return head["sha"]


def finalized_record(comments, head):
    candidates = [
        comment["body"] for comment in comments
        if (comment.get("user") or {}).get("login") == BOT
        and isinstance(comment.get("body"), str)
        and "<!-- garnet-runtime-review -->" in comment["body"]
    ]
    if not candidates:
        return None
    body = candidates[-1]
    summary = parse_summary(body)
    if (
        COMMIT_RE.findall(body) != [head]
        or summary is None
        or summary.get("status") != "finalized"
        or summary.get("commit") != head
    ):
        return None
    return summary, body


def plain(text):
    return html.unescape(TAG_RE.sub("", text)).strip()


def parse_summary(body):
    match = SUMMARY_RE.search(body)
    if match is None:
        return None
    try:
        summary = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return summary if isinstance(summary, dict) else None


def changed_destinations(body):
    """The `+` destinations of each recorded job with the chain that reached them.

    Reads only the job folds; the "How to read this" legend is dropped first, so its
    sample tree is never mistaken for the record.
    """
    body = EXPLAINER_RE.sub("", body)
    changes = []
    for heading, fold in JOB_FOLD_RE.findall(body):
        names = [b or c for b, c in JOB_NAME_RE.findall(heading)]
        job = next((n for n in names if not re.fullmatch(r"[+\u2212\-\d&nbsp;\s]+", html.unescape(n))), names[0] if names else "recorded job")
        for fence in DIFF_FENCE_RE.findall(fold):
            rows = []
            for raw in fence.splitlines():
                if raw.startswith("@@") or not raw.strip():
                    continue
                mark, line = (raw[:1], raw[1:]) if raw[:1] in "+- " else (" ", raw)
                stripped = line.lstrip(" │├└─")
                depth = len(line) - len(stripped)
                name = plain(DEFANG_RE.sub(".", stripped)).lstrip("○→ ").strip()
                if name:
                    rows.append((mark, depth, name))
            stack = []
            for index, (mark, depth, name) in enumerate(rows):
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                has_children = index + 1 < len(rows) and rows[index + 1][1] > depth
                if mark == "+" and not has_children and "." in name:
                    changes.append({"job": plain(job), "destination": name, "chain": [p for _, p in stack]})
                stack.append((depth, name))
    return changes


def evidence_gaps(summary, head):
    gaps = []
    if summary.get("status") != "finalized":
        gaps.append(f"record status is `{summary.get('status', 'missing')}`, not finalized")
    if summary.get("commit") != head:
        gaps.append(f"record is bound to `{str(summary.get('commit', 'missing'))[:12]}`, not the head `{head[:12]}`")
    quality = summary.get("capture_quality")
    if quality != "complete":
        gaps.append(f"capture is `{quality or 'undeclared'}`, not declared complete")
    incomplete = summary.get("incomplete")
    if type(incomplete) is not int or incomplete != 0:
        gaps.append(f"{incomplete if isinstance(incomplete, int) else 'an unknown number of'} job(s) recorded incompletely")
    return gaps


def pr_files(owner, repo, pr):
    files = gh("GET", f"/repos/{owner}/{repo}/pulls/{pr}/files?per_page=100")
    return [f["filename"] for f in files]


def decide(summary, gaps, changes):
    """Garnet's outcome, relayed. A finalized, head-bound record that shows new
    destinations is CHANGED whatever its capture quality; a record that shows
    nothing new is NOTHING NEW only when the capture is declared complete.
    Everything else is UNDETERMINABLE."""
    bound = summary.get("status") == "finalized" and not any(g.startswith("record is bound") for g in gaps)
    outcome = summary.get("outcome")
    if bound and outcome == "changed" and changes:
        return "CHANGED"
    previous = summary.get("previous")
    if not gaps and outcome == "unchanged" and isinstance(previous, str) and HEAD_RE.fullmatch(previous) and previous != summary.get("commit"):
        return "NOTHING NEW"
    return "UNDETERMINABLE"


def render(summary, decision, gaps, changes, head, files):
    jobs = summary.get("jobs")
    destinations = summary.get("destinations")
    dependency_only = bool(files) and all(DEPENDENCY_FILE_RE.search(f) for f in files)
    lines = [
        MARKER,
        f"## Merge gate: **{decision}**",
        "",
        f"Head `{head[:12]}` · {jobs if isinstance(jobs, int) else 'unknown'} recorded job(s) · "
        f"{destinations if isinstance(destinations, int) else 'unknown'} destinations · "
        f"recorded {summary.get('recorded', 'time unknown')}.",
        f"Changed files: {'dependency manifests only' if dependency_only else f'{len(files)} files, not only dependency manifests'}.",
        "",
    ]
    heading = summary.get("line") or summary.get("heading")
    if heading:
        lines += [f"Garnet: {heading}", ""]
    if gaps:
        lines += ["Evidence gaps (nothing new cannot be concluded until these clear):", ""]
        lines += [f"- {gap}" for gap in gaps] + [""]
    if changes:
        lines += ["New in this commit, with the execution chain that reached it:", ""]
        for change in changes:
            chain = " → ".join(change["chain"][-3:]) or "chain not shown in the comment"
            lines.append(f"- `{change['destination']}` · {chain} · job {change['job']}")
        lines.append("")
    elif decision == "NOTHING NEW":
        lines += [f"Nothing new recorded against `{summary['previous']}`.", ""]
    lines += ["_Garnet reports; the reviewer decides._"]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ready", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("repo", choices=[FORK])
    parser.add_argument("pr", type=int)
    args = parser.parse_args()
    if args.pr <= 0:
        parser.error("PR number must be positive")
    pr_path = f"repos/{args.repo}/pulls/{args.pr}"
    pr = gh("GET", pr_path)
    head = eligible_head(pr, args.repo)
    if head is None:
        return
    record = finalized_record(comment_pages(args.repo, args.pr), head)
    if args.ready:
        if record is not None and eligible_head(gh("GET", pr_path), args.repo) == head:
            print(head)
        return
    fields = {
        "context": "garnet/runtime-evidence",
        "target_url": pr["html_url"],
    }
    decision = "UNDETERMINABLE"
    if record is not None:
        summary, body = record
        gaps = evidence_gaps(summary, head)
        changes = changed_destinations(body)
        decision = decide(summary, gaps, changes)
        owner, repo = args.repo.split("/")
        print(render(summary, decision, gaps, changes, head, pr_files(owner, repo, args.pr)))
    if eligible_head(gh("GET", pr_path), args.repo) != head:
        return
    fields["state"] = {"CHANGED": "failure", "NOTHING NEW": "success"}.get(decision, "pending")
    fields["description"] = f"{decision} for {head[:12]} (shadow; not an approval)"
    gh("POST", f"repos/{args.repo}/statuses/{head}", fields)


if __name__ == "__main__":
    main()
