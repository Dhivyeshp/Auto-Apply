"""Local website for AutoApply: inbox, job/resume review, apply sessions, and chat. Runs on 127.0.0.1 only."""

from __future__ import annotations

import difflib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src import review, schemas, tailor, tracker, watch
from src.apply import run
from src.web import chat
from src.web.sessions import SessionManager, TaskManager

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

TABS = {
    "new": ("New", lambda j: j["status"] == "queued" and not j.get("tailor_dir")),
    "ready": ("Ready to review", lambda j: j["status"] == "queued" and bool(j.get("tailor_dir"))),
    "approved": ("Approved", lambda j: j["status"] == "drafted"),
    "submitted": ("Submitted", lambda j: j["status"] in ("submitted", "interview", "offer", "rejected")),
    "skipped": ("Skipped", lambda j: j["status"] == "skipped"),
}


def create_app(cfg: dict, runner_factory=None) -> FastAPI:
    app = FastAPI(title="AutoApply", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    db_path = ROOT / cfg["db_path"]
    tcfg = cfg.get("tailor", {})
    tasks = TaskManager(workers=cfg.get("batch", {}).get("parallel", 3))
    sessions = SessionManager()
    app_chat_state: dict[str, Any] = {"session_id": None, "turns": [], "busy": False}
    resume_chats: dict[int, list[dict]] = {}

    def runner():
        if runner_factory:
            return runner_factory()
        return tailor.claude_runner(model=tcfg.get("model", "sonnet"))

    def conn():
        return tracker.connect(db_path)

    def job_or_404(c, job_id: int) -> dict:
        job = tracker.get_job(c, job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return job

    def unapprove_if_changed(c, job: dict, out: Path) -> None:
        record = review.load_record(out)
        if record.pop("approved", None) is not None:
            review.save_record(out, record)
        if job["status"] == "drafted":
            tracker.update_status(c, job["id"], "queued", notes="resume edited after approval")

    def record_for(job: dict) -> dict | None:
        d = job.get("tailor_dir")
        if d and (Path(d) / "tailoring.json").exists():
            return review.load_record(Path(d))
        return None

    # ---------- pages ----------

    @app.get("/", response_class=HTMLResponse)
    def inbox(request: Request, tab: str = "ready", q: str = "", ats: str = "applyable", limit: int = 150):
        c = conn()
        jobs = tracker.list_jobs(c)
        counts = {k: sum(1 for j in jobs if f(j)) for k, (_, f) in TABS.items()}
        shown = [j for j in jobs if TABS.get(tab, TABS["ready"])[1](j)]
        if ats == "applyable":
            shown = [j for j in shown if j["ats"] in run.SUPPORTED_ATS]
        if q:
            ql = q.lower()
            shown = [j for j in shown if ql in f"{j['company']} {j['role']} {' '.join(j.get('locations') or [])}".lower()]
        if tab in ("ready", "approved"):
            shown.sort(key=lambda j: (j.get("rating") or 0, j.get("date_posted") or ""), reverse=True)
        else:
            shown.sort(key=lambda j: (j.get("date_posted") or "", j["id"]), reverse=True)
        return templates.TemplateResponse(request, "inbox.html", {
            "tabs": TABS, "tab": tab, "counts": counts, "jobs": shown[:limit], "total": len(shown),
            "q": q, "ats": ats, "active": {t.job_id: t for t in tasks.active()},
        })

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: int):
        c = conn()
        job = job_or_404(c, job_id)
        record = record_for(job)
        rows = bullet_rows(record) if record else []
        return templates.TemplateResponse(request, "job.html", {
            "job": job, "record": record, "rows": rows, "task": tasks.for_job(job_id),
            "session": sessions.get(job_id), "chat": resume_chats.get(job_id, []),
            "supported": job["ats"] in run.SUPPORTED_ATS,
        })

    @app.get("/job/{job_id}/apply", response_class=HTMLResponse)
    def apply_page(request: Request, job_id: int):
        job = job_or_404(conn(), job_id)
        return templates.TemplateResponse(request, "apply.html", {"job": job})

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request):
        return templates.TemplateResponse(request, "chat.html", {"turns": app_chat_state["turns"]})

    # ---------- files ----------

    @app.get("/job/{job_id}/file/{name}")
    def job_file(job_id: int, name: str):
        job = job_or_404(conn(), job_id)
        if name not in ("resume.pdf", "form.png", "confirmation.png") or not job.get("tailor_dir"):
            raise HTTPException(404)
        path = Path(job["tailor_dir"]) / name
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    # ---------- job actions ----------

    @app.post("/api/jobs/tailor")
    def tailor_many(ids: list[int] = Body(..., embed=True)):
        c = conn()
        profile, bank = schemas.load_profile(), schemas.load_bullets()
        started = []
        for jid in ids:
            job = job_or_404(c, jid)

            def work(job=job):
                watch.tailor_job(tracker.connect(db_path), job, cfg, runner(), profile, bank)

            started.append(asdict(tasks.submit(jid, "tailor", work)))
        return {"tasks": started}

    @app.get("/api/jobs/{job_id}/task")
    def job_task(job_id: int):
        t = tasks.for_job(job_id)
        return asdict(t) if t else {"status": "none"}

    @app.post("/api/jobs/{job_id}/rate")
    def rate(job_id: int):
        job = job_or_404(conn(), job_id)
        out = Path(job["tailor_dir"])

        def work():
            record = review.load_record(out)
            r = tailor.rate(runner(), (out / "jd.txt").read_text(encoding="utf-8"), out / "resume.pdf")
            record["rating"] = r.model_dump()
            record.pop("rating_stale", None)
            review.save_record(out, record)
            tracker.set_tailored(tracker.connect(db_path), job_id, str(out), r.score)

        return asdict(tasks.submit(job_id, "rate", work))

    @app.post("/api/jobs/{job_id}/edit")
    def edit(job_id: int, keep: list[str] = Body(..., embed=True)):
        c = conn()
        job = job_or_404(c, job_id)
        out = Path(job["tailor_dir"])
        record = review.load_record(out)
        state = review.ReviewState(kept=list(keep), texts=dict(record.get("rewrites_accepted", {})))
        cov = review.rerender(out, record, schemas.load_profile(), schemas.load_bullets(), state,
                              tcfg.get("coverage_threshold", 50))
        unapprove_if_changed(c, job, out)
        return {"coverage": cov.coverage_pct, "kept": state.kept}

    @app.post("/api/jobs/{job_id}/decide")
    def decide(job_id: int, decision: str = Body(..., embed=True)):
        if decision not in ("approve", "skip"):
            raise HTTPException(400, "decision must be approve or skip")
        c = conn()
        job = job_or_404(c, job_id)
        out = Path(job["tailor_dir"])
        record = review.load_record(out)
        name = record.get("pdf_name") or tailor.resolve_pdf_name(
            tcfg.get("pdf_name"), tcfg.get("pdf_types"), record.get("analysis", {}).get("role_category"))
        review.decide(out, record, decision, c, name)
        return {"status": tracker.get_job(c, job_id)["status"]}

    @app.post("/api/jobs/{job_id}/status")
    def set_status(job_id: int, status: str = Body(..., embed=True)):
        if status not in ("queued", "skipped", "interview", "rejected", "offer"):
            raise HTTPException(400, "can't set that status here")
        c = conn()
        job_or_404(c, job_id)
        tracker.update_status(c, job_id, status)
        return {"status": status}

    @app.post("/api/jobs/{job_id}/chat")
    def resume_chat(job_id: int, message: str = Body(..., embed=True)):
        job = job_or_404(conn(), job_id)
        if not job.get("tailor_dir"):
            raise HTTPException(400, "tailor this job first")
        history = resume_chats.setdefault(job_id, [])
        res = chat.resume_chat(Path(job["tailor_dir"]), message, history, schemas.load_profile(),
                               schemas.load_bullets(), runner(), tcfg.get("coverage_threshold", 50))
        history += [{"role": "you", "text": message}, {"role": "claude", "text": res.reply}]
        if res.changed:
            unapprove_if_changed(conn(), job, Path(job["tailor_dir"]))
        return {"reply": res.reply, "changed": res.changed, "coverage": res.coverage}

    # ---------- apply sessions ----------

    @app.post("/api/jobs/{job_id}/apply/start")
    def apply_start(job_id: int):
        c = conn()
        job = job_or_404(c, job_id)
        if job["status"] != "drafted":
            raise HTTPException(400, "approve the resume first")
        s = sessions.start(job_id, db_path=db_path, profile=schemas.load_profile(), bank=schemas.load_bullets(),
                           runner=runner())
        return s.view()

    def session_or_404(job_id: int):
        s = sessions.get(job_id)
        if s is None:
            raise HTTPException(404, "no apply session")
        return s

    @app.get("/api/jobs/{job_id}/apply")
    def apply_state(job_id: int):
        s = sessions.get(job_id)
        return s.view() if s else {"state": "none"}

    @app.post("/api/jobs/{job_id}/apply/answers")
    def apply_answers(job_id: int, values: dict[str, Any] = Body(...), remember: list[str] = Body(default=[])):
        session_or_404(job_id).answer(values, remember)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/apply/field")
    def apply_field(job_id: int, name: str = Body(...), value: Any = Body(...)):
        session_or_404(job_id).set_field(name, value)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/apply/{action}")
    def apply_action(job_id: int, action: str, went_through: bool = Body(default=False, embed=True)):
        s = session_or_404(job_id)
        if action == "submit":
            if s.state != "review":
                raise HTTPException(400, f"can't submit while {s.state}")
            s.submit()
        elif action == "skip":
            s.skip()
        elif action == "close":
            s.close()
        elif action == "continue":
            s.human_done()
        elif action == "confirm":
            s.unclear_result(went_through)
        else:
            raise HTTPException(404)
        return {"ok": True}

    # ---------- app chat ----------

    @app.post("/api/chat")
    def app_chat(message: str = Body(..., embed=True)):
        if app_chat_state["busy"]:
            raise HTTPException(409, "Claude is still working on the last message")
        app_chat_state["busy"] = True
        app_chat_state["turns"].append({"role": "you", "text": message})

        def work():
            try:
                res = chat.app_chat(message, app_chat_state["session_id"], model=cfg.get("chat", {}).get("model", "sonnet"))
                app_chat_state["session_id"] = res.session_id
                app_chat_state["turns"].append({"role": "claude", "text": res.reply, "files": res.files})
            finally:
                app_chat_state["busy"] = False

        def guarded():
            try:
                work()
            except Exception as e:
                app_chat_state["turns"].append({"role": "claude", "text": f"Error: {e}", "files": []})

        tasks.pool.submit(guarded)
        return {"ok": True}

    @app.get("/api/chat")
    def app_chat_state_view():
        return {"busy": app_chat_state["busy"], "turns": app_chat_state["turns"]}

    @app.post("/api/chat/undo")
    def app_chat_undo(files: list[str] = Body(..., embed=True)):
        undone = chat.undo(files)
        app_chat_state["turns"].append({"role": "claude", "text": "Undid changes to: " + (", ".join(undone) or "nothing"),
                                        "files": []})
        return {"undone": undone}

    @app.post("/api/chat/reset")
    def app_chat_reset():
        app_chat_state.update(session_id=None, turns=[])
        return {"ok": True}

    return app


