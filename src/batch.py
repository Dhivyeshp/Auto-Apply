"""Batch mode: tailor many jobs in parallel, review them back to back, fill all forms, submit one by one."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

from src import review, tracker, watch
from src.apply import plan, run
from src.schemas import BulletBank, Profile


@dataclass
class BatchResult:
    tailored: list[int] = field(default_factory=list)
    below_cutoff: list[int] = field(default_factory=list)
    approved: list[int] = field(default_factory=list)
    outcomes: dict[int, str] = field(default_factory=dict)  # job id → submitted/skipped/quit/error: ...


def is_tailored(job: dict) -> bool:
    return bool(job.get("tailor_dir")) and (Path(job["tailor_dir"]) / "tailoring.json").exists()


def select_jobs(conn, inbox: list[dict], ids: list[int] | None, top: int) -> list[dict]:
    """Explicit ids, or the top of the inbox; only jobs the bot can apply to and that are still open to act on."""
    if ids:
        jobs = [tracker.get_job(conn, i) for i in ids]
        jobs = [j for j in jobs if j]
    else:
        jobs = inbox
    jobs = [j for j in jobs if j["ats"] in run.SUPPORTED_ATS and j["status"] in ("queued", "drafted")]
    return jobs if ids else jobs[:top]


def tailor_parallel(jobs: list[dict], db_path: Path, tailor_one: Callable, workers: int,
                    on_done: Callable[[dict, str | None], None]) -> dict[int, str]:
    """Tailor each job on its own thread (own DB connection). Returns job id → error for failures."""
    errors: dict[int, str] = {}

    def work(job):
        tailor_one(tracker.connect(db_path), job)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(work, j): j for j in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            err = None
            try:
                fut.result()
            except Exception as e:
                err = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
                errors[job["id"]] = err
            on_done(job, err)
    return errors


def prepare_parallel(jobs: list[dict], prepare_one: Callable, workers: int) -> tuple[list, dict[int, str]]:
    """Read forms + draft answers for each approved job in parallel; keeps the input order."""
    preps, errors = {}, {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(prepare_one, j): j for j in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                preps[job["id"]] = fut.result()
            except Exception as e:
                errors[job["id"]] = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
    return [preps[j["id"]] for j in jobs if j["id"] in preps], errors


def run_batch(
    conn,
    db_path: Path,
    jobs: list[dict],
    cfg: dict,
    profile: Profile,
    bank: BulletBank,
    runner,
    console: Console,
    ask: Callable[[str], str] = input,
    workers: int = 3,
    min_rating: int = 0,
    open_browser: Callable | None = None,
) -> BatchResult:
    res = BatchResult()
    tcfg = cfg.get("tailor", {})

    # 1. tailor (parallel)
    todo = [j for j in jobs if j["status"] == "queued" and not is_tailored(j)]
    if todo:
        done = [0]

        def on_done(job, err):
            done[0] += 1
            mark = f"[red]failed: {err}[/red]" if err else "[green]done[/green]"
            console.print(f"  [{done[0]}/{len(todo)}] {job['company']} — {job['role']}: {mark}")

        console.print(f"[bold]Tailoring {len(todo)} resume(s), {workers} at a time…[/bold]")
        errors = tailor_parallel(todo, db_path,
                                 lambda c, j: watch.tailor_job(c, j, cfg, runner, profile, bank), workers, on_done)
        res.tailored = [j["id"] for j in todo if j["id"] not in errors]
        for jid, err in errors.items():
            res.outcomes[jid] = f"error: {err}"

    # 2. review (one after another, best rating first)
    jobs = [tracker.get_job(conn, j["id"]) for j in jobs if j["id"] not in res.outcomes]
    to_review = [j for j in jobs if j["status"] == "queued" and is_tailored(j)]
    to_review.sort(key=lambda j: (j.get("rating") or 0, j["preferred"]), reverse=True)
    for j in to_review:
        if min_rating and (j.get("rating") or 0) < min_rating:
            res.below_cutoff.append(j["id"])
            res.outcomes[j["id"]] = f"below cutoff ({j.get('rating')}/10)"
            continue
        console.rule(f"[bold]Review {to_review.index(j) + 1}/{len(to_review)}[/bold]")
        decision = review.review(Path(j["tailor_dir"]), profile, bank, conn,
                                 threshold=tcfg.get("coverage_threshold", 50), ask=ask, console=console,
                                 pdf_name=tcfg.get("pdf_name"), pdf_types=tcfg.get("pdf_types"))
        if decision == "skip":
            res.outcomes[j["id"]] = "skipped"
        elif decision == "quit":
            res.outcomes[j["id"]] = "left for later"
    ready = [tracker.get_job(conn, j["id"]) for j in jobs]
    ready = [j for j in ready if j["status"] == "drafted"]
    res.approved = [j["id"] for j in ready]
    if not ready:
        return res

    # 3. prepare forms (parallel), then ask you everything up front
    console.print(f"[bold]Reading {len(ready)} application form(s) and drafting answers…[/bold]")
    preps, errors = prepare_parallel(ready, lambda j: run.prepare(j, profile, bank, runner), workers)
    for jid, err in errors.items():
        res.outcomes[jid] = f"error: {err}"
    for p in preps:
        if any(a.source == "you" and a.required for a in p.answers):
            console.rule(f"[bold]{p.job['company']} — {p.job['role']}[/bold]: questions for you")
        run.ask_human(p.answers, ask, console)
        p.log["answers"] = plan.to_json(p.answers)
        run.save_apply(p.out_dir, p.log)
    if not preps:
        return res

    # 4. fill every form in its own tab, then 5. review + submit each
    (open_browser or _with_chromium)(lambda browser: _fill_and_submit(browser, preps, conn, profile, ask, console, res))
    return res


def _with_chromium(fn: Callable) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            fn(browser)
        finally:
            browser.close()


def _fill_and_submit(browser, preps, conn, profile, ask, console, res: BatchResult) -> None:
    pages = []
    for i, prep in enumerate(preps, 1):
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        with console.status(f"Filling form {i}/{len(preps)}: {prep.job['company']}…"):
            try:
                fills, shot = run.fill(page, prep, profile)
            except Exception as e:
                res.outcomes[prep.job["id"]] = f"error: {e}"
                page.close()
                continue
        pages.append((prep, page, fills, shot))
    for i, (prep, page, fills, shot) in enumerate(pages, 1):
        page.bring_to_front()
        console.print(f"\n[bold]Application {i}/{len(pages)}[/bold] (its tab is now in front)")
        outcome = run.review_and_submit(page, prep, fills, shot, conn, ask, console)
        res.outcomes[prep.job["id"]] = {"quit": "left for later"}.get(outcome, outcome)


def summary(conn, res: BatchResult) -> Table:
    table = Table(title="Batch summary")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Company", style="bold")
    table.add_column("Role")
    table.add_column("Fit", justify="right")
    table.add_column("Result")
    style = {"submitted": "green", "skipped": "yellow"}
    for jid, outcome in res.outcomes.items():
        j = tracker.get_job(conn, jid)
        r = j.get("rating")
        color = style.get(outcome, "red" if outcome.startswith("error") else "dim")
        table.add_row(str(jid), j["company"], j["role"], f"{r}/10" if r else "—", f"[{color}]{outcome}[/{color}]")
    return table
