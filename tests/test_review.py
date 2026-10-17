import io
import json

import pytest
from rich.console import Console

from src import render, review, schemas, tracker
from src.tools import find_exe

needs_latex = pytest.mark.skipif(
    not (find_exe("tectonic") or find_exe("pdflatex")), reason="no LaTeX engine"
)

IBM1 = "Engineered an Instana synthetic monitoring engine running API health checks across 12 global AWS/IBM Cloud environments"
IBM1_NEW = "Developed an Instana synthetic monitoring engine running API health checks across 12 global AWS/IBM Cloud environments"


@pytest.fixture
def bank():
    return schemas.load_bullets()


def state():
    return review.ReviewState(kept=["ibm-1", "ibm-2", "nrve-1"], texts={"ibm-1": IBM1_NEW}, dropped=["echo-2"])


def test_diff_rows_markers(bank):
    record = {"scores": [{"id": "ibm-1", "score": 80, "reason": ""}]}
    rows = {bid: (m, score, text.plain) for m, bid, score, text in review.diff_rows(record, bank, state())}
    assert rows["ibm-1"][0] == "~" and rows["ibm-1"][1] == "80"
    assert rows["ibm-2"][0] == "+"
    assert rows["echo-2"][0] == "-" and "dropped to fit" in rows["echo-2"][2]
    assert rows["acm-2"][0] == "-" and "not selected" in rows["acm-2"][2]


def test_word_diff_marks_changes():
    t = review.word_diff("Built a thing", "Developed a thing")
    styles = {str(span.style) for span in t.spans}
    assert "red strike" in styles and "green" in styles


@pytest.mark.parametrize(
    "cmd,msg,check",
    [
        ("-ibm-2", "removed ibm-2", lambda s: "ibm-2" not in s.kept),
        ("+acm-2", "added acm-2", lambda s: s.kept[-1] == "acm-2"),
        ("r ibm-1", "reverted ibm-1", lambda s: "ibm-1" not in s.texts),
        ("+nope", "unknown id", lambda s: len(s.kept) == 3),
        ("t nrve-1 Shipped a personalized education app on a Python/Firebase backend serving 900+ beta users",
         "rejected: new numbers", lambda s: "nrve-1" not in s.texts),
        ("t nrve-1 Launched a personalized education app on a Python/Firebase backend serving 300+ beta users",
         "updated nrve-1", lambda s: s.texts["nrve-1"].startswith("Launched")),
        ("zzz", "unknown command", lambda s: True),
    ],
)
def test_apply_edit(bank, cmd, msg, check):
    s = state()
    assert review.apply_edit(cmd, s, bank).startswith(msg)
    assert check(s)


@pytest.fixture
def tailored(tmp_path, bank):
    """A tailored output folder plus a tracked job it's linked to."""
    profile = schemas.load_profile()
    kept = ["ibm-1", "ibm-2", "nrve-1", "nrve-3", "echo-1"]
    fit = render.fit_one_page(profile, bank, kept, tmp_path / "out", texts={"ibm-1": IBM1_NEW})
    conn = tracker.connect(tmp_path / "t.db")
    tracker.insert_job(conn, {"listing_id": "L1", "company": "Acme", "role": "SWE Intern",
                              "url": "https://jobs.lever.co/acme/1", "ats": "lever"})
    record = {
        "job_id": 1, "company": "Acme", "role": "SWE Intern",
        "analysis": {"must_have": ["Python"], "nice_to_have": [], "keywords": ["Python", "Kubernetes", "Rust"],
                     "role_category": "swe", "summary": "Build things."},
        "scores": [{"id": i, "score": 70, "reason": ""} for i in kept],
        "ranked": kept, "kept": fit.kept, "dropped_to_fit": [], "rewrites_accepted": {"ibm-1": IBM1_NEW},
        "ats": {"coverage_pct": 66.7, "must_have_pct": 100.0, "missing": ["Rust"], "must_have_missing": [],
                "below_threshold": False},
    }
    review.save_record(tmp_path / "out", record)
    return tmp_path / "out", profile, conn


def scripted(*answers):
    it = iter(answers)
    return lambda prompt: next(it)


def quiet():
    return Console(file=io.StringIO(), width=140)


@needs_latex
def test_approve_marks_job_drafted(tailored, bank):
    out, profile, conn = tailored
    assert review.review(out, profile, bank, conn, ask=scripted("a"), console=quiet()) == "approve"
    record = review.load_record(out)
    assert record["approved"]["sha256"] == review.sha256(out / "resume.pdf")
    job = tracker.get_job(conn, 1)
    assert job["status"] == "drafted" and job["coverage_pct"] == 66.7
    assert job["resume_path"].endswith("resume.pdf")


@needs_latex
def test_skip_marks_job_skipped(tailored, bank):
    out, profile, conn = tailored
    assert review.review(out, profile, bank, conn, ask=scripted("s"), console=quiet()) == "skip"
    assert tracker.get_job(conn, 1)["status"] == "skipped"


@needs_latex
def test_quit_leaves_job_queued(tailored, bank):
    out, profile, conn = tailored
    assert review.review(out, profile, bank, conn, ask=scripted("q"), console=quiet()) == "quit"
    assert tracker.get_job(conn, 1)["status"] == "queued"
    assert "approved" not in review.load_record(out)


@needs_latex
def test_edit_rerenders_then_approve(tailored, bank):
    out, profile, conn = tailored
    answers = scripted("e", "-echo-1", "+acm-1", "r ibm-1", "", "a")
    assert review.review(out, profile, bank, conn, ask=answers, console=quiet()) == "approve"
    record = review.load_record(out)
    assert "echo-1" not in record["kept"] and "acm-1" in record["kept"]
    assert record["rewrites_accepted"] == {}
    assert record["edited_in_review"] is True
    assert record["approved"]["sha256"] == review.sha256(out / "resume.pdf")
    assert IBM1.split()[0] in render.escape_latex((out / "resume.tex").read_text(encoding="utf-8"))
