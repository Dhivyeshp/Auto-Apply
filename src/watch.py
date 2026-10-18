"""Background job: discover new postings, pre-tailor the applyable ones, and alert."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from src import ingest, tailor, tracker
from src.discover import simplify
from src.schemas import BulletBank, Profile

ROOT = Path(__file__).resolve().parents[1]
LOCK_STALE_S = 2 * 3600


@dataclass
class WatchResult:
    unchanged: bool = False
    new_jobs: list[dict] = field(default_factory=list)  # tracker rows, refreshed after tailoring
    tailored: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    alerted: bool = False


def tailor_job(conn: sqlite3.Connection, job: dict, cfg: dict, runner, profile: Profile,
               bank: BulletBank) -> tailor.TailorResult:
    """Fetch the JD for a tracked job, tailor + rate, and record the output folder on the job."""
    tcfg = cfg.get("tailor", {})
    posting = ingest.fetch_posting(job["url"])
    result = tailor.tailor(
        posting.jd_text, job["company"], job["role"], profile, bank, runner=runner,
        out_root=ROOT / "output",
        max_bullets=tcfg.get("max_bullets", 15),
        min_score=tcfg.get("min_score", 20),
        coverage_threshold=tcfg.get("coverage_threshold", 50),
        job_id=job["id"],
        pdf_name=tcfg.get("pdf_name"),
        pdf_types=tcfg.get("pdf_types"),
        target_rating=tcfg.get("target_rating"),
        improve_rounds=tcfg.get("improve_rounds", 2),
    )
    rating = result.rating.score if result.rating else None
    tracker.set_tailored(conn, job["id"], str(result.out_dir), rating)
    copy_to_sync_dir((cfg.get("notify") or {}).get("sync_dir"), job, result.pdf, rating)
    return result


def copy_to_sync_dir(sync_dir: str | None, job: dict, pdf: Path, rating: int | None) -> Path | None:
    """Copy a tailored PDF into a cloud-synced folder (OneDrive/Drive/Dropbox) under a readable name."""
    if not sync_dir or not pdf.exists():
        return None
    folder = Path(os.path.expandvars(os.path.expanduser(sync_dir)))
    folder.mkdir(parents=True, exist_ok=True)
    score = f" ({rating} of 10)" if rating else ""
    name = re.sub(r'[<>:"/\\|?*]+', "-", f"{job['company']} - {job['role']}")[:100].strip(" .-")
    dest = folder / f"{name}{score} #{job['id']}.pdf"
    shutil.copyfile(pdf, dest)
    return dest


def _pick_for_tailoring(conn: sqlite3.Connection, jobs: list[dict], wcfg: dict) -> list[dict]:
    """Applyable new jobs, preferred (TX/remote) first, within the per-run and per-day caps."""
    ats_ok = set(wcfg.get("ats", ["greenhouse", "lever", "ashby"]))
    candidates = [j for j in jobs if j["ats"] in ats_ok]
    candidates.sort(key=lambda j: (j["preferred"], j.get("date_posted") or ""), reverse=True)
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    left_today = max(0, wcfg.get("max_tailor_per_day", 20) - tracker.count_tailored_since(conn, day_ago))
    return candidates[: min(wcfg.get("max_tailor_per_run", 5), left_today)]


def alert_text(jobs: list[dict]) -> tuple[str, list[str]]:
    """Title + lines, best first: rated jobs by score, then the rest (preferred first)."""
    ranked = sorted(jobs, key=lambda j: (j.get("rating") or 0, j["preferred"]), reverse=True)
    lines = []
    for j in ranked[:6]:
        star = "★ " if j["preferred"] else ""
        score = f"  {j['rating']}/10 ready" if j.get("rating") else ""
        lines.append(f"{star}{j['company']} — {j['role']}{score}")
    if len(jobs) > 6:
        lines.append(f"+{len(jobs) - 6} more")
    ready = sum(1 for j in jobs if j.get("rating"))
    title = f"{len(jobs)} new internship{'s' if len(jobs) != 1 else ''}" + (f" · {ready} ready to apply" if ready else "")
    return title, lines


class Lock:
    """Skip a run if the previous one is still tailoring (stale locks expire)."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> bool:
        if self.path.exists() and time.time() - self.path.stat().st_mtime < LOCK_STALE_S:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)


def run_watch(
    cfg: dict,
    conn: sqlite3.Connection,
    profile: Profile,
    bank: BulletBank,
    runner,
    notify: Callable[[str, list[str]], list[str]],
    listings: list[dict] | None = None,
    log: Callable[[str], None] = lambda s: None,
    on_ready: Callable[[dict, tailor.TailorResult], None] | None = None,
) -> WatchResult:
    wcfg = cfg.get("watch", {})
    res = WatchResult()
    stats = simplify.run(cfg, conn, listings=listings, etag_file=ROOT / "data" / "listings.etag")
    if stats.get("unchanged"):
        res.unchanged = True
        log("feed unchanged")
        return res
    new = [tracker.get_job_by_listing(conn, j["listing_id"]) for j in stats["new_jobs"]]
    new = [j for j in new if j]
    log(f"{stats['total']} listings, {stats['kept']} match, {len(new)} new")
    if not new:
        return res

    if wcfg.get("auto_tailor", True) and runner is not None:
        for job in _pick_for_tailoring(conn, new, wcfg):
            try:
                result = tailor_job(conn, job, cfg, runner, profile, bank)
                res.tailored.append(job["id"])
                log(f"tailored job {job['id']} {job['company']} — {job['role']}")
            except Exception as e:  # one bad posting must not stop the alert
                res.errors.append(f"job {job['id']}: {e}")
                log(f"tailor failed for job {job['id']}: {e}")
                continue
            if on_ready is not None:
                try:
                    on_ready(job, result)
                except Exception as e:
                    res.errors.append(f"ready push for job {job['id']}: {e}")
                    log(f"ready push failed for job {job['id']}: {e}")

    res.new_jobs = [tracker.get_job(conn, j["id"]) for j in new]
    title, lines = alert_text(res.new_jobs)
    errs = notify(title, lines + ["Open: python main.py inbox"])
    res.errors += errs
    res.alerted = True
    for e in errs:
        log(f"alert error: {e}")
    return res
