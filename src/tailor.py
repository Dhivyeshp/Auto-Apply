"""Tailor the resume to a JD via headless Claude Code: extract → select → rewrite → render."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

from src import ats_check, render
from src.schemas import BulletBank, Profile
from src.tools import NO_WINDOW, find_exe

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompts"

SYSTEM_PROMPT = (
    "You are a precise resume-tailoring assistant. Follow the task instructions exactly "
    "and answer only with the requested structured output."
)

T = TypeVar("T", bound=BaseModel)


# ---- step outputs ----

class JDAnalysis(BaseModel):
    must_have: list[str]
    nice_to_have: list[str]
    keywords: list[str]
    role_category: Literal["swe", "ml", "fde", "data", "pm_adjacent", "hardware"]
    summary: str


class BulletScore(BaseModel):
    id: str
    score: int = Field(ge=0, le=100)
    reason: str


class Selection(BaseModel):
    scores: list[BulletScore]


class Rewrite(BaseModel):
    id: str
    text: str


class Rewrites(BaseModel):
    rewrites: list[Rewrite]


class Improvement(BaseModel):
    selected: list[str]  # bullet ids, most important first
    rewrites: list[Rewrite] = Field(default_factory=list)
    note: str = ""


class Rating(BaseModel):
    score: int = Field(ge=1, le=10)
    verdict: str
    strengths: list[str] = Field(max_length=3)
    gaps: list[str] = Field(max_length=3)


def rate(runner: Runner, jd_text: str, pdf: Path) -> Rating:
    """Recruiter-style 1-10 fit score for the rendered resume against the JD."""
    payload = {"jd": jd_text[:12000], "resume": ats_check.pdf_text(pdf)}
    return call(runner, "rate.md", json.dumps(payload), Rating)


# ---- claude -p runner ----

Runner = Callable[[str, str, dict], dict]


def claude_runner(model: str = "sonnet", timeout: int = 300) -> Runner:
    """Run `claude -p` headless with tools off and a short system prompt; return structured output."""
    exe = find_exe("claude", "AUTOAPPLY_CLAUDE")
    if not exe:
        raise RuntimeError("`claude` CLI not found: install Claude Code, or set AUTOAPPLY_CLAUDE to claude.exe's path")

    def run(prompt: str, stdin_text: str, schema: dict) -> dict:
        cmd = [
            exe, "-p", prompt,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", SYSTEM_PROMPT,
            "--tools", "",
            "--no-session-persistence",
            "--model", model,
        ]
        # empty cwd so no project CLAUDE.md gets pulled into context
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                cmd, input=stdin_text, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd, creationflags=NO_WINDOW,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p failed ({proc.returncode}): {proc.stderr[-2000:] or proc.stdout[-2000:]}")
        envelope = json.loads(proc.stdout)
        if envelope.get("is_error"):
            raise RuntimeError(f"claude -p error: {envelope.get('result')}")
        out = envelope.get("structured_output")
        if out is None:
            out = json.loads(envelope.get("result") or "null")
        return out

    return run


def call(runner: Runner, prompt_file: str, stdin_text: str, model: type[T]) -> T:
    """One structured call, validated with pydantic; retries once on bad output."""
    prompt = (PROMPTS / prompt_file).read_text(encoding="utf-8")
    schema = model.model_json_schema()
    last: Exception | None = None
    for _ in range(2):
        try:
            return model.model_validate(runner(prompt, stdin_text, schema))
        except (ValidationError, json.JSONDecodeError, TypeError) as e:
            last = e
    raise RuntimeError(f"{prompt_file}: invalid output after retry: {last}")


# ---- no-invention check for rewrites ----

_NUM_RE = re.compile(r"\d[\d,.]*\+?%?")
_TOKEN_RE = re.compile(r"[A-Za-z][\w.+#/&-]*[\w+#]|[A-Za-z]")


def _nums(text: str) -> set[str]:
    return {n.rstrip(".,") for n in _NUM_RE.findall(text)}


def check_rewrite(original: str, new: str, vocab: set[str]) -> list[str]:
    """Return violations; empty means the rewrite keeps the original's facts."""
    problems = []
    new = new.strip()
    if not new:
        return ["empty"]
    added, lost = _nums(new) - _nums(original), _nums(original) - _nums(new)
    if added:
        problems.append(f"new numbers {sorted(added)}")
    if lost:
        problems.append(f"dropped numbers {sorted(lost)}")
    orig_lower = original.lower()
    for i, tok in enumerate(_TOKEN_RE.findall(new)):
        proper = any(c.isupper() for c in tok[1:]) or (i > 0 and tok[0].isupper())
        if proper and tok.lower() not in orig_lower:
            problems.append(f"new proper noun {tok!r}")
    for term in vocab:
        pat = rf"(?<![\w]){re.escape(term.lower())}(?![\w])"
        if re.search(pat, new.lower()) and not re.search(pat, orig_lower):
            problems.append(f"new tech {term!r}")
    if len(new) > len(original) * 1.15 + 5:
        problems.append("too long")
    return problems


