"""Background work for the website: tailoring tasks, and apply sessions that each own a Chrome window."""

from __future__ import annotations

import queue
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src import tracker
from src.apply import plan, run


# ---- tailoring and other one-shot jobs ----

@dataclass
class Task:
    id: str
    job_id: int
    kind: str
    status: str = "queued"  # queued | running | done | error
    error: str = ""
    started: str = ""


class TaskManager:
    def __init__(self, workers: int = 3):
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()

    def submit(self, job_id: int, kind: str, fn: Callable[[], Any]) -> Task:
        with self.lock:
            running = self.for_job(job_id)
            if running and running.status in ("queued", "running"):
                return running  # one task per job at a time
            task = Task(uuid.uuid4().hex[:8], job_id, kind)
            self.tasks[task.id] = task

        def work():
            task.status, task.started = "running", datetime.now().isoformat(timespec="seconds")
            try:
                fn()
                task.status = "done"
            except Exception as e:
                task.status, task.error = "error", (str(e).splitlines() or [type(e).__name__])[0][:300]

        self.pool.submit(work)
        return task

    def for_job(self, job_id: int) -> Task | None:
        mine = [t for t in self.tasks.values() if t.job_id == job_id]
        return mine[-1] if mine else None

    def active(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status in ("queued", "running")]


# ---- apply sessions ----

