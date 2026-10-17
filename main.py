"""AutoApply CLI."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from src import tracker
from src.discover import simplify

ROOT = Path(__file__).resolve().parent
console = Console()


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


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


def main() -> None:
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

    args = parser.parse_args()
    args.func(args, load_config(Path(args.config)))


if __name__ == "__main__":
    main()
