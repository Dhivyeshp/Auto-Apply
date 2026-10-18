import json

import pytest

from src import schemas, tailor
from src.tools import find_exe

needs_latex = pytest.mark.skipif(
    not (find_exe("tectonic") or find_exe("pdflatex")), reason="no LaTeX engine"
)

VOCAB = {"Kubernetes", "Python", "AWS", "Kafka", "Docker"}


@pytest.mark.parametrize(
    "new",
    [
        "Developed CI/CD with GitHub Actions and Docker/Kubernetes, cutting provisioning time 30% with zero-downtime deploys",
        "Cut provisioning time 30% with zero-downtime deploys by building CI/CD on GitHub Actions and Docker/Kubernetes",
    ],
)
def test_rewrite_accepts_truthful_rephrase(new):
    orig = "Built CI/CD with GitHub Actions and Docker/Kubernetes, cutting provisioning time 30% with zero-downtime deploys"
    assert tailor.check_rewrite(orig, new, VOCAB) == []


@pytest.mark.parametrize(
    "new,problem",
    [
        ("Built Python services on Kubernetes serving 500+ users", "new numbers"),
        ("Built Python services on Kubernetes for many users", "dropped numbers"),
        ("Built Python services on Kubernetes and Kafka serving 300+ users", "new tech"),
        ("Built Python services on Kubernetes at Google serving 300+ users", "new proper noun"),
        ("Built Python microservices with gRPC on Kubernetes serving 300+ users", "new proper noun"),
        ("Built Python services on kubernetes with docker serving 300+ users", "new tech"),
        ("", "empty"),
        ("Built Python services on Kubernetes serving 300+ users " + "with care " * 10, "too long"),
    ],
)
def test_rewrite_rejects_invention(new, problem):
    orig = "Built Python services on Kubernetes serving 300+ users"
    problems = tailor.check_rewrite(orig, new, VOCAB)
    assert any(p.startswith(problem) for p in problems), problems


def test_rank_bullets_priority_then_score():
    bank = schemas.load_bullets()
    scores = [tailor.BulletScore(id=b.id, score=50, reason="") for b in bank.all_bullets()]
    scores = [s if s.id != "acm-2" else tailor.BulletScore(id="acm-2", score=95, reason="") for s in scores]
    scores = [s if s.id != "ibm-4" else tailor.BulletScore(id="ibm-4", score=5, reason="") for s in scores]
    ranked = tailor.rank_bullets(bank, scores, max_bullets=12, min_score=20)
    p1 = {b.id for b in bank.all_bullets() if b.priority == 1}
    assert set(ranked[: len(p1)]) == p1           # relevant priority-1 bullets lead
    assert ranked[len(p1)] == "acm-2"             # then highest score
    assert "ibm-4" not in ranked                  # below min_score
    assert len(ranked) == 12


def test_rank_ignores_unknown_ids():
    bank = schemas.load_bullets()
    ranked = tailor.rank_bullets(bank, [tailor.BulletScore(id="made-up", score=99, reason="")], 15, 20)
    assert ranked == []


class FakeRunner:
    """Stands in for `claude -p`: answers by prompt file, can return bad output first."""

    def __init__(self, rewrites, bad_first=False, rating=None):
        self.calls = []
        self.rewrites = rewrites
        self.bad_first = bad_first
        self.rating = rating or {"score": 7, "verdict": "Solid backend fit.",
                                 "strengths": ["Monitoring across 12 clouds"], "gaps": ["No Go experience"]}
        self.rate_input = None

    def __call__(self, prompt, stdin_text, schema):
        self.calls.append(prompt[:40])
        if "analyze a job description" in prompt:
            if self.bad_first and len(self.calls) == 1:
                return {"keywords": "not a list"}
            return {"must_have": ["Python"], "nice_to_have": ["Kubernetes"],
                    "keywords": ["Python", "observability", "CI/CD"], "role_category": "swe",
                    "summary": "Platform team intern."}
        if "score resume bullets" in prompt:
            bullets = json.loads(stdin_text)["bullets"]
            return {"scores": [{"id": b["id"], "score": 90 if b["id"].startswith("ibm") else 40, "reason": "x"}
                               for b in bullets]}
        if "rephrase resume bullets" in prompt:
            return {"rewrites": self.rewrites}
        if "technical recruiter" in prompt:
            self.rate_input = json.loads(stdin_text)
            return self.rating
        raise AssertionError(prompt)


