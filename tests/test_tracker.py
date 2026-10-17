import pytest

from src import tracker


@pytest.fixture
def conn(tmp_path):
    return tracker.connect(tmp_path / "test.db")


def job(**over):
    j = {
        "listing_id": "id-1",
        "company": "Acme",
        "role": "SWE Intern",
        "url": "https://jobs.lever.co/acme/1",
        "ats": "lever",
        "terms": ["Summer 2027"],
        "locations": ["Austin, TX"],
        "degrees": [],
        "preferred": 1,
        "raw_json": {"id": "id-1"},
    }
    j.update(over)
    return j


def test_insert_and_list(conn):
    assert tracker.insert_job(conn, job())
    rows = tracker.list_jobs(conn, status="queued")
    assert len(rows) == 1
    assert rows[0]["terms"] == ["Summer 2027"]
    assert rows[0]["status"] == "queued"


def test_dedupe_by_listing_id(conn):
    assert tracker.insert_job(conn, job())
    assert not tracker.insert_job(conn, job(url="https://jobs.lever.co/acme/other"))


def test_dedupe_by_url(conn):
    assert tracker.insert_job(conn, job())
    assert not tracker.insert_job(conn, job(listing_id="id-2"))
    assert len(tracker.list_jobs(conn)) == 1


def test_update_status(conn):
    tracker.insert_job(conn, job())
    jid = tracker.list_jobs(conn)[0]["id"]
    tracker.update_status(conn, jid, "skipped", notes="not a fit")
    row = tracker.list_jobs(conn)[0]
    assert row["status"] == "skipped" and row["notes"] == "not a fit"
    with pytest.raises(ValueError):
        tracker.update_status(conn, jid, "bogus")
    with pytest.raises(ValueError):
        tracker.update_status(conn, jid, "skipped", url="x")