def validate_rewrites(rewrites: list[Rewrite], allowed: set[str], by_id: dict, vocab: set[str]
                      ) -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
    """(accepted texts, rejected problems, all proposals). Rejected rewrites keep the original text."""
    texts, rejected, proposed = {}, {}, {}
    for rw in rewrites:
        if rw.id not in by_id or rw.id not in allowed:
            continue
        original = by_id[rw.id].text
        new = rw.text.strip().rstrip(".")
        proposed[rw.id] = new
        if new == original:
            continue
        problems = check_rewrite(original, new, vocab)
        if problems:
            rejected[rw.id] = problems
        else:
            texts[rw.id] = new
    return texts, rejected, proposed


def tech_vocab(bank: BulletBank) -> set[str]:
    vocab = {t for b in bank.all_bullets() for t in b.tech}
    vocab |= {s for items in bank.skills.values() for s in items}
    return {v for v in vocab if len(v) > 1}


# ---- pipeline ----

@dataclass
class TailorResult:
    out_dir: Path
    pdf: Path
    analysis: JDAnalysis
    ranked: list[str]
    kept: list[str]
    dropped: list[str]
    texts: dict[str, str]
    rejected: dict[str, list[str]]
    coverage: ats_check.Coverage
    rating: Rating | None = None


def _bullet_sections(bank: BulletBank) -> dict[str, str]:
    out = {}
    for name, entries in (("experience", bank.experience), ("projects", bank.projects), ("leadership", bank.leadership)):
        for e in entries:
            for b in e.bullets:
                out[b.id] = name
    return out


def rank_bullets(bank: BulletBank, scores: list[BulletScore], max_bullets: int, min_score: int) -> list[str]:
    """Best-first bullet ids: relevant priority-1 bullets first, then by score, capped at max_bullets."""
    by_id = {b.id: b for b in bank.all_bullets()}
    score = {s.id: s.score for s in scores if s.id in by_id}
    relevant = [i for i, s in score.items() if s >= min_score]
    relevant.sort(key=lambda i: (by_id[i].priority != 1, -score[i], by_id[i].priority))
    return relevant[:max_bullets]


def resolve_pdf_name(template: str | None, types: dict[str, str] | None, category: str | None) -> str | None:
    """'Dhivyesh_Resume_{type}' + job category → 'Dhivyesh_Resume_ML'. Unknown categories fall back to SWE."""
    if not template:
        return None
    label = (types or {}).get(category or "", "SWE")
    return template.replace("{type}", label)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "x"