@pytest.mark.parametrize(
    "rating",
    [
        {"score": 11, "verdict": "x", "strengths": [], "gaps": []},
        {"score": 0, "verdict": "x", "strengths": [], "gaps": []},
        {"score": 5, "verdict": "x", "strengths": ["a", "b", "c", "d"], "gaps": []},
    ],
)
def test_rating_rejects_out_of_range(rating):
    with pytest.raises(Exception):
        tailor.Rating.model_validate(rating)


def test_call_retries_once_on_bad_output():
    runner = FakeRunner([], bad_first=True)
    analysis = tailor.call(runner, "extract.md", "jd", tailor.JDAnalysis)
    assert analysis.role_category == "swe" and len(runner.calls) == 2


@needs_latex
def test_tailor_end_to_end_with_fake_claude(tmp_path):
    profile, bank = schemas.load_profile(), schemas.load_bullets()
    rewrites = [
        # truthful: accepted
        {"id": "ibm-3", "text": "Automated microservice and data-path validation in Python, replacing manual testing across cloud environments"},
        {"id": "ibm-1", "text": "Developed an Instana synthetic monitoring engine running API health checks across 12 global AWS/IBM Cloud environments"},
        # invents a metric: rejected, original kept
        {"id": "nrve-1", "text": "Shipped a personalized education app on a Python/Firebase backend serving 5,000+ beta users"},
    ]
    runner = FakeRunner(rewrites)
    result = tailor.tailor("We want Python interns.", "Acme Corp", "SWE Intern", profile, bank,
                           runner=runner, out_root=tmp_path, max_bullets=12)
    assert result.pdf.exists()
    assert result.rating.score == 7
    # the rating sees the final rendered resume, rewrites included
    assert "Developed an Instana" in runner.rate_input["resume"]
    assert runner.rate_input["jd"] == "We want Python interns."
    assert result.texts == {"ibm-1": rewrites[1]["text"]}  # ibm-3 unchanged, so not an override
    assert "nrve-1" in result.rejected
    record = json.loads((result.out_dir / "tailoring.json").read_text(encoding="utf-8"))
    assert record["rewrites_rejected"]["nrve-1"]["problems"][0].startswith("new numbers")
    assert record["rating"]["score"] == 7 and record["rating"]["gaps"] == ["No Go experience"]
    assert (result.out_dir / "jd.txt").read_text(encoding="utf-8") == "We want Python interns."
    assert result.out_dir.name.startswith("acme-corp_swe-intern_")


@pytest.mark.parametrize(
    "category,expected",
    [("swe", "Dhivyesh_Resume_SWE"), ("ml", "Dhivyesh_Resume_ML"), ("data", "Dhivyesh_Resume_ML"),
     ("fde", "Dhivyesh_Resume_SWE"), ("something_new", "Dhivyesh_Resume_SWE"), (None, "Dhivyesh_Resume_SWE")],
)
def test_resolve_pdf_name(category, expected):
    types = {"swe": "SWE", "fde": "SWE", "ml": "ML", "data": "ML"}
    assert tailor.resolve_pdf_name("Dhivyesh_Resume_{type}", types, category) == expected


def test_resolve_pdf_name_fixed_and_empty():
    assert tailor.resolve_pdf_name("My_Resume", {"ml": "ML"}, "ml") == "My_Resume"
    assert tailor.resolve_pdf_name(None, {}, "ml") is None
