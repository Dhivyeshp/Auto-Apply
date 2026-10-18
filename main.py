"""AutoApply CLI."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from src import tracker
from src.discover import simplify

ROOT = Path(__file__).resolve().parent
console = Console()


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path) -> dict:
    """config.yaml, overlaid with config.local.yaml (gitignored: topics, webhooks, personal tweaks)."""
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    local = path.with_name("config.local.yaml")
    if local.exists():
        cfg = _merge(cfg, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    return cfg


def jobs_table(jobs: list[dict], title: str, limit: int | None = None) -> Table:
    table = Table(title=title, show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("★", justify="center")
    table.add_column("Company", style="bold")
    table.add_column("Role")
    table.add_column("Location")
    table.add_column("Terms")
    table.add_column("Cat")
    table.add_column("ATS")
    table.add_column("Posted", style="dim")
    # preferred (TX/remote) first, then newest
    shown = sorted(jobs, key=lambda j: (j["preferred"], j.get("date_posted") or ""), reverse=True)
    if limit:
        shown = shown[:limit]
    for i, j in enumerate(shown, 1):
        locs = j.get("locations") or []
        loc = locs[0] + (f" +{len(locs) - 1}" if len(locs) > 1 else "") if locs else ""
        table.add_row(
            str(j.get("id", i)),
            "★" if j["preferred"] else "",
            j["company"],
            j["role"],
            loc,
            ", ".join(j.get("terms") or []),
            j.get("category") or "",
            j.get("ats") or "",
            j.get("date_posted") or "",
        )
    return table


def cmd_discover(args: argparse.Namespace, cfg: dict) -> None:
    conn = None if args.dry_run else tracker.connect(ROOT / cfg["db_path"])
    with console.status("Fetching SimplifyJobs listings…"):
        stats = simplify.run(cfg, conn, dry_run=args.dry_run)

    summary = Table(title="Discovery summary", show_header=False)
    summary.add_row("Listings in feed", str(stats["total"]))
    summary.add_row("Matched filters", str(stats["kept"]))
    summary.add_row("New (queued)", "—" if args.dry_run else str(stats["new"]))
    for reason, n in stats["drops"].items():
        summary.add_row(f"  dropped: {reason}", str(n), style="dim")
    console.print(summary)

    jobs = stats["kept_jobs"] if args.dry_run else stats["new_jobs"]
    if jobs:
        label = "Matches (dry run)" if args.dry_run else "Newly queued"
        console.print(jobs_table(jobs, f"{label}: {len(jobs)}", limit=args.limit))
        if args.limit and len(jobs) > args.limit:
            console.print(f"[dim]…showing {args.limit} of {len(jobs)}; use --limit 0 for all[/dim]")


def cmd_jobs(args: argparse.Namespace, cfg: dict) -> None:
    conn = tracker.connect(ROOT / cfg["db_path"])
    jobs = tracker.list_jobs(conn, status=args.status or None)
    console.print(jobs_table(jobs, f"Jobs ({args.status or 'all'}): {len(jobs)}", limit=args.limit))


def cmd_render(args: argparse.Namespace, cfg: dict) -> None:
    from src import render, schemas

    profile, bank = schemas.load_profile(), schemas.load_bullets()
    # base resume: every bullet, ranked by priority (stable within the bank's order)
    ranked = [b.id for b in sorted(bank.all_bullets(), key=lambda b: b.priority)]
    with console.status("Compiling resume…"):
        result = render.fit_one_page(profile, bank, ranked, Path(args.out))
    console.print(f"[green]{result.pdf}[/green]  pages={result.pages}  bullets={len(result.kept)}")
    if result.dropped:
        console.print(f"[yellow]dropped to fit 1 page:[/yellow] {', '.join(result.dropped)}")


def cmd_tailor(args: argparse.Namespace, cfg: dict) -> None:
    from src import schemas, tailor

    tcfg = cfg.get("tailor", {})
    company, role, job = args.company, args.role, None
    if args.job_id is not None:
        job = tracker.get_job(tracker.connect(ROOT / cfg["db_path"]), args.job_id)
        if job is None:
            raise SystemExit(f"no job with id {args.job_id}")
        company, role = company or job["company"], role or job["role"]
    if not (company and role):
        raise SystemExit("give --job-id, or both --company and --role")
    if args.jd:
        jd_text = Path(args.jd).read_text(encoding="utf-8")
    elif job is not None:
        from src import ingest

        try:
            with console.status("Fetching the job description…"):
                jd_text = ingest.fetch_posting(job["url"]).jd_text
        except NotImplementedError as e:
            raise SystemExit(f"{e}; save the JD to a file and pass --jd")
    else:
        raise SystemExit("pass --jd <file> (or --job-id for a Greenhouse job to fetch it automatically)")
    with console.status("Tailoring (extract → select → rewrite → render)…"):
        result = tailor.tailor(
            jd_text, company, role,
            schemas.load_profile(), schemas.load_bullets(),
            runner=tailor.claude_runner(model=args.model or tcfg.get("model", "sonnet")),
            max_bullets=tcfg.get("max_bullets", 15),
            min_score=tcfg.get("min_score", 20),
            rewrite=not args.no_rewrite,
            coverage_threshold=tcfg.get("coverage_threshold", 50),
            job_id=args.job_id,
            pdf_name=tcfg.get("pdf_name"),
        )
    if job is not None:
        from src.watch import copy_to_sync_dir

        rating = result.rating.score if result.rating else None
        tracker.set_tailored(tracker.connect(ROOT / cfg["db_path"]), job["id"], str(result.out_dir), rating)
        copy_to_sync_dir((cfg.get("notify") or {}).get("sync_dir"), job, result.pdf, rating)
    a = result.analysis
    console.print(f"[bold]{company} — {role}[/bold]  ({a.role_category})  {a.summary}")
    console.print(f"[dim]keywords:[/dim] {', '.join(a.keywords)}")
    console.print(f"bullets kept {len(result.kept)}, dropped to fit {len(result.dropped)}, "
                  f"rewrites accepted {len(result.texts)}, rejected {len(result.rejected)}")
    for bid, problems in result.rejected.items():
        console.print(f"  [yellow]kept original {bid}:[/yellow] {'; '.join(problems)}")
    print_coverage(result.coverage)
    if result.rating:
        from src.review import show_rating

        show_rating(console, result.rating.model_dump())
    console.print(f"[green]{result.pdf}[/green]")


def cmd_rate(args: argparse.Namespace, cfg: dict) -> None:
    import json

    from src import tailor
    from src.review import show_rating

    out_dir = Path(args.dir)
    record_path = out_dir / "tailoring.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    jd_text = (out_dir / "jd.txt").read_text(encoding="utf-8")
    runner = tailor.claude_runner(model=cfg.get("tailor", {}).get("model", "sonnet"))
    with console.status("Claude is rating the resume…"):
        rating = tailor.rate(runner, jd_text, out_dir / "resume.pdf")
    record["rating"] = rating.model_dump()
    record.pop("rating_stale", None)
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    show_rating(console, record["rating"])


def print_coverage(cov) -> None:
    color = "red" if cov.below_threshold else "green"
    console.print(f"ATS coverage [{color}]{cov.coverage_pct}%[/{color}] of keywords, "
                  f"must-haves {cov.must_have_pct}%" + ("  [red]⚠ below threshold[/red]" if cov.below_threshold else ""))
    if cov.must_have_missing:
        console.print(f"  [yellow]missing must-haves:[/yellow] {', '.join(cov.must_have_missing)}")
    if cov.missing:
        console.print(f"  [dim]missing keywords: {', '.join(cov.missing)}[/dim]")


def cmd_ats(args: argparse.Namespace, cfg: dict) -> None:
    import json

    from src import ats_check

    out_dir = Path(args.dir)
    record = json.loads((out_dir / "tailoring.json").read_text(encoding="utf-8"))
    a = record["analysis"]
    cov = ats_check.check_pdf(out_dir / "resume.pdf", a["keywords"], a["must_have"],
                              cfg.get("tailor", {}).get("coverage_threshold", 50))
    print_coverage(cov)


def cmd_review(args: argparse.Namespace, cfg: dict) -> None:
    from src import review, schemas

    review.review(
        Path(args.dir), schemas.load_profile(), schemas.load_bullets(),
        conn=tracker.connect(ROOT / cfg["db_path"]),
        threshold=cfg.get("tailor", {}).get("coverage_threshold", 50),
        console=console,
        pdf_name=cfg.get("tailor", {}).get("pdf_name"),
    )


def cmd_apply(args: argparse.Namespace, cfg: dict) -> None:
    from src import schemas, tailor
    from src.apply import run

    runner = None if args.no_claude else tailor.claude_runner(model=cfg.get("tailor", {}).get("model", "sonnet"))
    try:
        run.apply_job(args.job_id, tracker.connect(ROOT / cfg["db_path"]), schemas.load_profile(),
                      schemas.load_bullets(), runner, console=console)
    except run.ApplyError as e:
        raise SystemExit(str(e))


def _log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    log_path = ROOT / "data" / "watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def cmd_watch(args: argparse.Namespace, cfg: dict) -> None:
    from src import notify, schemas, tailor, watch

    with watch.Lock(ROOT / "data" / "watch.lock") as got_lock:
        if not got_lock:
            _log("previous run still going; skipped")
            return
        runner = None if args.no_tailor else tailor.claude_runner(model=cfg.get("tailor", {}).get("model", "sonnet"))
        try:
            res = watch.run_watch(
                cfg, tracker.connect(ROOT / cfg["db_path"]), schemas.load_profile(), schemas.load_bullets(), runner,
                notify=lambda title, lines: notify.send(cfg, title, lines), log=_log,
                on_ready=lambda job, r: notify.send_ready(
                    cfg, job, r.pdf, r.rating.score if r.rating else None, r.rating.verdict if r.rating else ""),
            )
        except Exception as e:
            _log(f"watch failed: {e}")
            raise
    if res.unchanged:
        console.print("Feed unchanged since the last check.")
    elif not res.new_jobs:
        console.print("No new matching jobs.")
    else:
        console.print(f"{len(res.new_jobs)} new, {len(res.tailored)} pre-tailored. Run [bold]python main.py inbox[/bold].")
    for e in res.errors:
        console.print(f"[yellow]{e}[/yellow]")


def inbox_jobs(conn) -> list[dict]:
    """Queued/drafted jobs: tailored ones by rating, then untailored applyable ones, newest first."""
    jobs = [j for j in tracker.list_jobs(conn) if j["status"] in ("queued", "drafted")]
    ready = sorted((j for j in jobs if j.get("tailor_dir")),
                   key=lambda j: (j["status"] == "drafted", j.get("rating") or 0, j["preferred"]), reverse=True)
    rest = sorted((j for j in jobs if not j.get("tailor_dir") and j["ats"] in ("greenhouse", "lever", "ashby")),
                  key=lambda j: (j.get("discovered_at") or "", j["preferred"]), reverse=True)
    return ready + rest


def cmd_inbox(args: argparse.Namespace, cfg: dict) -> None:
    conn = tracker.connect(ROOT / cfg["db_path"])
    jobs = inbox_jobs(conn)[: args.limit or None]
    if not jobs:
        console.print("Inbox empty. New jobs show up here after `discover` or the scheduled watcher.")
        return
    table = Table(title="Inbox", show_lines=False)
    for col, kw in [("#", {"justify": "right", "style": "dim"}), ("★", {"justify": "center"}),
                    ("Fit", {"justify": "right"}), ("Status", {}), ("Company", {"style": "bold"}), ("Role", {}),
                    ("ATS", {}), ("Found", {"style": "dim"})]:
        table.add_column(col, **kw)
    for j in jobs:
        r = j.get("rating")
        fit = f"[{'green' if r >= 7 else 'yellow' if r >= 5 else 'red'}]{r}/10[/]" if r else "—"
        status = "approved" if j["status"] == "drafted" else ("ready" if j.get("tailor_dir") else "new")
        table.add_row(str(j["id"]), "★" if j["preferred"] else "", fit, status, j["company"], j["role"],
                      j["ats"], (j.get("discovered_at") or "")[:10])
    console.print(table)
    choice = input("Job # to open (blank to exit): ").strip()
    if choice.isdigit():
        go(int(choice), cfg)


def go(job_id: int, cfg: dict) -> None:
    """Tailor if needed → review → apply, for one job."""
    from src import review, schemas, tailor, watch
    from src.apply import run

    conn = tracker.connect(ROOT / cfg["db_path"])
    job = tracker.get_job(conn, job_id)
    if job is None:
        raise SystemExit(f"no job with id {job_id}")
    if job["status"] not in ("queued", "drafted"):
        raise SystemExit(f"job {job_id} is already {job['status']}")
    profile, bank = schemas.load_profile(), schemas.load_bullets()
    runner = tailor.claude_runner(model=cfg.get("tailor", {}).get("model", "sonnet"))

    if job["status"] == "queued":
        if not (job.get("tailor_dir") and (Path(job["tailor_dir"]) / "tailoring.json").exists()):
            with console.status(f"Tailoring for {job['company']} — {job['role']}…"):
                watch.tailor_job(conn, job, cfg, runner, profile, bank)
            job = tracker.get_job(conn, job_id)
        decision = review.review(Path(job["tailor_dir"]), profile, bank, conn,
                                 threshold=cfg.get("tailor", {}).get("coverage_threshold", 50), console=console,
                                 pdf_name=cfg.get("tailor", {}).get("pdf_name"))
        if decision != "approve":
            return
        if input("Fill the application now? [Y/n] ").strip().lower() == "n":
            return
    try:
        run.apply_job(job_id, conn, profile, bank, runner, console=console)
    except run.ApplyError as e:
        raise SystemExit(str(e))


def cmd_go(args: argparse.Namespace, cfg: dict) -> None:
    go(args.job_id, cfg)


TASK_NAME = "AutoApply watcher"


def cmd_schedule(args: argparse.Namespace, cfg: dict) -> None:
    import subprocess
    import sys

    if args.remove:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False)
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    action = f'"{exe}" "{ROOT / "main.py"}" watch'
    subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "MINUTE", "/MO", str(args.every),
                    "/TR", action], check=True)
    console.print(f"Scheduled [bold]{TASK_NAME}[/bold] every {args.every} min: {action}\n"
                  f"Log: {ROOT / 'data' / 'watch.log'}   Remove: python main.py schedule --remove")


def cmd_cloud_check(args: argparse.Namespace, cfg: dict) -> None:
    from src import cloud

    res = cloud.check(cfg, ROOT / "data" / "cloud_seen.json", cloud.env_sender(cfg),
                      etag_file=ROOT / "data" / "listings.etag")
    if res.status == "unchanged":
        print("feed unchanged")
    elif res.status == "seeded":
        print("first run: recorded current jobs, no alerts sent")
    else:
        print(f"{len(res.new)} new job(s) alerted")
    if res.errors:
        raise SystemExit("alert errors (will retry next run): " + "; ".join(res.errors))


def cmd_notify_test(args: argparse.Namespace, cfg: dict) -> None:
    from src import notify

    errors = notify.send(cfg, "AutoApply alerts are working",
                         ["★ Example Co — Software Engineer Intern  8/10 ready", "Open: python main.py inbox"])
    console.print("[red]" + "\n".join(errors) + "[/red]" if errors else "Sent. You should see a notification.")


def main() -> None:
    import sys

    if sys.stdout is None:  # running windowless (pythonw from the scheduler): send output to the log
        log_path = ROOT / "data" / "watch.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(log_path, "a", encoding="utf-8")
        console.file = sys.stdout
    parser = argparse.ArgumentParser(prog="autoapply")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="pull SimplifyJobs listings and queue new matches")
    p.add_argument("--dry-run", action="store_true", help="filter and report without writing to the DB")
    p.add_argument("--limit", type=int, default=50, help="rows to show (0 = all)")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("jobs", help="list tracked jobs")
    p.add_argument("--status", default="queued", help="filter by status ('' = all)")
    p.add_argument("--limit", type=int, default=50, help="rows to show (0 = all)")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("render", help="render the base resume (all bullets) to a 1-page PDF")
    p.add_argument("--out", default=str(ROOT / "output" / "base"))
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("tailor", help="tailor a 1-page resume to a JD via headless Claude Code")
    p.add_argument("--jd", help="path to the JD text file (optional with --job-id for Greenhouse jobs)")
    p.add_argument("--job-id", type=int, help="tracked job id; fills company/role and links the review")
    p.add_argument("--company")
    p.add_argument("--role")
    p.add_argument("--model", help="override tailor.model from config")
    p.add_argument("--no-rewrite", action="store_true", help="select only; keep bullet text verbatim")
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser("ats", help="re-check keyword coverage of a tailored output folder")
    p.add_argument("dir", help="output/{company}_{role}_{date} folder")
    p.set_defaults(func=cmd_ats)

    p = sub.add_parser("rate", help="have Claude (re)rate a tailored resume against its JD, 1-10")
    p.add_argument("dir", help="output/{company}_{role}_{date} folder")
    p.set_defaults(func=cmd_rate)

    p = sub.add_parser("review", help="review a tailored resume: diff, coverage, approve/edit/skip")
    p.add_argument("dir", help="output/{company}_{role}_{date} folder")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("apply", help="fill a Greenhouse application, review it, submit only on your say-so")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--no-claude", action="store_true", help="don't draft answers with Claude; ask for all of them")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("watch", help="discover new jobs, pre-tailor applyable ones, and alert (run by the scheduler)")
    p.add_argument("--no-tailor", action="store_true", help="alert only; don't pre-tailor")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("inbox", help="new and ready jobs, best fit first; pick one to review and apply")
    p.add_argument("--limit", type=int, default=30, help="rows to show (0 = all)")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("go", help="one job end to end: tailor if needed → review → apply")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_go)

    p = sub.add_parser("schedule", help="run `watch` automatically with Windows Task Scheduler")
    p.add_argument("--every", type=int, default=30, help="minutes between checks")
    p.add_argument("--remove", action="store_true", help="remove the scheduled task")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("cloud-check", help="phone alerts for new jobs (run by GitHub Actions, no laptop needed)")
    p.set_defaults(func=cmd_cloud_check)

    p = sub.add_parser("notify-test", help="send a test alert through the configured channels")
    p.set_defaults(func=cmd_notify_test)

    args = parser.parse_args()
    args.func(args, load_config(Path(args.config)))


if __name__ == "__main__":
    main()
