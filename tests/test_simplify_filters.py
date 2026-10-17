import json
from pathlib import Path

import pytest
import yaml

from src.discover import simplify

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "listings_sample.json"


@pytest.fixture
def cfg():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def make(**over):
    raw = {
        "id": "abc",
        "source": "Simplify",
        "category": "Software",
        "company_name": "Acme ",
        "title": "Software Engineer Intern",
        "active": True,
        "is_visible": True,
        "terms": ["Summer 2027"],
        "date_posted": 1790000000,
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "locations": ["Austin, TX"],
        "sponsorship": "Other",
        "degrees": ["Bachelor's"],
    }
    raw.update(over)
    return simplify.normalize(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Software", "swe"),
        ("Software Engineering", "swe"),
        ("AI/ML/Data", "ml"),
        ("Data Science, AI & Machine Learning", "ml"),
        ("Hardware Engineering", "hardware"),
        ("Product Management", "pm"),
        ("Quantitative Finance", "quant"),
        ("Basket Weaving", None),
    ],
)
def test_category_aliases(raw, expected):
    assert make(category=raw).category == expected


@pytest.mark.parametrize(
    "url,ats",
    [
        ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://jobs.lever.co/acme/123", "lever"),
        ("https://jobs.ashbyhq.com/acme/123/application", "ashby"),
        ("https://acme.wd5.myworkdayjobs.com/x/job/1", "workday"),
        ("https://careers-lmi.icims.com/jobs/1", "other"),
        ("https://notgreenhouse.io.evil.com/x", "other"),
    ],
)
def test_detect_ats(url, ats):
    assert simplify.detect_ats(url) == ats


@pytest.mark.parametrize(
    "loc,us",
    [
        ("Austin, TX", True),
        ("NYC", True),
        ("SF", True),
        ("Remote in USA", True),
        ("Remote", True),
        ("United States", True),
        ("Texas", True),
        ("Washington, DC", True),
        ("Toronto, ON, Canada", False),
        ("London, UK", False),
        ("Remote in UK", False),
        ("Remote in Canada", False),
        ("Dublin, Ireland", False),
        ("Ontario, Canada", False),
    ],
)
def test_is_us_location(loc, us):
    assert simplify.is_us_location(loc) is us


def test_keeps_good_listing(cfg):
    assert simplify.filter_reason(make(), cfg) is None
    assert make().company == "Acme"


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"active": False}, "inactive"),
        ({"is_visible": False}, "hidden"),
        ({"terms": ["Summer 2026"]}, "term"),
        ({"terms": ["N/A"]}, "term"),
        ({"category": "Product"}, "category"),
        ({"category": "Quant"}, "category"),
        ({"category": "Unknown"}, "category"),
        ({"degrees": ["PhD"]}, "advanced_degree_only"),
        ({"degrees": ["Master's", "PhD"]}, "advanced_degree_only"),
        ({"title": "Software Engineer"}, "title_include"),
        ({"title": "Internal Tools Engineer"}, "title_include"),
        ({"title": "PhD Software Intern"}, "title_exclude"),
        ({"title": "Senior Intern Program Lead"}, "title_exclude"),
        ({"locations": ["Toronto, ON, Canada", "London, UK"]}, "non_us"),
    ],
)
def test_drop_reasons(cfg, over, reason):
    assert simplify.filter_reason(make(**over), cfg) == reason


@pytest.mark.parametrize("title", ["Software Co-op", "SWE Co-Op, Fall 2027", "Data Interns", "ML Internship"])
def test_title_include_variants(cfg, title):
    assert simplify.filter_reason(make(title=title), cfg) is None


def test_mixed_degrees_and_locations_kept(cfg):
    listing = make(degrees=["Bachelor's", "PhD"], locations=["Toronto, ON, Canada", "Seattle, WA"])
    assert simplify.filter_reason(listing, cfg) is None


def test_pm_toggle(cfg):
    cfg["categories"]["pm"] = True
    assert simplify.filter_reason(make(category="Product"), cfg) is None


def test_sponsorship_flags(cfg):
    nope = make(sponsorship="Does Not Offer Sponsorship")
    citizen = make(sponsorship="U.S. Citizenship is Required")
    assert simplify.filter_reason(nope, cfg) is None
    assert simplify.filter_reason(citizen, cfg) is None
    cfg["sponsorship"] = {"exclude_no_sponsorship": True, "exclude_citizenship_required": True}
    assert simplify.filter_reason(nope, cfg) == "no_sponsorship"
    assert simplify.filter_reason(citizen, cfg) == "citizenship_required"


def test_preferred(cfg):
    assert simplify.is_preferred(make(locations=["Austin, TX"]), cfg)
    assert simplify.is_preferred(make(locations=["Remote in USA"]), cfg)
    assert not simplify.is_preferred(make(locations=["Seattle, WA"]), cfg)


def test_schema_check_reports_fields():
    with pytest.raises(RuntimeError, match="fields present"):
        simplify._check_schema([{"name": "x"}], "u")


def test_run_on_fixture(cfg):
    listings = json.loads(FIXTURE.read_text(encoding="utf-8"))
    stats = simplify.run(cfg, conn=None, dry_run=True, listings=listings)
    assert stats["total"] == len(listings)
    assert stats["kept"] + sum(stats["drops"].values()) == stats["total"]
    for job in stats["kept_jobs"]:
        assert set(job["terms"]) & set(cfg["terms"])
        assert job["category"] in {"swe", "ml", "hardware"}
        assert any(simplify.is_us_location(loc) for loc in job["locations"])
