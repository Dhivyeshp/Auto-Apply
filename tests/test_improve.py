import json

import pytest

from src import schemas, tailor
from src.tools import find_exe

needs_latex = pytest.mark.skipif(not (find_exe("tectonic") or find_exe("pdflatex")), reason="no LaTeX engine")

ECHO1 = "Architected a FastAPI orchestration backend on PostgreSQL and Redis running concurrent multi-agent workflows"


class Runner:
    """Fake Claude: rates attempts from a script; proposes a fixed improvement."""

    def __init__(self, scores, improvement):
        self.scores = list(scores)
        self.improvement = improvement
        self.calls = []

    def __call__(self, prompt, stdin_text, schema):
        if "analyze a job description" in prompt:
            self.calls.append("extract")
            return {"must_have": ["Python"], "nice_to_have": [], "keywords": ["Python", "agents"],
                    "role_category": "ml", "summary": "Agent platform intern."}
        if "score resume bullets" in prompt:
            self.calls.append("select")
            bullets = json.loads(stdin_text)["bullets"]
            return {"scores": [{"id": b["id"], "score": 80 if b["id"].startswith("ibm") else 30, "reason": ""}
                               for b in bullets]}
        if "rephrase resume bullets" in prompt:
            self.calls.append("rewrite")
            return {"rewrites": []}
        if "technical recruiter" in prompt:
            self.calls.append("rate")
            score = self.scores.pop(0)
            return {"score": score, "verdict": f"scored {score}", "strengths": [], "gaps": ["no agent work shown"]}
        if "You improve a one-page" in prompt:
            self.calls.append("improve")
            self.improve_input = json.loads(stdin_text)
            return self.improvement
        raise AssertionError(prompt[:60])


def run(runner, tmp_path, **kw):
    return tailor.tailor("We build AI agents in Python.", "Acme", "AI Intern", schemas.load_profile(),
                         schemas.load_bullets(), runner=runner, out_root=tmp_path, max_bullets=8,
                         job_id=5, **kw)


@needs_latex
def test_reaches_target_and_keeps_improved_version(tmp_path):
    improvement = {"selected": ["echo-1", "echo-2", "ibm-1", "ibm-2", "ibm-3"],
                   "rewrites": [{"id": "echo-1", "text": "Architected a FastAPI orchestration backend running "
                                                         "concurrent multi-agent workflows on PostgreSQL and Redis"}],
                   "note": "led with agent work"}
    runner = Runner([6, 9], improvement)
    res = run(runner, tmp_path, target_rating=9, improve_rounds=2)
    assert res.rating.score == 9
    assert runner.calls.count("improve") == 1  # stopped once the target was hit
    assert res.ranked[0] == "echo-1" and "echo-1" in res.texts
    record = json.loads((res.out_dir / "tailoring.json").read_text(encoding="utf-8"))
    assert [a["score"] for a in record["attempts"]] == [6, 9] and record["best_round"] == 1
    # the final resume.pdf is the improved attempt
    assert (res.out_dir / "resume.pdf").read_bytes() == (res.out_dir / "attempts" / "1" / "resume.pdf").read_bytes()
    # improve saw the whole bank (not just the selected bullets) and the recruiter's gaps
    assert len(runner.improve_input["bullets"]) == 17 and runner.improve_input["rating"]["gaps"]
    assert res.out_dir.name.endswith("_5")  # job id keeps same-titled jobs apart


@needs_latex
def test_no_improvement_keeps_original(tmp_path):
    runner = Runner([7, 6, 5], {"selected": ["acm-2", "montecore-1"], "rewrites": [], "note": "x"})
    res = run(runner, tmp_path, target_rating=9, improve_rounds=2)
    assert res.rating.score == 7 and runner.calls.count("improve") == 2
    record = json.loads((res.out_dir / "tailoring.json").read_text(encoding="utf-8"))
    assert record["best_round"] == 0 and [a["score"] for a in record["attempts"]] == [7, 6, 5]


@needs_latex
def test_invented_rewrite_rejected_in_improvement(tmp_path):
    improvement = {"selected": ["echo-1", "ibm-1"],
                   "rewrites": [{"id": "echo-1", "text": ECHO1 + " serving 10,000 users with Kafka"}], "note": ""}
    runner = Runner([5, 8], improvement)
    res = run(runner, tmp_path, target_rating=9, improve_rounds=1)
    assert "echo-1" not in res.texts and "echo-1" in res.rejected


@needs_latex
def test_already_at_target_skips_improvement(tmp_path):
    runner = Runner([9], {"selected": [], "rewrites": [], "note": ""})
    res = run(runner, tmp_path, target_rating=9)
    assert "improve" not in runner.calls and res.rating.score == 9
