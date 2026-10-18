"""Jinja → LaTeX → PDF, with a loop that drops low-ranked bullets until the resume fits 1 page."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import jinja2
import pdfplumber

from src.schemas import Bullet, BulletBank, Profile
from src.tools import NO_WINDOW, find_exe

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates"

_LATEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "–": "--",
    "—": "---",
}
_LATEX_RE = re.compile("|".join(re.escape(k) for k in _LATEX_SPECIAL))


def escape_latex(text: str) -> str:
    return _LATEX_RE.sub(lambda m: _LATEX_SPECIAL[m.group()], text)


def bullet_latex(bullet: Bullet, text: str | None = None) -> str:
    """Escape a bullet and bold its emphasized phrases (skipped if a rewrite dropped them)."""
    text = text if text is not None else bullet.text
    out = escape_latex(text)
    for phrase in bullet.bold:
        esc = escape_latex(phrase)
        if esc in out:
            out = out.replace(esc, rf"\textbf{{{esc}}}", 1)
    return out


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        block_start_string="((*",
        block_end_string="*))",
        variable_start_string="(((",
        variable_end_string=")))",
        comment_start_string="((=",
        comment_end_string="=))",
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
        autoescape=False,
    )


def _contact_line(profile: Profile) -> str:
    parts = [
        escape_latex(profile.location),
        rf"\href{{mailto:{profile.email}}}{{{escape_latex(profile.email)}}}",
        escape_latex(profile.phone),
    ]
    for url in (profile.links.linkedin, profile.links.github, profile.links.portfolio):
        if url:
            label = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
            label = label.replace("linkedin.com/", "")  # matches the "in/handle" style
            parts.append(rf"\href{{{url}}}{{{escape_latex(label)}}}")
    return " $|$ ".join(parts)


def build_context(
    profile: Profile,
    bank: BulletBank,
    include: set[str] | None = None,
    texts: dict[str, str] | None = None,
) -> dict:
    """Template context. `include` limits bullets by id (None = all); `texts` overrides bullet text."""
    texts = texts or {}

    def pick(bullets: list[Bullet]) -> list[str]:
        return [bullet_latex(b, texts.get(b.id)) for b in bullets if include is None or b.id in include]

    experience = []
    for e in bank.experience:
        bullets = pick(e.bullets)
        if bullets:
            org_line = e.org_line or (f"{e.org} ({e.team})" if e.team else e.org)
            experience.append({
                "title": escape_latex(e.title),
                "dates": escape_latex(e.dates),
                "org_line": escape_latex(org_line),
                "location": escape_latex(e.location),
                "bullets": bullets,
            })

    projects = []
    for p in bank.projects:
        bullets = pick(p.bullets)
        if bullets:
            projects.append({
                "name": escape_latex(p.name),
                "tech_line": escape_latex(", ".join(p.tech_line)),
                "dates": escape_latex(p.dates),
                "bullets": bullets,
            })

    return {
        "name": escape_latex(profile.name),
        "contact_line": _contact_line(profile),
        "education": [
            {
                "school": escape_latex(ed.school),
                "location": escape_latex(ed.location),
                "degree": escape_latex(ed.degree),
                "graduation": escape_latex(f"Graduating {ed.graduation}"),
                "coursework": escape_latex(", ".join(ed.coursework)),
                "leadership": escape_latex(", ".join(ed.leadership)),
            }
            for ed in profile.education
        ],
        "experience": experience,
        "projects": projects,
        "skills": [
            {"label": escape_latex(bank.skill_labels.get(k, k)), "list": escape_latex(", ".join(v))}
            for k, v in bank.skills.items()
            if v
        ],
    }


def render_tex(context: dict) -> str:
    return _env().get_template("resume.tex").render(**context)


def _latex_cmd(tex: Path, out_dir: Path) -> list[str]:
    tectonic = find_exe("tectonic", "AUTOAPPLY_TECTONIC")
    if tectonic:
        return [tectonic, "--chatter", "minimal", "--outdir", str(out_dir), str(tex)]
    pdflatex = find_exe("pdflatex")
    if pdflatex:
        return [pdflatex, "-interaction=nonstopmode", "-halt-on-error", f"-output-directory={out_dir}", str(tex)]
    raise RuntimeError("no LaTeX engine found: install tectonic (or pdflatex) or set AUTOAPPLY_TECTONIC")


def compile_pdf(tex_source: str, out_dir: Path, name: str = "resume") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    tex = out_dir / f"{name}.tex"
    tex.write_text(tex_source, encoding="utf-8")
    proc = subprocess.run(_latex_cmd(tex, out_dir), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NO_WINDOW)
    pdf = out_dir / f"{name}.pdf"
    if proc.returncode != 0 or not pdf.exists():
        raise RuntimeError(f"LaTeX compile failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    return pdf


def named_copy(pdf: Path, name: str | None) -> Path:
    """Copy resume.pdf to <name>.pdf beside it (the file employers see); returns the path to upload."""
    if not name:
        return pdf
    dest = pdf.with_name(f"{name}.pdf")
    shutil.copyfile(pdf, dest)
    return dest


def page_count(pdf: Path) -> int:
    with pdfplumber.open(pdf) as doc:
        return len(doc.pages)


@dataclass
class FitResult:
    pdf: Path
    pages: int
    kept: list[str]
    dropped: list[str] = field(default_factory=list)


def fit_one_page(
    profile: Profile,
    bank: BulletBank,
    ranked_ids: list[str],
    out_dir: Path,
    texts: dict[str, str] | None = None,
    min_bullets: int = 6,
) -> FitResult:
    """Render with `ranked_ids` (best first); drop the lowest-ranked bullet until it fits 1 page."""
    known = {b.id for b in bank.all_bullets()}
    unknown = [i for i in ranked_ids if i not in known]
    if unknown:
        raise ValueError(f"unknown bullet ids: {unknown}")
    kept = list(ranked_ids)
    dropped: list[str] = []
    while True:
        ctx = build_context(profile, bank, include=set(kept), texts=texts)
        pdf = compile_pdf(render_tex(ctx), out_dir)
        pages = page_count(pdf)
        if pages <= 1:
            return FitResult(pdf=pdf, pages=pages, kept=kept, dropped=dropped)
        if len(kept) <= min_bullets:
            raise RuntimeError(f"still {pages} pages with only {len(kept)} bullets; check the template")
        dropped.append(kept.pop())
