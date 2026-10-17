import io
import json
import sqlite3
import urllib.error
from pathlib import Path

import pytest
import yaml

from src import notify, schemas, tracker, watch
from src.discover import simplify

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    c = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    c["watch"] = {"auto_tailor": True, "ats": ["greenhouse", "lever", "ashby"],
                  "max_tailor_per_run": 2, "max_tailor_per_day": 20}
    return c


@pytest.fixture
def conn(tmp_path):
    return tracker.connect(tmp_path / "t.db")


def listing(i, ats_url="https://boards.greenhouse.io/acme/jobs/{i}", loc="Austin, TX", posted=1790000000):
    return {"id": f"id-{i}", "source": "Simplify", "category": "Software", "company_name": f"Co{i}",
            "title": "Software Engineer Intern", "active": True, "is_visible": True, "terms": ["Summer 2027"],
            "date_posted": posted + i, "url": ats_url.format(i=i), "locations": [loc],
            "sponsorship": "Other", "degrees": ["Bachelor's"]}


# ---- fetch with ETag ----

class FakeResp(io.BytesIO):
    def __init__(self, body, etag):
        super().__init__(json.dumps(body).encode())
        self.headers = {"ETag": etag}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_fetch_uses_etag_and_reports_unchanged(monkeypatch, tmp_path):
    seen_headers = []

    def fake_urlopen(req, timeout):
        seen_headers.append(dict(req.header_items()))
        if req.get_header("If-none-match") == '"v1"':
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
        return FakeResp([listing(1)], '"v1"')

    monkeypatch.setattr(simplify.urllib.request, "urlopen", fake_urlopen)
    etag_file = tmp_path / "etag.json"
    assert len(simplify.fetch_listings(["https://x/listings.json"], etag_file=etag_file)) == 1
    assert simplify.fetch_listings(["https://x/listings.json"], etag_file=etag_file) is None
    assert "If-none-match" not in seen_headers[0] and seen_headers[1]["If-none-match"] == '"v1"'


# ---- tracker migration ----

def test_old_database_gains_new_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, listing_id TEXT UNIQUE, company TEXT NOT NULL, "
                "role TEXT NOT NULL, url TEXT NOT NULL UNIQUE, discovered_at TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'queued', preferred INTEGER NOT NULL DEFAULT 0)")
    old.commit()
    old.close()
    conn = tracker.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"tailor_dir", "tailored_at", "rating"} <= cols


# ---- run_watch ----

@pytest.fixture
def fake_tailor(monkeypatch):
    calls = []

    def fake(conn, job, cfg, runner, profile, bank):
        calls.append(job["id"])
        if job["company"] == "Co3":
            raise RuntimeError("posting closed")
        tracker.set_tailored(conn, job["id"], f"/out/{job['id']}", 8)

    monkeypatch.setattr(watch, "tailor_job", fake)
    return calls


def run(cfg, conn, listings, notes):
    return watch.run_watch(cfg, conn, schemas.load_profile(), schemas.load_bullets(), runner=object(),
                           notify=lambda t, lines: notes.append((t, lines)) or [], listings=listings)


def test_new_jobs_are_tailored_within_cap_and_alerted(cfg, conn, fake_tailor):
    notes = []
    jobs = [listing(1), listing(2, loc="Seattle, WA"), listing(3),
            listing(4, ats_url="https://acme.wd5.myworkdayjobs.com/x/job/{i}")]
    res = run(cfg, conn, jobs, notes)
    assert len(res.new_jobs) == 4
    # Workday is never pre-tailored; preferred (TX) first; cap of 2 per run
    assert len(fake_tailor) == 2 and all(conn.execute("SELECT ats FROM jobs WHERE id=?", (i,)).fetchone()[0]
                                         != "workday" for i in fake_tailor)
    title, lines = notes[0]
    assert title.startswith("4 new internships")
    assert lines[-1] == "Open: python main.py inbox"


def test_tailor_failure_still_alerts(cfg, conn, fake_tailor):
    cfg["watch"]["max_tailor_per_run"] = 5
    notes = []
    res = run(cfg, conn, [listing(1), listing(3)], notes)
    assert notes and any("posting closed" in e for e in res.errors)
    assert 1 in res.tailored and 3 not in res.tailored


def test_no_new_jobs_no_alert(cfg, conn, fake_tailor):
    notes = []
    run(cfg, conn, [listing(1)], notes)
    notes.clear()
    res = run(cfg, conn, [listing(1)], notes)  # same feed again
    assert not res.new_jobs and not notes


def test_daily_cap(cfg, conn, fake_tailor):
    cfg["watch"].update(max_tailor_per_run=10, max_tailor_per_day=1)
    run(cfg, conn, [listing(1), listing(2)], [])
    assert len(fake_tailor) == 1


def test_alert_text_orders_by_rating():
    jobs = [{"company": "A", "role": "SWE", "preferred": 0, "rating": None},
            {"company": "B", "role": "ML", "preferred": 1, "rating": 6},
            {"company": "C", "role": "SWE", "preferred": 0, "rating": 9}]
    title, lines = watch.alert_text(jobs)
    assert title == "3 new internships · 2 ready to apply"
    assert lines[0] == "C — SWE  9/10 ready" and lines[1].startswith("★ B") and lines[2] == "A — SWE"


def test_lock_blocks_overlap(tmp_path):
    lock = tmp_path / "w.lock"
    with watch.Lock(lock) as first:
        assert first
        with watch.Lock(lock) as second:
            assert not second
    assert not lock.exists()


def test_toast_xml_escapes():
    xml = notify.toast_xml("2 new <jobs>", ["AT&T — SWE", "x", "dropped"])
    assert "&lt;jobs&gt;" in xml and "AT&amp;T" in xml and "dropped" not in xml


def test_send_reports_errors_instead_of_raising(monkeypatch):
    monkeypatch.setattr(notify, "toast", lambda t, l: (_ for _ in ()).throw(OSError("no toast")))
    errors = notify.send({"notify": {"toast": True}}, "t", ["l"])
    assert errors == ["toast: no toast"]
