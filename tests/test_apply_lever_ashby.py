from pathlib import Path

import pytest

from src import ingest, schemas
from src.apply import plan

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def profile():
    return schemas.load_profile()


@pytest.fixture
def no_saved():
    return {"how_did_you_hear": [], "by_label": {}}


# ---- ingest ----

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://jobs.lever.co/CesiumAstro/6a953cbc-af69-452c-b357-1a0a3db80bbf/apply",
         ("api.lever.co", "CesiumAstro", "6a953cbc-af69-452c-b357-1a0a3db80bbf")),
        ("https://jobs.eu.lever.co/cirrus/46a841b3-e3ce-4c78-b010-0e9cfe9e0f8e/apply",
         ("api.eu.lever.co", "cirrus", "46a841b3-e3ce-4c78-b010-0e9cfe9e0f8e")),
    ],
)
def test_parse_lever_url(url, expected):
    assert ingest.parse_lever_url(url) == expected


def test_parse_ashby_url():
    url = "https://jobs.ashbyhq.com/saronic/60afb634-5515-4347-824a-3816735541c2/application?embed=true"
    assert ingest.parse_ashby_url(url) == ("saronic", "60afb634-5515-4347-824a-3816735541c2")


def test_parse_lever_form():
    qs = {q.name: q for q in ingest.parse_lever_form((FIXTURES / "lever_form.html").read_text(encoding="utf-8"))}
    assert qs["name"].key == "full_name" and qs["name"].required
    assert qs["resume"].type == "file" and qs["resume"].required
    assert not qs["phone"].required
    assert qs["urls[LinkedIn]"].label == "LinkedIn URL"
    assert qs["cards[c1][field0]"].widget == "radio" and qs["cards[c1][field0]"].options == ["Yes", "No"]
    assert qs["cards[c1][field1]"].widget == "native_select" and qs["cards[c1][field1]"].required
    assert qs["cards[c1][field2]"].type == "multiselect"
    assert qs["cards[c1][field4]"].type == "unsupported"
    assert qs["eeo[race]"].widget == "radio" and qs["eeo[race]"].key == "race"
    assert qs["eeo[veteran]"].options == ["I am a Protected Veteran", "I am not a Protected Veteran"]


def test_ashby_questions():
    posting = {
        "applicationForm": {"sections": [{"fieldEntries": [
            {"isRequired": True, "field": {"path": "_systemfield_name", "type": "String", "title": "Name"}},
            {"isRequired": True, "field": {"path": "p1", "type": "Phone", "title": "Phone"}},
            {"isRequired": True, "field": {"path": "p2", "type": "Boolean", "title": "U.S. Person?"}},
            {"isRequired": True, "field": {"path": "p3", "type": "ValueSelect", "title": "Degree",
                                           "selectableValues": [{"label": "Bachelor's"}, {"label": "Master's"}]}},
            {"isRequired": True, "field": {"path": "_systemfield_education_history", "type": "EducationHistory",
                                           "title": "College/University"}},
            {"isRequired": False, "field": {"path": "p4", "type": "Score", "title": "Weird"}},
        ]}]},
        "surveyForms": [{"sections": [{"fieldEntries": [
            {"isRequired": False, "field": {"path": "_systemfield_eeoc_race", "type": "ValueSelect", "title": "Race",
                                            "selectableValues": [{"label": "Asian (Not Hispanic or Latino)"}]}}]}]}],
    }
    qs = {q.name: q for q in ingest.ashby_questions(posting)}
    assert qs["_systemfield_name"].key == "full_name"
    assert qs["p1"].key == "phone"
    assert qs["p2"].widget == "boolean" and qs["p2"].options == ["Yes", "No"]
    assert qs["p3"].widget == "radio"
    assert qs["_systemfield_education_history"].widget == "school"
    assert qs["p4"].type == "unsupported"
    assert qs["_systemfield_eeoc_race"].section == "eeo" and qs["_systemfield_eeoc_race"].key == "race"


# ---- new answer rules ----

def q(label, type="select", options=(), widget="", key=None, required=True):
    return ingest.Question("x", label, type, required, list(options), key=key, widget=widget)


