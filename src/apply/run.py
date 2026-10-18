"""Apply to one tracked job: plan answers → fill form → human review → submit → log."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table
from rich.text import Text

from src import ingest, tracker
from src.apply import plan
from src.review import load_record, sha256
from src.schemas import BulletBank, Profile

SOURCE_STYLE = {"profile": "green", "rule": "green", "saved": "cyan", "claude": "yellow", "you": "red", "skip": "dim"}


class ApplyError(RuntimeError):
    pass


def approved_resume(job: dict) -> tuple[Path, dict]:
    """The approved PDF for a drafted job, verified against the hash taken at approval."""
    if job["status"] != "drafted" or not job.get("resume_path"):
        raise ApplyError(f"job {job['id']} is {job['status']!r}; tailor it and approve the resume in review first")
    pdf = Path(job["resume_path"])
    record = load_record(pdf.parent)
    approved = record.get("approved") or {}
    if not pdf.exists() or approved.get("sha256") != sha256(pdf):
        raise ApplyError(f"{pdf} isn't the approved version anymore; run review again")
    # upload the named copy (e.g. Dhivyesh_Resume_SWE.pdf) only if it's byte-identical to what was approved
    upload = Path(approved.get("upload_path") or pdf)
    if not upload.exists() or sha256(upload) != approved["sha256"]:
        upload = pdf
    return upload, record


def ask_human(answers: list[plan.Answer], ask: Callable[[str], str], console: Console) -> None:
    """Prompt for required answers nothing else could fill; optionally remember them."""
    for a in answers:
        if not (a.source == "you" and a.required):
            continue
        console.print(f"\n[bold]{a.label}[/bold]" + (f"  [dim]({a.note})[/dim]" if a.note else ""))
        for i, o in enumerate(a.options, 1):
            console.print(f"  {i}. {o}")
        hint = "number(s), comma-separated" if a.type == "multiselect" else "number" if a.options else "answer"
        raw = ask(f"{hint} (blank = fill it yourself in the browser): ").strip()
        if not raw:
            continue
        if a.options:
            try:
                picks = [a.options[int(x) - 1] for x in raw.split(",")]
            except (ValueError, IndexError):
                console.print("[red]not a valid choice, leaving blank[/red]")
                continue
            a.value = picks if a.type == "multiselect" else picks[0]
        else:
            a.value = raw
        a.source, a.note = "you", "entered in terminal"
        if ask("remember this answer for this exact question? [y/N] ").strip().lower() == "y":
            plan.remember(a.label, a.value)


def show_plan(console: Console, answers: list[plan.Answer], fills: dict[str, tuple[bool, str]]) -> None:
    table = Table(expand=True, show_lines=False)
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("question", ratio=2)
    table.add_column("answer", ratio=3)
    table.add_column("from", width=7)
    table.add_column("filled", width=24)
    for i, a in enumerate(answers, 1):
        if a.source == "skip":
            continue
        value = ", ".join(a.value) if isinstance(a.value, list) else (a.value or "")
        if a.type != "textarea" and len(value) > 80:
            value = value[:77] + "…"
        ok, err = fills.get(a.name, (False, "not filled"))
        status = Text("✓", style="green") if ok else Text(f"✗ {err[:22]}", style="red")
        label = ("* " if a.required else "  ") + a.label
        table.add_row(str(i), label[:120], value, Text(a.source, style=SOURCE_STYLE[a.source]), status)
    console.print(table)
    drafted = [a for a in answers if a.source == "claude"]
    if drafted:
        console.print("[yellow]Claude drafted the yellow rows. Read them before submitting.[/yellow]")


def save_apply(out_dir: Path, data: dict) -> None:
    (out_dir / "apply.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


SUPPORTED_ATS = ("greenhouse", "lever", "ashby")


def _filler(ats: str):
    from src.apply import ashby, greenhouse, lever

    return {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}[ats]


@dataclass
class Prepared:
    """Everything needed to fill one application, gathered before any browser opens."""
    job: dict
    pdf: Path
    out_dir: Path
    answers: list[plan.Answer]
    log: dict = field(default_factory=dict)


def prepare(job: dict, profile: Profile, bank: BulletBank, runner) -> Prepared:
    """Check the approved resume, read the form, and plan answers (Claude drafts what it can).

    No prompts and no shared DB connection, so batch mode can run several of these in parallel.
    """
    if job["ats"] not in SUPPORTED_ATS:
        raise ApplyError(f"{job['ats']!r} apply isn't built (Greenhouse, Lever and Ashby are)")
    pdf, record = approved_resume(job)
    posting = ingest.fetch_posting(job["url"])
    answers = plan.resolve(posting.questions, profile, pdf)
    if runner is not None and any(a.source == "you" and a.required for a in answers):
        answers = plan.draft_unresolved(answers, posting.jd_text, job["company"],
                                        plan.facts_for_claude(profile, bank), runner)
    log = {"job_id": job["id"], "url": job["url"], "resume_sha256": record["approved"]["sha256"],
           "answers": plan.to_json(answers)}
    return Prepared(job, pdf, pdf.parent, answers, log)


def fill(page, prep: Prepared, profile: Profile) -> tuple[dict[str, tuple[bool, str]], Path]:
    from src.apply import common

    results = _filler(prep.job["ats"]).fill_form(page, prep.answers, extra=plan.page_extras(profile, prep.job["ats"]),
                                                 url=prep.job["url"])
    return {r.name: (r.ok, r.error) for r in results}, common.screenshot(page, prep.out_dir / "form.png")


def review_and_submit(page, prep: Prepared, fills: dict, shot: Path, conn: sqlite3.Connection,
                      ask: Callable[[str], str], console: Console) -> str:
    """The human gate for one filled form. Returns 'submitted', 'skipped' or 'quit'."""
    from src.apply import common

    job, answers, log, out_dir = prep.job, prep.answers, prep.log, prep.out_dir
    while True:
        console.rule(f"[bold]{job['company']} — {job['role']}[/bold]")
        show_plan(console, answers, fills)
        console.print(f"resume: {prep.pdf}\nform screenshot: {shot}")
        console.print("You can also fix anything directly in the browser window.\n"
                      "[bold]submit[/bold] submit   [bold]e N[/bold] change answer N   "
                      "[bold]s[/bold] skip this job   [bold]q[/bold] quit (decide later)")
        choice = ask("> ").strip()
        if choice == "submit":
            break
        if choice.lower() == "s":
            tracker.update_status(conn, job["id"], "skipped", notes="skipped at apply review")
            log["skipped_at"] = _now()
            save_apply(out_dir, log)
            return "skipped"
        if choice.lower() == "q":
            return "quit"
        if choice.lower().startswith("e "):
            try:
                a = answers[int(choice.split()[1]) - 1]
            except (ValueError, IndexError):
                console.print("[red]no such row[/red]")
                continue
            a.source = "you"
            a.required = True  # so ask_human prompts for it
            ask_human([a], ask, console)
            if a.value not in (None, "", []):
                try:
                    _filler(job["ats"]).fill_field(page, a)
                    fills[a.name] = (True, "")
                except Exception as e:
                    fills[a.name] = (False, common.first_line(e))
                shot = common.screenshot(page, out_dir / "form.png")
            log["answers"] = plan.to_json(answers)
            save_apply(out_dir, log)
            continue
        console.print(f"[red]unknown choice {choice!r}[/red] (type the word submit to submit)")

    confirmed = common.submit(page, wait_for_human=lambda msg: ask(msg + " "))
    conf = common.screenshot(page, out_dir / "confirmation.png")
    if not confirmed:
        console.print(f"[yellow]Didn't see a confirmation message. Check the browser and {conf}.[/yellow]")
        confirmed = ask("Did the application go through? [y/N] ").strip().lower() == "y"
    if not confirmed:
        log["submit_unconfirmed_at"] = _now()
        save_apply(out_dir, log)
        console.print("[red]Not marked as submitted. The job stays 'drafted'.[/red]")
        return "quit"
    log["submitted_at"] = _now()
    save_apply(out_dir, log)
    tracker.update_status(conn, job["id"], "submitted", date_applied=date.today().isoformat(),
                          notes=f"confirmation: {conf}")
    console.print(f"[green]Submitted.[/green] Logged with resume {prep.pdf.name} "
                  f"(sha256 {log['resume_sha256'][:12]}…)")
    return "submitted"


def apply_job(
    job_id: int,
    conn: sqlite3.Connection,
    profile: Profile,
    bank: BulletBank,
    runner,
    ask: Callable[[str], str] = input,
    console: Console | None = None,
    headless: bool = False,
) -> str:
    """One job: prepare → your answers → fill → review → submit. Returns 'submitted', 'skipped' or 'quit'."""
    from playwright.sync_api import sync_playwright

    console = console or Console()
    job = tracker.get_job(conn, job_id)
    if job is None:
        raise ApplyError(f"no job with id {job_id}")
    with console.status("Reading the application form and drafting answers…"):
        prep = prepare(job, profile, bank, runner)
    ask_human(prep.answers, ask, console)
    prep.log["answers"] = plan.to_json(prep.answers)
    save_apply(prep.out_dir, prep.log)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        with console.status("Filling the form…"):
            fills, shot = fill(page, prep, profile)
        outcome = review_and_submit(page, prep, fills, shot, conn, ask, console)
        browser.close()
    return outcome


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
