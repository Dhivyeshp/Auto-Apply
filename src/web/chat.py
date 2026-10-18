"""Chat: edit one resume in plain words, or ask Claude Code to fix the app itself."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from src import review, tailor
from src.schemas import BulletBank, Profile
from src.tools import NO_WINDOW, find_exe

ROOT = Path(__file__).resolve().parents[2]


# ---- resume chat ----

class ResumeEdit(BaseModel):
    reply: str
    selected: list[str] | None = None
    rewrites: list[tailor.Rewrite] = Field(default_factory=list)


@dataclass
class ResumeChatResult:
    reply: str
    changed: bool
    rejected: dict[str, list[str]] = field(default_factory=dict)
    coverage: float | None = None


def resume_chat(out_dir: Path, request: str, history: list[dict], profile: Profile, bank: BulletBank,
                runner, threshold: float = 50.0) -> ResumeChatResult:
    record = review.load_record(out_dir)
    by_id = {b.id: b for b in bank.all_bullets()}
    sections = {b.id: s for s, entries in (("experience", bank.experience), ("projects", bank.projects),
                                           ("leadership", bank.leadership)) for e in entries for b in e.bullets}
    texts = dict(record.get("rewrites_accepted", {}))
    a = record.get("analysis", {})
    payload = {
        "request": request,
        "history": history[-8:],
        "job": {"company": record.get("company"), "role": record.get("role"), "summary": a.get("summary"),
                "keywords": a.get("keywords"), "must_have": a.get("must_have")},
        "rating": record.get("rating"),
        "selected": record.get("kept", []),
        "bullets": [{"id": b.id, "section": sections.get(b.id), "original": b.text,
                     "current": texts.get(b.id, b.text)} for b in bank.all_bullets()],
    }
    edit = tailor.call(runner, "resume_chat.md", json.dumps(payload), ResumeEdit)

    kept = list(record.get("kept", []))
    if edit.selected is not None:
        kept = [i for i in dict.fromkeys(edit.selected) if i in by_id]
    new_texts, rejected, _ = tailor.validate_rewrites(edit.rewrites, set(kept), by_id, tailor.tech_vocab(bank))
    texts = {i: t for i, t in {**texts, **new_texts}.items() if i in kept}
    changed = kept != record.get("kept", []) or texts != record.get("rewrites_accepted", {})
    reply = edit.reply
    if rejected:
        reply += "\n\n(Kept original wording for " + ", ".join(rejected) + ": the change would have added facts "\
                 "that aren't in your bullet: " + "; ".join(p for ps in rejected.values() for p in ps) + ")"
    cov = None
    if changed and kept:
        state = review.ReviewState(kept=kept, texts=texts)
        cov = review.rerender(out_dir, record, profile, bank, state, threshold).coverage_pct
    return ResumeChatResult(reply, changed, rejected, cov)


# ---- "fix the app" chat: Claude Code with file tools inside this project ----

APP_SYSTEM = """You are working on AutoApply, the user's internship auto-apply tool (Python). The user is chatting with you
from its local website and wants something fixed or changed. Read claude.md for the spec and conventions.
- Make the change, then run the tests: .venv/Scripts/python.exe -m pytest -q
- Keep the project's rules: never invent resume facts; a human approves every submission.
- Don't commit, push, or touch git history. Don't submit job applications or run `apply`, `go`, `batch`, `watch`.
- Don't edit data/profile.json, data/bullets.json, data/answers.json or config.local.yaml unless the user explicitly asks.
- Finish with a short plain-English summary of what you changed and whether tests pass."""

ALLOWED_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(.venv/Scripts/python.exe -m pytest:*)",
                 "Bash(.venv/Scripts/python.exe main.py discover --dry-run:*)", "Bash(git status:*)",
                 "Bash(git diff:*)"]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NO_WINDOW).stdout


def changed_files() -> set[str]:
    return {line[3:].strip() for line in _git("status", "--porcelain").splitlines() if line.strip()}


@dataclass
class AppChatResult:
    reply: str
    session_id: str | None
    files: list[str]
    cost_usd: float | None = None


def app_chat(message: str, session_id: str | None = None, model: str = "sonnet", timeout: int = 900) -> AppChatResult:
    exe = find_exe("claude", "AUTOAPPLY_CLAUDE")
    if not exe:
        raise RuntimeError("`claude` CLI not found")
    before = changed_files()
    cmd = [exe, "-p", message, "--output-format", "json", "--model", model,
           "--append-system-prompt", APP_SYSTEM, "--permission-mode", "acceptEdits",
           "--allowedTools", *ALLOWED_TOOLS]
    if session_id:
        cmd += ["--resume", session_id]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude failed: {proc.stderr[-1500:] or proc.stdout[-1500:]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {out.get('result')}")
    return AppChatResult(out.get("result") or "", out.get("session_id"),
                         sorted(changed_files() - before), out.get("total_cost_usd"))


def undo(files: list[str]) -> list[str]:
    """Revert files a chat turn changed: tracked files back to the last commit, new untracked files deleted."""
    tracked = set(_git("ls-files").splitlines())
    undone = []
    for f in files:
        path = (ROOT / f).resolve()
        if ROOT not in path.parents:
            continue
        if f in tracked:
            _git("checkout", "--", f)
            undone.append(f)
        elif path.is_file():
            path.unlink()
            undone.append(f)
    return undone