@pytest.mark.parametrize(
    "question,expected",
    [
        (q("Name", type="text", key="full_name"), "Dhivyesh Prithiviraj"),
        (q("Major/Field of Study", type="text"), "Computer Engineering"),
        (q("Expected Graduation Month", options=["April/May/June", "August/September", "December"]), "August/September"),
        (q("Expected Graduation Year", options=["2026", "2027", "2028"]), "2027"),
        (q("The role requires U.S. Person status. Please confirm you are a U.S. Person.", options=["Yes", "No"],
           widget="boolean"), "Yes"),
        (q("Race", options=["White (Not Hispanic or Latino)", "Asian (Not Hispanic or Latino)"], key="race"),
         "Asian (Not Hispanic or Latino)"),
        (q("Disability", options=["Yes, I have a disability, or have had one in the past",
                                  "No, I do not have a disability and have not had one in the past"], key="disability"),
         "No, I do not have a disability and have not had one in the past"),
        (q("Are you related to any current employees?", options=["Yes", "No"], widget="boolean"), None),
    ],
)
def test_rules(profile, no_saved, question, expected):
    [a] = plan.resolve([question], profile, None, no_saved)
    assert a.value == expected


def test_unsupported_required_goes_to_you(profile, no_saved):
    [a] = plan.resolve([q("Transcript", type="unsupported", widget="file-upload")], profile, None, no_saved)
    assert a.source == "you" and "browser" in a.note


def test_lever_extras_sign_disability_form(profile):
    extras = plan.page_extras(profile, "lever")
    assert extras["eeo[disabilitySignature]"] == "Dhivyesh Prithiviraj"
    assert len(extras["eeo[disabilitySignatureDate]"]) == 10


# ---- fillers against local copies of each form ----

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def browser():
    try:
        with playwright.sync_playwright() as p:
            b = p.chromium.launch()
            yield b
            b.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium not installed")
        raise


def ans(name, type, value, widget=""):
    return plan.Answer(name=name, label=name, type=type, required=True, value=value, source="profile", widget=widget)


def test_lever_fill(browser, tmp_path):
    from src.apply import lever

    page = browser.new_page()
    page.goto((FIXTURES / "lever_form.html").as_uri())
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    answers = [
        ans("resume", "file", str(resume)),
        ans("name", "text", "Dhivyesh Prithiviraj"),
        ans("cards[c1][field0]", "select", "No", "radio"),
        ans("cards[c1][field1]", "select", "3.9", "native_select"),
        ans("cards[c1][field2]", "multiselect", ["Summer 2027"], "checkbox"),
        ans("cards[c1][field3]", "textarea", "Because."),
        ans("eeo[race]", "select", "Asian (Not Hispanic or Latino)", "radio"),
        ans("eeo[veteran]", "select", "I am not a Protected Veteran", "native_select"),
    ]
    results = lever.fill_form(page, answers, extra={"eeo[disabilitySignature]": "Dhivyesh Prithiviraj"})
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    assert page.input_value('[name="name"]') == "Dhivyesh Prithiviraj"
    assert page.is_checked('[name="cards[c1][field0]"][value="No"]')
    assert page.input_value('[name="cards[c1][field1]"]') == "3.9"
    assert page.is_checked('[name="cards[c1][field2]"][value="Summer 2027"]')
    assert not page.is_checked('[name="cards[c1][field2]"][value="Spring 2027"]')
    assert page.is_checked('[name="eeo[race]"][value="Asian (Not Hispanic or Latino)"]')
    assert page.input_value('[name="eeo[veteran]"]') == "I am not a Protected Veteran"
    assert page.input_value('[name="eeo[disabilitySignature]"]') == "Dhivyesh Prithiviraj"


def test_ashby_fill(browser):
    from src.apply import ashby

    page = browser.new_page()
    page.goto((FIXTURES / "ashby_form.html").as_uri())
    answers = [
        ans("_systemfield_name", "text", "Dhivyesh Prithiviraj"),
        ans("_systemfield_education_history", "text", "University of Texas at Dallas", "school"),
        ans("deg", "select", "Bachelor's", "radio"),
        ans("teams", "multiselect", ["Systems Software"], "checkbox"),
        ans("usperson", "select", "Yes", "boolean"),
        ans("related", "select", "No", "boolean"),
        ans("why", "textarea", "Because."),
    ]
    results = ashby.fill_form(page, answers)
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    assert page.input_value("#_systemfield_name") == "Dhivyesh Prithiviraj"
    assert page.input_value('[role="combobox"]') == "The University of Texas at Dallas"
    assert page.is_checked("#f1_deg-labeled-radio-0")
    assert page.is_checked("#f1_teams-labeled-checkbox-1") and not page.is_checked("#f1_teams-labeled-checkbox-0")
    # each Yes/No click lands in its own question
    assert page.get_attribute('[name="usperson"]', "data-answer") == "Yes"
    assert page.get_attribute('[name="related"]', "data-answer") == "No"
