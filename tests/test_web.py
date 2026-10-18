import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import render, review, schemas, tracker
from src.apply import plan, run
from src.tools import find_exe
from src.web import app as webapp
from src.web import chat, sessions

needs_latex = pytest.mark.skipif(not (find_exe("tectonic") or find_exe("pdflatex")), reason="no LaTeX engine")

ECHO1 = "Architected a FastAPI orchestration backend on PostgreSQL and Redis running concurrent multi-agent workflows"


class FakeClaude:
    def __init__(self, edit=None):
        self.edit = edit or {"reply": "ok", "selected": None, "rewrites": []}

    def __call__(self, prompt, stdin_text, schema):
        if "You edit a candidate's one-page" in prompt:
            return self.edit
        if "technical recruiter" in prompt:
            return {"score": 8, "verdict": "good", "strengths": [], "gaps": []}
        raise AssertionError(prompt[:50])


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "t.db"
    conn = tracker.connect(db)
    profile, bank = schemas.load_profile(), schemas.load_bullets()
    jobs = {}
    for i, (company, ats) in enumerate([("Acme", "lever"), ("Globex", "greenhouse"), ("Initech", "workday")], 1):
        tracker.insert_job(conn, {"listing_id": f"L{i}", "company": company, "role": "SWE Intern",
                                  "url": f"https://jobs.lever.co/{company.lower()}/{i}", "ats": ats,
                                  "locations": ["Austin, TX"], "preferred": 0})
        jobs[company] = tracker.get_job_by_listing(conn, f"L{i}")["id"]
    return {"db": db, "conn": conn, "tmp": tmp_path, "jobs": jobs, "profile": profile, "bank": bank}


def tailored(env, company="Acme"):
    out = env["tmp"] / f"out_{company}"
    kept = ["ibm-1", "ibm-2", "nrve-1", "echo-1"]
    fit = render.fit_one_page(env["profile"], env["bank"], kept, out)
    review.save_record(out, {
        "job_id": env["jobs"][company], "company": company, "role": "SWE Intern", "pdf_name": "Dhivyesh_Resume_SWE",
        "analysis": {"must_have": ["Python"], "keywords": ["Python", "Redis"], "role_category": "swe",
                     "summary": "Build things.", "nice_to_have": []},
        "scores": [{"id": i, "score": 70, "reason": "fit"} for i in kept], "ranked": kept, "kept": fit.kept,
        "dropped_to_fit": [], "rewrites_accepted": {},
        "ats": {"coverage_pct": 100.0, "must_have_pct": 100.0, "missing": [], "must_have_missing": [],
                "below_threshold": False},
        "rating": {"score": 8, "verdict": "Solid.", "strengths": ["IBM"], "gaps": ["No Rust"]},
        "attempts": [{"round": 0, "score": 8}], "best_round": 0,
    })
    tracker.set_tailored(env["conn"], env["jobs"][company], str(out), 8)
    return out


@pytest.fixture
def client(env):
    cfg = {"db_path": str(env["db"]), "tailor": {"coverage_threshold": 50}, "batch": {"parallel": 2}}
    env["claude"] = FakeClaude()
    return TestClient(webapp.create_app(cfg, runner_factory=lambda: env["claude"]))


def test_inbox_tabs_and_search(client, env):
    r = client.get("/?tab=new")
    assert r.status_code == 200 and "Acme" in r.text and "Globex" in r.text
    assert "Initech" not in r.text  # Workday hidden unless "all sites"
    assert "Initech" in client.get("/?tab=new&ats=all").text
    assert "Globex" not in client.get("/?tab=new&q=acme").text


@needs_latex
def test_job_page_and_decisions(client, env):
    out = tailored(env)
    jid = env["jobs"]["Acme"]
    page = client.get(f"/job/{jid}").text
    assert "Solid." in page and "No Rust" in page and "Approve resume" in page and "ibm-1" in page
    assert client.get(f"/job/{jid}/file/resume.pdf").status_code == 200
    assert client.get(f"/job/{jid}/file/../../data/profile.json").status_code == 404
    assert client.post(f"/api/jobs/{jid}/decide", json={"decision": "approve"}).json()["status"] == "drafted"
    assert (out / "Dhivyesh_Resume_SWE.pdf").exists()
    assert "Apply →" in client.get(f"/job/{jid}").text
    # editing an approved resume sends it back for approval
    r = client.post(f"/api/jobs/{jid}/edit", json={"keep": ["ibm-1", "echo-1"]})
    assert r.status_code == 200
    assert tracker.get_job(env["conn"], jid)["status"] == "queued"
    assert "approved" not in review.load_record(out)
    assert review.load_record(out)["kept"] == ["ibm-1", "echo-1"]


