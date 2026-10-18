"""CLI approval gate: show resume diff vs base + ATS coverage, then approve / edit / skip."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src import ats_check, render, tracker
from src.schemas import BulletBank, Profile
from src.tailor import check_rewrite, tech_vocab

HELP = """[bold]a[/bold] approve   [bold]s[/bold] skip   [bold]o[/bold] open PDF   [bold]e[/bold] edit   [bold]q[/bold] quit (decide later)
edit commands (one per line, blank line to re-render):
  -ID          remove bullet          +ID          add bullet (lowest rank)
  r ID         revert to original     t ID TEXT    replace text (no-invention check applies)"""


@dataclass
class ReviewState:
    kept: list[str]
    texts: dict[str, str]
    dropped: list[str] = field(default_factory=list)


def load_record(out_dir: Path) -> dict:
    return json.loads((out_dir / "tailoring.json").read_text(encoding="utf-8"))


def save_record(out_dir: Path, record: dict) -> None:
    (out_dir / "tailoring.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def word_diff(old: str, new: str) -> Text:
    out = Text()
    a, b = old.split(), new.split()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b).get_opcodes():
        if op == "equal":
            out.append(" ".join(a[i1:i2]) + " ")
        if op in ("delete", "replace"):
            out.append(" ".join(a[i1:i2]) + " ", style="red strike")
        if op in ("insert", "replace"):
            out.append(" ".join(b[j1:j2]) + " ", style="green")
    return out


def diff_rows(record: dict, bank: BulletBank, state: ReviewState) -> list[tuple[str, str, str, Text]]:
    """(marker, id, score, text) for every bullet in bank order: '+' kept, '~' rewritten, '-' left out."""
    scores = {s["id"]: s for s in record.get("scores", [])}
    rows = []
    for b in bank.all_bullets():
        sc = scores.get(b.id, {})
        score = str(sc.get("score", "")) if sc else ""
        if b.id in state.kept:
            if b.id in state.texts and state.texts[b.id] != b.text:
                rows.append(("~", b.id, score, word_diff(b.text, state.texts[b.id])))
            else:
                rows.append(("+", b.id, score, Text(b.text)))
        else:
            why = "dropped to fit 1 page" if b.id in state.dropped else "not selected"
            rows.append(("-", b.id, score, Text(f"{b.text}  ({why})", style="dim")))
    return rows


def apply_edit(cmd: str, state: ReviewState, bank: BulletBank) -> str:
    """Apply one edit command to the state; returns a message for the user."""
    by_id = {b.id: b for b in bank.all_bullets()}
    cmd = cmd.strip()
    if cmd.startswith(("-", "+")):
        bid = cmd[1:].strip()
        if bid not in by_id:
            return f"unknown id {bid!r}"
        if cmd[0] == "-":
            if bid in state.kept:
                state.kept.remove(bid)
            return f"removed {bid}"
        if bid not in state.kept:
            state.kept.append(bid)
        return f"added {bid}"
    head, _, rest = cmd.partition(" ")
    if head == "r":
        bid = rest.strip()
        if bid not in by_id:
            return f"unknown id {bid!r}"
        state.texts.pop(bid, None)
        return f"reverted {bid}"
    if head == "t":
        bid, _, text = rest.strip().partition(" ")
        if bid not in by_id:
            return f"unknown id {bid!r}"
        problems = check_rewrite(by_id[bid].text, text, tech_vocab(bank))
        if problems:
            return f"rejected: {'; '.join(problems)}"
        state.texts[bid] = text.strip()
        return f"updated {bid}"
    return f"unknown command {cmd!r}"


def rerender(out_dir: Path, record: dict, profile: Profile, bank: BulletBank, state: ReviewState,
             threshold: float) -> ats_check.Coverage:
    texts = {k: v for k, v in state.texts.items() if k in state.kept}
    fit = render.fit_one_page(profile, bank, list(state.kept), out_dir, texts=texts)
    state.kept, state.dropped = fit.kept, fit.dropped
    a = record["analysis"]
    cov = ats_check.check_pdf(fit.pdf, a["keywords"], a["must_have"], threshold)
    by_id = {b.id: b for b in bank.all_bullets()}
    record.update({
        "kept": fit.kept,
        "dropped_to_fit": fit.dropped,
        "rewrites_accepted": texts,
        "original_text": {i: by_id[i].text for i in fit.kept},
        "ats": cov.to_dict(),
        "edited_in_review": True,
        "rating_stale": bool(record.get("rating")),
    })
    save_record(out_dir, record)
    return cov


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decide(out_dir: Path, record: dict, decision: str, conn: sqlite3.Connection | None,
           pdf_name: str | None = None) -> None:
    """Record approve/skip in tailoring.json and, if linked to a job, in the tracker."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pdf = out_dir / "resume.pdf"
    cov = record.get("ats", {}).get("coverage_pct")
    if decision == "approve":
        upload = render.named_copy(pdf, pdf_name)  # refreshed so the named file is exactly what was approved
        record["approved"] = {"at": now, "resume_path": str(pdf), "upload_path": str(upload),
                              "sha256": sha256(pdf), "coverage_pct": cov}
        record.pop("skipped", None)
    else:
        record["skipped"] = {"at": now}
        record.pop("approved", None)
    save_record(out_dir, record)
    job_id = record.get("job_id")
    if conn is not None and job_id is not None:
        if decision == "approve":
            tracker.update_status(conn, job_id, "drafted", resume_path=str(pdf), coverage_pct=cov)
        else:
            tracker.update_status(conn, job_id, "skipped")