def word_diff(old: str, new: str) -> list[tuple[str, str]]:
    a, b = old.split(), new.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
        if op == "equal":
            out.append(("eq", " ".join(a[i1:i2])))
        if op in ("delete", "replace"):
            out.append(("del", " ".join(a[i1:i2])))
        if op in ("insert", "replace"):
            out.append(("ins", " ".join(b[j1:j2])))
    return out


def bullet_rows(record: dict) -> list[dict]:
    """Every bullet in bank order: kept / rewritten / left out, with score and a word diff for rewrites."""
    bank = schemas.load_bullets()
    kept = set(record.get("kept", []))
    texts = record.get("rewrites_accepted", {})
    dropped = set(record.get("dropped_to_fit", []))
    scores = {s["id"]: s for s in record.get("scores", [])}
    rows = []
    for section, entries in (("Experience", bank.experience), ("Projects", bank.projects), ("Leadership", bank.leadership)):
        for e in entries:
            for b in e.bullets:
                sc = scores.get(b.id, {})
                state = "kept" if b.id in kept else ("dropped" if b.id in dropped else "out")
                rows.append({
                    "id": b.id, "section": section, "entry": getattr(e, "org", None) or getattr(e, "name", ""),
                    "state": state, "rewritten": b.id in kept and b.id in texts and texts[b.id] != b.text,
                    "text": b.text, "diff": word_diff(b.text, texts[b.id]) if b.id in texts else None,
                    "score": sc.get("score"), "reason": sc.get("reason", ""),
                })
    return rows
