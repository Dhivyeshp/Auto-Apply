import shutil

import pytest

from src import render, schemas

needs_latex = pytest.mark.skipif(
    not (shutil.which("tectonic") or shutil.which("pdflatex")), reason="no LaTeX engine"
)


def test_escape_latex():
    assert render.escape_latex("R&D 100% $5 C# a_b ~x") == r"R\&D 100\% \$5 C\# a\_b \textasciitilde{}x"
    assert render.escape_latex("June 2026 – Aug 2026") == "June 2026 -- Aug 2026"


def test_bullet_bold_and_rewrite():
    b = schemas.Bullet(id="x", text="Cut costs 30% with zero-downtime deploys", bold=["30%"], priority=1)
    assert render.bullet_latex(b) == r"Cut costs \textbf{30\%} with zero-downtime deploys"
    # a rewrite that dropped the phrase just renders without bold
    assert render.bullet_latex(b, "Reduced costs with zero-downtime deploys") == "Reduced costs with zero-downtime deploys"


def test_build_context_filters_and_drops_empty_entries():
    profile, bank = schemas.load_profile(), schemas.load_bullets()
    ctx = render.build_context(profile, bank, include={"ibm-1", "echo-1"})
    assert [e["org_line"] for e in ctx["experience"]] == ["IBM (watsonx.ai Data Refinery)"]
    assert [p["name"] for p in ctx["projects"]] == ["Echo Agents"]
    assert len(ctx["skills"]) == 5


def test_template_renders_without_leftover_delimiters():
    profile, bank = schemas.load_profile(), schemas.load_bullets()
    tex = render.render_tex(render.build_context(profile, bank))
    assert "(((" not in tex and "((*" not in tex
    assert r"\textbf{12 global}" in tex


@needs_latex
def test_fit_one_page_drops_lowest_ranked(tmp_path):
    profile = schemas.load_profile()
    long = "Built and operated a distributed data processing service handling requests across many regions " * 2
    bullets = [{"id": f"b{i}", "text": long.strip(), "priority": 2} for i in range(40)]
    bank = schemas.BulletBank.model_validate({
        "experience": [{"id": "e", "org": "Org", "title": "Engineer", "location": "Dallas, TX",
                        "dates": "2026", "bullets": bullets}],
        "skills": {"languages": ["Python"]},
        "skill_labels": {"languages": "Languages"},
    })
    ranked = [b["id"] for b in bullets]
    result = render.fit_one_page(profile, bank, ranked, tmp_path)
    assert result.pages == 1
    assert result.dropped, "40 long bullets should not fit on one page"
    assert result.kept == ranked[: len(result.kept)]
    assert result.dropped == list(reversed(ranked[len(result.kept):]))


@needs_latex
def test_base_resume_fits_one_page(tmp_path):
    profile, bank = schemas.load_profile(), schemas.load_bullets()
    ranked = [b.id for b in bank.all_bullets()]
    result = render.fit_one_page(profile, bank, ranked, tmp_path)
    assert result.pages == 1 and not result.dropped