def show(console: Console, record: dict, bank: BulletBank, state: ReviewState) -> None:
    a = record["analysis"]
    console.print(Panel(f"[bold]{record['company']} — {record['role']}[/bold]  ({a['role_category']})\n{a['summary']}",
                        title=f"job {record.get('job_id') or '—'}"))
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("", width=1)
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("score", justify="right", width=5)
    table.add_column("bullet (diff vs base resume)", ratio=1)
    style = {"+": "green", "~": "yellow", "-": "red"}
    for marker, bid, score, text in diff_rows(record, bank, state):
        table.add_row(Text(marker, style=style[marker]), bid, score, text)
    console.print(table)
    cov = record.get("ats", {})
    color = "red" if cov.get("below_threshold") else "green"
    console.print(f"ATS coverage [{color}]{cov.get('coverage_pct')}%[/{color}]  must-haves {cov.get('must_have_pct')}%  "
                  f"bullets {len(state.kept)}")
    if cov.get("must_have_missing"):
        console.print(f"  [yellow]missing must-haves:[/yellow] {', '.join(cov['must_have_missing'])}")
    if cov.get("missing"):
        console.print(f"  [dim]missing keywords: {', '.join(cov['missing'])}[/dim]")
    if record.get("rating"):
        show_rating(console, record["rating"], stale=bool(record.get("rating_stale")))


def show_rating(console: Console, r: dict, stale: bool = False) -> None:
    color = "green" if r["score"] >= 7 else "yellow" if r["score"] >= 5 else "red"
    note = "  [dim](before your edits; `python main.py rate <dir>` refreshes it)[/dim]" if stale else ""
    console.print(f"Claude's fit rating [bold {color}]{r['score']}/10[/bold {color}]{note}  {r['verdict']}")
    for s in r.get("strengths", []):
        console.print(f"  [green]+[/green] {s}")
    for g in r.get("gaps", []):
        console.print(f"  [red]-[/red] {g}")


def review(
    out_dir: Path,
    profile: Profile,
    bank: BulletBank,
    conn: sqlite3.Connection | None,
    threshold: float = 50.0,
    ask: Callable[[str], str] = input,
    console: Console | None = None,
    pdf_name: str | None = None,
) -> str:
    """Interactive loop. Returns 'approve', 'skip' or 'quit'."""
    console = console or Console()
    record = load_record(out_dir)
    state = ReviewState(kept=list(record["kept"]), texts=dict(record.get("rewrites_accepted", {})),
                        dropped=list(record.get("dropped_to_fit", [])))
    show(console, record, bank, state)
    while True:
        console.print(HELP)
        choice = ask("> ").strip().lower()
        if choice == "a":
            decide(out_dir, record, "approve", conn, pdf_name)
            console.print(f"[green]approved[/green] {record['approved']['upload_path']}")
            return "approve"
        if choice == "s":
            decide(out_dir, record, "skip", conn)
            console.print("[yellow]skipped[/yellow]")
            return "skip"
        if choice in ("q", ""):
            return "quit"
        if choice == "o":
            pdf = out_dir / "resume.pdf"
            if hasattr(os, "startfile"):
                os.startfile(pdf)  # type: ignore[attr-defined]
            else:
                console.print(str(pdf))
            continue
        if choice == "e":
            changed = False
            while (line := ask("edit> ").strip()):
                msg = apply_edit(line, state, bank)
                changed = changed or not msg.startswith(("unknown", "rejected"))
                console.print(f"  {msg}")
            if changed:
                with console.status("Re-rendering…"):
                    rerender(out_dir, record, profile, bank, state, threshold)
                show(console, record, bank, state)
            continue
        console.print(f"unknown choice {choice!r}")