def tailor(
    jd_text: str,
    company: str,
    role: str,
    profile: Profile,
    bank: BulletBank,
    runner: Runner,
    out_root: Path = ROOT / "output",
    max_bullets: int = 15,
    min_score: int = 20,
    rewrite: bool = True,
    coverage_threshold: float = 50.0,
    job_id: int | None = None,
    with_rating: bool = True,
    pdf_name: str | None = None,
    pdf_types: dict[str, str] | None = None,
    target_rating: int | None = None,
    improve_rounds: int = 2,
) -> TailorResult:
    # job id in the name keeps two same-titled jobs (tailored in parallel) from sharing a folder
    suffix = f"_{job_id}" if job_id is not None else ""
    out_dir = out_root / f"{slug(company)}_{slug(role)}_{date.today().isoformat()}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "jd.txt").write_text(jd_text, encoding="utf-8")

    analysis = call(runner, "extract.md", jd_text, JDAnalysis)

    sections = _bullet_sections(bank)
    payload = {
        "jd": analysis.model_dump(),
        "bullets": [
            {"id": b.id, "section": sections[b.id], "text": b.text, "tags": b.tags, "tech": b.tech}
            for b in bank.all_bullets()
        ],
    }
    selection = call(runner, "select.md", json.dumps(payload), Selection)
    ranked = rank_bullets(bank, selection.scores, max_bullets, min_score)

    by_id = {b.id: b for b in bank.all_bullets()}
    vocab = tech_vocab(bank)
    texts: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    proposed: dict[str, str] = {}
    if rewrite and ranked:
        req = {"keywords": analysis.keywords, "bullets": [{"id": i, "text": by_id[i].text} for i in ranked]}
        result = call(runner, "rewrite.md", json.dumps(req), Rewrites)
        texts, rejected, proposed = validate_rewrites(result.rewrites, set(ranked), by_id, vocab)

    fit = render.fit_one_page(profile, bank, ranked, out_dir, texts=texts)
    cov = ats_check.check_pdf(fit.pdf, analysis.keywords, analysis.must_have, coverage_threshold)
    rating = rate(runner, jd_text, fit.pdf) if with_rating else None

    # Improvement rounds: aim for target_rating using only real bullets and checked rephrasings.
    attempts = [{"round": 0, "score": rating.score if rating else None, "coverage": cov.coverage_pct}]
    best_round = 0
    if rating and target_rating and rating.score < target_rating:
        cur_ranked, cur_texts, cur_rating, cur_cov = ranked, texts, rating, cov
        for n in range(1, improve_rounds + 1):
            req = {
                "jd": jd_text[:12000],
                "keywords": analysis.keywords,
                "rating": cur_rating.model_dump(),
                "selected": cur_ranked,
                "max_bullets": max_bullets,
                "bullets": [{"id": b.id, "section": sections[b.id], "original": b.text,
                             "current": cur_texts.get(b.id, b.text)} for b in bank.all_bullets()],
            }
            imp = call(runner, "improve.md", json.dumps(req), Improvement)
            new_ranked = [i for i in dict.fromkeys(imp.selected) if i in by_id][: max_bullets + 3]
            if not new_ranked:
                break
            new_texts, bad, prop = validate_rewrites(imp.rewrites, set(new_ranked), by_id, vocab)
            # keep earlier accepted rewrites for bullets this round didn't touch
            new_texts = {**{i: t for i, t in cur_texts.items() if i in new_ranked}, **new_texts}
            rejected.update(bad)
            proposed.update(prop)
            attempt_dir = out_dir / "attempts" / str(n)
            a_fit = render.fit_one_page(profile, bank, new_ranked, attempt_dir, texts=new_texts)
            a_cov = ats_check.check_pdf(a_fit.pdf, analysis.keywords, analysis.must_have, coverage_threshold)
            a_rating = rate(runner, jd_text, a_fit.pdf)
            attempts.append({"round": n, "score": a_rating.score, "coverage": a_cov.coverage_pct,
                             "note": imp.note})
            if (a_rating.score, a_cov.coverage_pct) > (cur_rating.score, cur_cov.coverage_pct):
                cur_ranked, cur_texts, cur_rating, cur_cov = new_ranked, new_texts, a_rating, a_cov
                fit, best_round = a_fit, n
            if cur_rating.score >= target_rating:
                break
        if best_round:
            for name in ("resume.pdf", "resume.tex"):
                shutil.copyfile(out_dir / "attempts" / str(best_round) / name, out_dir / name)
            fit = render.FitResult(out_dir / "resume.pdf", fit.pages, fit.kept, fit.dropped)
            ranked, texts, rating, cov = cur_ranked, cur_texts, cur_rating, cur_cov

    named = resolve_pdf_name(pdf_name, pdf_types, analysis.role_category)
    render.named_copy(fit.pdf, named)

    (out_dir / "tailoring.json").write_text(json.dumps({
        "job_id": job_id,
        "pdf_name": named,
        "company": company,
        "role": role,
        "analysis": analysis.model_dump(),
        "scores": [s.model_dump() for s in selection.scores],
        "ranked": ranked,
        "kept": fit.kept,
        "dropped_to_fit": fit.dropped,
        "rewrites_accepted": texts,
        "rewrites_rejected": {i: {"proposed": proposed[i], "problems": p} for i, p in rejected.items()},
        "original_text": {i: by_id[i].text for i in fit.kept},
        "ats": cov.to_dict(),
        "rating": rating.model_dump() if rating else None,
        "target_rating": target_rating,
        "attempts": attempts,
        "best_round": best_round,
    }, indent=2), encoding="utf-8")

    return TailorResult(out_dir, fit.pdf, analysis, ranked, fit.kept, fit.dropped, texts, rejected, cov, rating)
