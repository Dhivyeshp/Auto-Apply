import json
from pathlib import Path

import pytest
import yaml

from src import cloud

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def listing(i, loc="Austin, TX", active=True):
    return {"id": f"id-{i}", "source": "Simplify", "category": "Software", "company_name": f"Co{i}",
            "title": "Software Engineer Intern", "active": active, "is_visible": True, "terms": ["Summer 2027"],
            "date_posted": 1790000000 + i, "url": f"https://jobs.lever.co/co{i}/{i}", "locations": [loc],
            "sponsorship": "Other", "degrees": ["Bachelor's"]}


class Outbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, title, message, click):
        if self.fail:
            raise OSError("network down")
        self.sent.append((title, message, click))


def test_first_run_seeds_without_alerting(cfg, tmp_path):
    out = Outbox()
    res = cloud.check(cfg, tmp_path / "seen.json", out, listings=[listing(1), listing(2)])
    assert res.status == "seeded" and out.sent == []
    assert set(json.loads((tmp_path / "seen.json").read_text())) == {
        "id-1", "id-2", "u:jobs.lever.co/co1/1", "u:jobs.lever.co/co2/2"}


def test_old_id_only_state_reseeds_silently(cfg, tmp_path):
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps(["id-1"]))  # format from before link matching
    out = Outbox()
    assert cloud.check(cfg, seen, out, listings=[listing(1), listing(2)]).status == "seeded"
    assert out.sent == []
    out2 = Outbox()
    cloud.check(cfg, seen, out2, listings=[listing(1), listing(2), listing(3)])
    assert [t for t, _, _ in out2.sent] == ["New: Co3"]


def test_same_job_on_two_lists_alerts_once(cfg, tmp_path):
    seen = tmp_path / "seen.json"
    cloud.check(cfg, seen, Outbox(), listings=[listing(0)])
    simplify_copy = {**listing(5), "url": "https://jobs.lever.co/co5/5?utm_source=Simplify&ref=Simplify"}
    other_list_copy = {**listing(5), "id": "other-list-uuid", "url": "https://jobs.lever.co/co5/5/"}
    out = Outbox()
    cloud.check(cfg, seen, out, listings=[listing(0), simplify_copy, other_list_copy])
    assert len(out.sent) == 1
    # later, only the other list still has it: not alerted again
    out2 = Outbox()
    cloud.check(cfg, seen, out2, listings=[listing(0), other_list_copy])
    assert out2.sent == []


def test_alerts_only_new_matching_jobs(cfg, tmp_path):
    seen = tmp_path / "seen.json"
    cloud.check(cfg, seen, Outbox(), listings=[listing(1)])
    out = Outbox()
    res = cloud.check(cfg, seen, out, listings=[listing(1), listing(2), listing(3, active=False),
                                                listing(4, loc="Toronto, ON, Canada")])
    assert [l.listing_id for l in res.new] == ["id-2"]
    [(title, message, click)] = out.sent
    assert title == "New: Co2" and "Co2 — Software Engineer Intern (Austin, TX)" in message
    assert click == "https://jobs.lever.co/co2/2"
    # next run with the same feed: nothing new
    out2 = Outbox()
    cloud.check(cfg, seen, out2, listings=[listing(1), listing(2)])
    assert out2.sent == []


def test_failed_push_is_retried_next_run(cfg, tmp_path):
    seen = tmp_path / "seen.json"
    cloud.check(cfg, seen, Outbox(), listings=[listing(1)])
    res = cloud.check(cfg, seen, Outbox(fail=True), listings=[listing(1), listing(2)])
    assert res.errors
    out = Outbox()
    cloud.check(cfg, seen, out, listings=[listing(1), listing(2)])
    assert len(out.sent) == 1


def test_big_batch_is_one_summary(cfg, tmp_path):
    seen = tmp_path / "seen.json"
    cloud.check(cfg, seen, Outbox(), listings=[listing(0)])
    out = Outbox()
    cloud.check(cfg, seen, out, listings=[listing(i) for i in range(13)])
    [(title, message, click)] = out.sent
    assert title == "12 new internships" and "+2 more" in message and click is None


def test_unchanged_feed_does_nothing(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud.simplify, "fetch_listings", lambda urls, etag_file=None: None)
    out = Outbox()
    assert cloud.check(cfg, tmp_path / "seen.json", out).status == "unchanged"
    assert not (tmp_path / "seen.json").exists()


def test_env_sender_needs_a_channel(cfg, monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK", raising=False)
    cfg["notify"] = {}
    with pytest.raises(RuntimeError, match="NTFY_TOPIC"):
        cloud.env_sender(cfg)
