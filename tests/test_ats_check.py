import shutil

import pytest

from src import ats_check, render, schemas

TEXT = ("Automated microservice validation in Python on Kubernetes. Built CI/CD with GitHub Actions. "
        "Open source contributor. Languages: C++, TypeScript, Go")


@pytest.mark.parametrize(
    "term,hit",
    [
        ("python", True),
        ("Microservices", True),        # plural in JD, singular in resume
        ("CI/CD", True),
        ("open-source", True),          # hyphen vs space
        ("C/C++", True),                # alternative match via C++
        ("Typescript/Javascript", True),
        ("Go", True),
        ("Rust", False),
        ("C", False),                   # must not match the C in C++
        ("Java", False),                # must not match inside JavaScript-free text
        ("GitHub Actions", True),
        ("Kubernetes", True),
        ("React", False),
    ],
)
def test_matches(term, hit):
    assert ats_check.matches(term, TEXT) is hit


def test_coverage_numbers_and_threshold():
    cov = ats_check.coverage(TEXT, ["Python", "python", "Rust", "Go", "React"], ["Python", "Rust"], threshold=60)
    assert cov.matched == ["Python", "Go"]
    assert cov.missing == ["Rust", "React"]
    assert cov.coverage_pct == 50.0 and cov.below_threshold
    assert cov.must_have_pct == 50.0 and cov.must_have_missing == ["Rust"]


def test_empty_keywords_is_full_coverage():
    assert ats_check.coverage(TEXT, []).coverage_pct == 100.0


@pytest.mark.skipif(not (shutil.which("tectonic") or shutil.which("pdflatex")), reason="no LaTeX engine")
def test_rendered_pdf_text_is_parseable(tmp_path):
    profile, bank = schemas.load_profile(), schemas.load_bullets()
    pdf = render.compile_pdf(render.render_tex(render.build_context(profile, bank)), tmp_path)
    cov = ats_check.check_pdf(pdf, ["Instana", "Data Refinery", "workflows", "confidence", "Kubernetes"])
    assert cov.coverage_pct == 100.0, cov.missing  # ligatures (fi/fl) must survive extraction