@dataclass
class ApplySession:
    """One application: prepare → your answers → fill in Chrome → you submit. All browser work on one thread."""
    job_id: int
    db_path: Path
    profile: Any
    bank: Any
    runner: Any
    state: str = "preparing"  # preparing | questions | filling | review | submitting | needs_human | unclear | submitted | skipped | error | closed
    message: str = ""
    prep: run.Prepared | None = None
    fills: dict[str, tuple[bool, str]] = field(default_factory=dict)
    screenshot: Path | None = None
    confirmation: Path | None = None
    _cmds: queue.Queue = field(default_factory=queue.Queue)
    _human: threading.Event = field(default_factory=threading.Event)
    _unclear_answer: queue.Queue = field(default_factory=queue.Queue)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name=f"apply-{self.job_id}").start()

    # -- commands from the website --
    def answer(self, values: dict[str, Any], remember: list[str]) -> None:
        self._cmds.put(("answers", values, remember))

    def set_field(self, name: str, value: Any) -> None:
        self._cmds.put(("field", name, value))

    def submit(self) -> None:
        self._cmds.put(("submit",))

    def skip(self) -> None:
        self._cmds.put(("skip",))

    def close(self) -> None:
        self._cmds.put(("close",))
        self._human.set()

    def human_done(self) -> None:
        self._human.set()

    def unclear_result(self, went_through: bool) -> None:
        self._unclear_answer.put(went_through)

    # -- view --
    def view(self) -> dict:
        answers = self.prep.answers if self.prep else []
        return {
            "job_id": self.job_id,
            "state": self.state,
            "message": self.message,
            "answers": [
                {"i": i, "name": a.name, "label": a.label, "type": a.type, "required": a.required,
                 "value": a.value, "source": a.source, "note": a.note, "options": a.options,
                 "filled": self.fills.get(a.name, (None, ""))[0], "fill_error": self.fills.get(a.name, (None, ""))[1]}
                for i, a in enumerate(answers) if a.source != "skip" or a.required
            ],
            "needs_answers": [a.name for a in answers if a.source == "you" and a.required and a.value in (None, "", [])],
            "screenshot": bool(self.screenshot and self.screenshot.exists()),
            "confirmation": bool(self.confirmation and self.confirmation.exists()),
            "resume": self.prep.pdf.name if self.prep else "",
        }

    # -- worker thread --
    def _run(self) -> None:
        conn = tracker.connect(self.db_path)
        try:
            job = tracker.get_job(conn, self.job_id)
            self.message = "Reading the form and drafting answers…"
            self.prep = run.prepare(job, self.profile, self.bank, self.runner)
            if any(a.source == "you" and a.required for a in self.prep.answers):
                self.state, self.message = "questions", "A few questions only you can answer."
                if not self._wait_for_answers():
                    return
            self._save_log()
            self._browser_flow(conn)
        except Exception as e:
            self.state, self.message = "error", f"{e}"
            traceback.print_exc()
        finally:
            conn.close()

    def _wait_for_answers(self) -> bool:
        while True:
            cmd = self._cmds.get()
            if cmd[0] == "close":
                self.state = "closed"
                return False
            if cmd[0] == "answers":
                _, values, remember = cmd
                self._apply_values(values, remember)
                return True

    def _apply_values(self, values: dict[str, Any], remember: list[str]) -> None:
        by_name = {a.name: a for a in self.prep.answers}
        for name, value in values.items():
            a = by_name.get(name)
            if a is None or value in (None, "", []):
                continue
            a.value, a.source, a.note = value, "you", "entered on the website"
            if name in remember:
                plan.remember(a.label, value)

    def _save_log(self) -> None:
        self.prep.log["answers"] = plan.to_json(self.prep.answers)
        run.save_apply(self.prep.out_dir, self.prep.log)

    def _browser_flow(self, conn) -> None:
        from playwright.sync_api import sync_playwright

        from src.apply import common

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                self.state, self.message = "filling", "Filling the form in Chrome…"
                self.fills, self.screenshot = run.fill(page, self.prep, self.profile)
                self.state = "review"
                self.message = "Check the form in Chrome (you can edit it there), then submit here."
                while True:
                    cmd = self._cmds.get()
                    if cmd[0] == "close":
                        self.state = "closed"
                        return
                    if cmd[0] == "skip":
                        tracker.update_status(conn, self.job_id, "skipped", notes="skipped at apply review")
                        self.prep.log["skipped_at"] = run._now()
                        run.save_apply(self.prep.out_dir, self.prep.log)
                        self.state, self.message = "skipped", "Skipped."
                        return
                    if cmd[0] == "field":
                        _, name, value = cmd
                        a = next((x for x in self.prep.answers if x.name == name), None)
                        if a is not None:
                            a.value, a.source, a.note = value, "you", "changed on the website"
                            try:
                                run._filler(self.prep.job["ats"]).fill_field(page, a)
                                self.fills[name] = (True, "")
                            except Exception as e:
                                self.fills[name] = (False, common.first_line(e))
                            self.screenshot = common.screenshot(page, self.prep.out_dir / "form.png")
                            self._save_log()
                        continue
                    if cmd[0] == "submit":
                        self.state, self.message = "submitting", "Submitting…"
                        outcome = run.submit_and_log(page, self.prep, conn, self._wait_for_human, self._ask_unclear)
                        self.confirmation = self.prep.out_dir / "confirmation.png"
                        if outcome == "submitted":
                            self.state, self.message = "submitted", "Submitted and logged."
                        else:
                            self.state, self.message = "review", "Not marked as submitted; the job stays approved."
                            continue
                        return
            finally:
                browser.close()

    def _wait_for_human(self, msg: str) -> None:
        self.state, self.message = "needs_human", msg
        self._human.clear()
        self._human.wait()
        self.state, self.message = "submitting", "Continuing…"

    def _ask_unclear(self, conf: Path) -> bool:
        self.confirmation = conf
        self.state = "unclear"
        self.message = "Didn't see a confirmation message. Did the application go through? Check Chrome."
        return self._unclear_answer.get()


class SessionManager:
    def __init__(self):
        self.sessions: dict[int, ApplySession] = {}

    def get(self, job_id: int) -> ApplySession | None:
        return self.sessions.get(job_id)

    def start(self, job_id: int, **kw) -> ApplySession:
        cur = self.sessions.get(job_id)
        if cur and cur.state not in ("submitted", "skipped", "error", "closed"):
            return cur
        s = ApplySession(job_id=job_id, **kw)
        self.sessions[job_id] = s
        s.start()
        return s
