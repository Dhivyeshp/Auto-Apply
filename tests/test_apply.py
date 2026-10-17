import json
from pathlib import Path

import pytest

from src import schemas
from src.apply import plan
from src.ingest import Question, html_to_text, parse_greenhouse_url

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_form.html"


@pytest.fixture
def profile():
    return schemas.load_profile()


@pytest.fixture
def no_saved():
    return {"how_did_you_hear": ["Job Board", "Online", "Other"], "how_did_you_hear_detail": "SimplifyJobs", "by_label": {}}


def q(label, type="select", options=(), required=True, name=None):
    return Question(name=name or "question_" + plan.norm(label).replace(" ", "_")[:20], label=label,
                    type=type, required=required, options=list(options))


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/cloudflare/jobs/8199958", ("cloudflare", "8199958")),
        ("https://job-boards.greenhouse.io/spacex/jobs/8621757002?gh_src=x", ("spacex", "8621757002")),
        ("https://boards.greenhouse.io/embed/job_app?for=acme&token=123", ("acme", "123")),
    ],
)
def test_parse_greenhouse_url(url, expected):
    assert parse_greenhouse_url(url) == expected


def test_html_to_text():
    assert html_to_text("<p>Hi&nbsp;there</p><ul><li>One</li><li>Two</li></ul>") == "Hi there\n- One\n- Two"


@pytest.mark.parametrize(
    "options,wanted,expected",
    [
        (["Yes", "No"], ["yes"], "Yes"),
        (["Decline To Self Identify", "Female", "Male"], ["Male"], "Male"),  # not "Female"
        (["Bachelor's", "Master's", "PhD"], ["Bachelor's"], "Bachelor's"),
        (["June 2027", "December 2027"], ["Aug 2027"], None),
        (["Option A", "Option B"], ["Option"], None),  # ambiguous
    ],
)
def test_match_option(options, wanted, expected):
    assert plan.match_option(options, *wanted) == expected


def test_profile_and_eeo(profile, no_saved):
    qs = [Question("first_name", "First Name", "text", True, key="first_name"),
          Question("phone", "Phone", "text", True, key="phone"),
          Question("resume", "Resume/CV", "file", True, key="resume"),
          Question("gender", "Gender", "select", False, ["Decline To Self Identify", "Female", "Male"], "eeo", "gender"),
          Question("veteran_status", "VeteranStatus", "select", False,
                   ["I don't wish to answer", "I identify as one or more of the classifications of a protected veteran",
                    "I am not a protected veteran"], "eeo", "veteran")]
    ans = {a.name: a for a in plan.resolve(qs, profile, Path("r.pdf"), no_saved)}
    assert ans["first_name"].value == "Dhivyesh"
    assert ans["resume"].value == "r.pdf"
    assert ans["gender"].value == "Male"
    assert ans["veteran_status"].value == "I am not a protected veteran"


@pytest.mark.parametrize(
    "question,expected",
    [
        (q("Do you now or will you in the future require immigration sponsorship?", options=["Yes", "No"]), "No"),
        (q("Are you a student in F-1 status who plans to work pursuant to CPT?", options=["Yes", "No"]), "No"),
        (q("Are you legally authorized to work in the United States?",
           options=["I am authorized to work in the United States for any employer",
                    "I require sponsorship to work in the United States"]),
         "I am authorized to work in the United States for any employer"),
        (q("Citizenship Status", options=["(a) U.S. citizen or national of the United States",
                                          "(b) U.S. lawful permanent resident"]),
         "(a) U.S. citizen or national of the United States"),
        (q("Are you willing to relocate?", options=["Yes", "No"]), "Yes"),
        (q("Please select the month you will be able to start your internship.",
           options=["April", "May", "June"]), "May"),
        (q("What degree are you currently pursuing?", options=["Bachelor's", "Master's"]), "Bachelor's"),
        (q("What is your expected graduation year?", options=["2026", "2027", "2028"]), "2027"),
        (q("When do you expect to graduate?", options=["June 2027", "December 2027"]), None),
        (q("How did you hear about this job?",
           options=["College/University Career Fair or Career Website", "Other conferences or events",
                    "Other (none of the above)"]), "Other (none of the above)"),
        (q("GPA (Undergraduate)", options=["3.0-3.4", "3.5-4.0"]), None),
    ],
)
def test_rules(profile, no_saved, question, expected):
    [a] = plan.resolve([question], profile, None, no_saved)
    assert a.value == expected
    assert a.source == ("you" if expected is None else "rule")


def test_please_specify_after_other(profile, no_saved):
    qs = [q("How did you hear about this job?", options=["LinkedIn", "Other (indicate below)"]),
          q("Please specify", type="text", required=False)]
    a, b = plan.resolve(qs, profile, None, no_saved)
    assert a.value == "Other (indicate below)" and b.value == "SimplifyJobs"