@needs_latex
def test_resume_chat_applies_real_edits_and_blocks_invention(client, env):
    out = tailored(env)
    jid = env["jobs"]["Acme"]
    env["claude"].edit = {
        "reply": "Moved Echo Agents first and dropped NRVE.",
        "selected": ["echo-1", "ibm-1", "ibm-2"],
        "rewrites": [{"id": "echo-1", "text": ECHO1 + " for 10,000 users on Kubernetes"}],
    }
    r = client.post(f"/api/jobs/{jid}/chat", json={"message": "lead with agents, drop NRVE"}).json()
    assert r["changed"] and "Kept original wording for echo-1" in r["reply"]
    record = review.load_record(out)
    assert record["kept"][0] == "echo-1" and "nrve-1" not in record["kept"]
    assert record["rewrites_accepted"] == {}  # the invented rewrite never made it in
    assert record["rating_stale"] is True


def test_apply_requires_approval(client, env):
    r = client.post(f"/api/jobs/{env['jobs']['Acme']}/apply/start")
    assert r.status_code == 400


def test_apply_session_flow(env, monkeypatch, tmp_path):
    """questions → answers (+remember) → fill → change a field → submit → CAPTCHA hand-off → submitted."""
    job = tracker.get_job(env["conn"], env["jobs"]["Acme"])
    answers = [plan.Answer("q_gpa", "GPA", "select", True, None, "you", options=["3.5-4.0", "3.0-3.4"]),
               plan.Answer("q_why", "Why us?", "textarea", True, "Because.", "claude")]
    prep = run.Prepared(job, tmp_path / "r.pdf", tmp_path, answers, {"resume_sha256": "x"})
    remembered, refilled = [], []
    monkeypatch.setattr(run, "prepare", lambda *a: prep)
    monkeypatch.setattr(run, "save_apply", lambda *a: None)
    monkeypatch.setattr(plan, "remember", lambda label, value: remembered.append((label, value)))
    monkeypatch.setattr(run, "fill", lambda page, p, profile: ({"q_gpa": (True, ""), "q_why": (True, "")}, tmp_path / "f.png"))

    class Filler:
        @staticmethod
        def fill_field(page, a):
            refilled.append((a.name, a.value))

    monkeypatch.setattr(run, "_filler", lambda ats: Filler)
    monkeypatch.setattr("src.apply.common.screenshot", lambda page, path: path)

    def fake_submit(page, p, conn, wait_for_human, confirm_unclear):
        wait_for_human("Solve the CAPTCHA")
        tracker.update_status(conn, p.job["id"], "submitted")
        return "submitted"

    monkeypatch.setattr(run, "submit_and_log", fake_submit)

    class FakePW:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        class chromium:
            @staticmethod
            def launch(headless):
                class B:
                    def new_page(self, **kw): return object()
                    def close(self): pass
                return B()

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePW())

    s = sessions.ApplySession(job_id=job["id"], db_path=env["db"], profile=env["profile"], bank=env["bank"], runner=None)
    s.start()

    def wait(state, timeout=5):
        end = time.time() + timeout
        while s.state != state and time.time() < end:
            time.sleep(0.02)
        assert s.state == state, (s.state, s.message)

    wait("questions")
    assert s.view()["needs_answers"] == ["q_gpa"]
    s.answer({"q_gpa": "3.5-4.0"}, remember=["q_gpa"])
    wait("review")
    assert remembered == [("GPA", "3.5-4.0")]
    s.set_field("q_why", "I like Acme.")
    end = time.time() + 5
    while not refilled and time.time() < end:
        time.sleep(0.02)
    assert refilled == [("q_why", "I like Acme.")]
    s.submit()
    wait("needs_human")
    s.human_done()
    wait("submitted")
    assert tracker.get_job(env["conn"], job["id"])["status"] == "submitted"


def test_app_chat_endpoints(client, monkeypatch):
    calls = []

    def fake(message, session_id=None, model="sonnet"):
        calls.append((message, session_id))
        return chat.AppChatResult("Fixed it.", "sess-1", ["src/x.py"])

    monkeypatch.setattr(chat, "app_chat", fake)
    assert client.post("/api/chat", json={"message": "fix the filter"}).status_code == 200
    end = time.time() + 5
    while client.get("/api/chat").json()["busy"] and time.time() < end:
        time.sleep(0.02)
    turns = client.get("/api/chat").json()["turns"]
    assert turns[-1] == {"role": "claude", "text": "Fixed it.", "files": ["src/x.py"]}
    client.post("/api/chat", json={"message": "and another"})
    end = time.time() + 5
    while client.get("/api/chat").json()["busy"] and time.time() < end:
        time.sleep(0.02)
    assert calls[1] == ("and another", "sess-1")  # continues the same Claude session
    monkeypatch.setattr(chat, "undo", lambda files: files)
    assert client.post("/api/chat/undo", json={"files": ["src/x.py"]}).json()["undone"] == ["src/x.py"]
    assert "Fix the app" in client.get("/chat").text


def test_undo_only_touches_project_files(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "ROOT", tmp_path)
    monkeypatch.setattr(chat, "_git", lambda *a: "tracked.py\n" if a[0] == "ls-files" else "")
    (tmp_path / "new_file.py").write_text("x")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("keep me")
    undone = chat.undo(["new_file.py", "tracked.py", "../outside.txt"])
    assert undone == ["new_file.py", "tracked.py"]
    assert not (tmp_path / "new_file.py").exists() and outside.exists()
