import json
import runpy
from pathlib import Path

import gate

HEAD = "a" * 40
BASE = "b" * 40
PUBLISH = Path(__file__).resolve().parents[1] / "scripts" / "stamphog-publish.py"
PUBLISHER = runpy.run_path(str(PUBLISH))


def record(head=HEAD, **changes):
    summary = {
        "commit": head, "status": "finalized", "outcome": "unchanged",
        "capture_quality": "complete", "incomplete": 0, "previous": BASE,
    } | changes
    return {
        "user": {"login": gate.BOT},
        "body": f"<!-- garnet-runtime-review -->\n<!-- garnet:commit {head} -->\n"
                f"<!-- garnet:summary {json.dumps(summary)} -->",
    }


def pull():
    return {"state": "open", "draft": True, "user": {"type": "Bot"},
            "labels": [{"name": "stamphog"}],
            "head": {"sha": HEAD, "repo": {"full_name": gate.FORK}}}


def test_admission_allows_draft_bot_but_requires_opt_in_and_internal_head():
    pr = pull()
    assert gate.eligible_head(pr, gate.FORK) == HEAD
    assert gate.eligible_head(pr | {"labels": []}, gate.FORK) is None
    pr["head"]["repo"]["full_name"] = "other/twenty"
    assert gate.eligible_head(pr, gate.FORK) is None


def test_finalized_record_rejects_stale_pending_spoofed_and_ambiguous_head():
    assert gate.finalized_record([record()], HEAD)[0]["commit"] == HEAD
    assert gate.finalized_record([record(), record(BASE)], HEAD) is None
    assert gate.finalized_record([record(status="pending")], HEAD) is None
    assert gate.finalized_record([record() | {"user": {"login": "other[bot]"}}], HEAD) is None
    item = record()
    item["body"] += f"<!-- garnet:commit {BASE} -->"
    assert gate.finalized_record([item], HEAD) is None


def test_receipt_does_not_promote_incomplete_or_unpaired_observation():
    for item in (record(capture_quality="best-effort"), record(incomplete=1), record(previous=None)):
        summary, _ = gate.finalized_record([item], HEAD)
        assert gate.decide(summary, gate.evidence_gaps(summary, HEAD), []) == "UNDETERMINABLE"
    summary, _ = gate.finalized_record([record()], HEAD)
    assert gate.decide(summary, gate.evidence_gaps(summary, HEAD), []) == "NOTHING NEW"


def test_sticky_receipt_matches_harness_shape_and_rechecks_head(monkeypatch):
    result = {"repo": gate.FORK, "pr_number": 1, "head_sha": HEAD, "final_verdict": "DRY-RUN"}
    sticky = PUBLISHER["render_receipt"](result, HEAD)
    assert sticky.splitlines()[:2] == [
        "<!-- stamphog-sticky-review -->", f"StampHog reviewed `{HEAD}` — verdict: **DRY_RUN**",
    ]
    calls = []
    pulls = iter([pull(), pull() | {"head": {"sha": BASE, "repo": {"full_name": gate.FORK}}}])
    adapter = PUBLISHER["GATE"]
    monkeypatch.setattr(adapter, "comment_pages", lambda *args: [record()])

    def gh(method, *args):
        calls.append(method)
        return next(pulls)

    monkeypatch.setattr(adapter, "gh", gh)
    PUBLISHER["publish"](result, gate.FORK, 1, HEAD)
    assert calls == ["GET", "GET"]