def test_saved_answers_and_remember(tmp_path, profile):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"how_did_you_hear": [], "by_label": {}}), encoding="utf-8")
    plan.remember("GPA (Undergraduate)", "3.5-4.0", path)
    [a] = plan.resolve([q("GPA  (Undergraduate)", options=["3.0-3.4", "3.5-4.0"])], profile, None, plan.load_answers(path))
    assert a.value == "3.5-4.0" and a.source == "saved"


def test_optional_unknowns_are_skipped(profile, no_saved):
    [a] = plan.resolve([q("Preferred First Name", type="text", required=False)], profile, None, no_saved)
    assert a.source == "skip"


class FakeRunner:
    def __init__(self, answers):
        self.answers = answers
        self.payload = None

    def __call__(self, prompt, stdin_text, schema):
        self.payload = json.loads(stdin_text)
        return {"answers": self.answers}


def test_draft_unresolved_validates_options(profile, no_saved):
    qs = [q("Area of interest", options=["Backend/Systems", "Full-stack", "Frontend"], name="area"),
          q("Office preference", options=["Austin", "SF"], name="office"),
          q("Why us?", type="textarea", name="why"),
          q("Which terms?", type="multiselect", options=["Spring 2027", "Summer 2027"], name="terms"),
          q("GPA", options=["3.0-3.4", "3.5-4.0"], name="gpa")]
    answers = plan.resolve(qs, profile, None, no_saved)
    runner = FakeRunner([
        {"name": "area", "value": "backend/systems", "needs_human": False, "note": "IBM work"},
        {"name": "office", "value": "New York", "needs_human": False, "note": "bad option"},
        {"name": "why", "value": "I built monitoring at IBM.", "needs_human": False, "note": "resume"},
        {"name": "terms", "value": ["Summer 2027"], "needs_human": False, "note": "availability"},
        {"name": "gpa", "value": None, "needs_human": True, "note": "GPA not in profile"},
    ])
    out = {a.name: a for a in plan.draft_unresolved(answers, "JD", "Acme", plan.facts_for_claude(profile, schemas.load_bullets()), runner)}
    assert out["area"].value == "Backend/Systems" and out["area"].source == "claude"
    assert out["office"].value is None and out["office"].source == "you"
    assert out["why"].source == "claude"
    assert out["terms"].value == ["Summer 2027"]
    assert out["gpa"].source == "you" and "GPA" in out["gpa"].note
    assert "eeo" not in runner.payload["facts"]["profile"]  # EEO never sent to Claude


# ---- browser filling against a local copy of the form structure ----

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def page():
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            pg = browser.new_page()
            pg.goto(FIXTURE.as_uri())
            yield pg
            browser.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium not installed")
        raise


def ans(name, type, value, source="profile"):
    return plan.Answer(name=name, label=name, type=type, required=True, value=value, source=source)


def test_fill_form(page, tmp_path):
    from src.apply import greenhouse as gh

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    answers = [
        ans("first_name", "text", "Dhivyesh"),
        ans("location", "text", "Dallas, TX"),
        ans("resume", "file", str(resume)),
        ans("question_1", "select", "No"),
        ans("question_2", "textarea", "Because."),
        ans("question_3[]", "multiselect", ["Summer (May - September 2027)"]),
        ans("race", "select", "Asian"),
        ans("gender", "select", "Male"),
        ans("question_9", "text", None, source="you"),  # unanswered: left alone
    ]
    results = {r.name: r for r in gh.fill_form(page, answers, extra={"country": "United States", "hispanic_ethnicity": "No"})}
    assert all(r.ok for r in results.values()), [r for r in results.values() if not r.ok]
    assert "question_9" not in results
    assert page.input_value("#first_name") == "Dhivyesh"
    assert page.input_value("#country") == "United States +1"
    assert page.input_value("#candidate-location") == "Dallas, Texas, United States"
    assert page.input_value("#question_1") == "No"
    assert page.is_checked('[id="question_3[]_2"]') and not page.is_checked('[id="question_3[]_1"]')
    assert page.input_value("#race") == "Asian"  # revealed by answering Hispanic/Latino first
    assert page.input_value("#gender") == "Male"
    assert page.eval_on_selector("#resume", "e => e.files[0].name") == "resume.pdf"


def test_race_hidden_reported_not_crashed(page):
    from src.apply import greenhouse as gh

    [r] = gh.fill_form(page, [ans("race", "select", "Asian")], extra={"hispanic_ethnicity": None})
    assert not r.ok and "hidden" in r.error


def test_submit_detects_confirmation(page):
    from src.apply import common

    assert common.submit(page, wait_for_human=lambda m: None, timeout_s=10) is True
