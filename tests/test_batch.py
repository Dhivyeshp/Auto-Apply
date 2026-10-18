import io
import threading
import time
from pathlib import Path

import pytest
from rich.console import Console

from src import batch, review, tracker, watch
from src.apply import run


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    conn = tracker.connect(path)
    for i, (ats, status) in enumerate([("greenhouse", "queued"), ("lever", "queued"), ("ashby", "queued"),
                                       ("workday", "queued"), ("greenhouse", "drafted"),
                                       ("greenhouse", "submitted")], 1):
        tracker.insert_job(conn, {"listing_id": f"L{i}", "company": f"Co{i}", "role": "SWE Intern",
                                  "url": f"https://x/{i}", "ats": ats, "status": status, "preferred": 0})
    return path, conn


def test_select_jobs_filters_unsupported_and_closed(db):
    _, conn = db
    inbox = tracker.list_jobs(conn)
    picked = batch.select_jobs(conn, inbox, None, top=10)
    assert {j["company"] for j in picked} == {"Co1", "Co2", "Co3", "Co5"}  # no Workday, no submitted
    assert [j["id"] for j in batch.select_jobs(conn, inbox, [4, 2, 6], top=1)] == [2]
    assert len(batch.select_jobs(conn, inbox, None, top=2)) == 2


def test_tailor_parallel_runs_concurrently_and_reports_errors(db):
    path, conn = db
    jobs = [tracker.get_job(conn, i) for i in (1, 2, 3)]
    active, peak, lock = [0], [0], threading.Lock()

    def tailor_one(c, job):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with lock:
            active[0] -= 1
        if job["company"] == "Co2":
            raise RuntimeError("posting closed")
        tracker.set_tailored(c, job["id"], "/out", 8)

    done = []
    errors = batch.tailor_parallel(jobs, path, tailor_one, workers=3, on_done=lambda j, e: done.append((j["id"], e)))
    assert peak[0] == 3  # all three at once
    assert errors == {2: "posting closed"} and len(done) == 3
    assert tracker.get_job(conn, 1)["rating"] == 8  # each worker's own connection wrote through


def test_prepare_parallel_keeps_order(db):
    _, conn = db
    jobs = [tracker.get_job(conn, i) for i in (3, 1, 2)]

    def prep(job):
        time.sleep(0.05 * job["id"])
        if job["id"] == 2:
            raise ValueError("form gone")
        return f"prep-{job['id']}"

    preps, errors = batch.prepare_parallel(jobs, prep, workers=3)
    assert preps == ["prep-3", "prep-1"] and errors == {2: "form gone"}


def test_run_batch_flow(db, monkeypatch, tmp_path):
    """Tailor → review (best first, cutoff) → prepare → fill tabs → submit, with every external step faked."""
    path, conn = db
    tailored = []

    def fake_tailor(c, job, cfg, runner, profile, bank):
        d = tmp_path / f"out{job['id']}"
        d.mkdir()
        (d / "tailoring.json").write_text("{}")
        tracker.set_tailored(c, job["id"], str(d), {1: 9, 2: 5, 3: 8}[job["id"]])
        tailored.append(job["id"])

    reviewed = []

    def fake_review(out_dir, profile, bank, c, **kw):
        jid = int(Path(out_dir).name[3:])
        reviewed.append(jid)
        if jid == 3:
            return "skip"
        tracker.update_status(c, jid, "drafted", resume_path=str(Path(out_dir) / "resume.pdf"))
        return "approve"

    prepared, filled, submitted = [], [], []
    monkeypatch.setattr(watch, "tailor_job", fake_tailor)
    monkeypatch.setattr(review, "review", fake_review)
    monkeypatch.setattr(run, "prepare", lambda job, *a: prepared.append(job["id"]) or
                        run.Prepared(job, tmp_path / "r.pdf", tmp_path, [], {}))
    monkeypatch.setattr(run, "ask_human", lambda answers, ask, console: None)
    monkeypatch.setattr(run, "save_apply", lambda out_dir, log: None)
    monkeypatch.setattr(run, "fill", lambda page, prep, profile: filled.append(prep.job["id"]) or ({}, tmp_path / "f.png"))

    def fake_submit(page, prep, fills, shot, c, ask, console):
        submitted.append(prep.job["id"])
        return "submitted"

    monkeypatch.setattr(run, "review_and_submit", fake_submit)

    class FakePage:
        def bring_to_front(self): pass
        def close(self): pass

    class FakeBrowser:
        def new_page(self, **kw): return FakePage()

    jobs = [tracker.get_job(conn, i) for i in (1, 2, 3, 5)]
    console = Console(file=io.StringIO())
    res = batch.run_batch(conn, path, jobs, {"tailor": {}}, None, None, runner=None, console=console,
                          workers=3, min_rating=7, open_browser=lambda fn: fn(FakeBrowser()))
    assert sorted(tailored) == [1, 2, 3]       # job 5 was already approved
    assert reviewed == [1, 3]                   # best first; job 2 (5/10) below the 7 cutoff
    assert res.below_cutoff == [2]
    assert sorted(prepared) == [1, 5] and sorted(filled) == [1, 5] and sorted(submitted) == [1, 5]
    assert res.outcomes[3] == "skipped" and res.outcomes[1] == "submitted"
