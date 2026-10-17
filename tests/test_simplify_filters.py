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


# ---- extra "season" feeds and cross-list dedupe ----

@pytest.mark.parametrize(
    "title,cat",
    [
        ("Software Engineer Intern, Backend & API", "swe"),
        ("Software Engineer Intern, ML Ops", "ml"),
        ("Data Scientist Intern", "ml"),
        ("LLM Post-training Engineer Intern", "ml"),
        ("Equity Volatility Quant Researcher Intern", "quant"),
        ("Embedded Firmware Intern", "hardware"),
        ("Associate Product Manager Intern", "pm"),
        ("Linux Engineer Intern", "swe"),
    ],
)
def test_category_from_title(title, cat):
    assert simplify.category_from_title(title) == cat


def test_adapt_season_listing(cfg):
    raw = {"id": "v1", "company_name": "Physical Reasoning", "title": "Software Engineering Intern",
           "season": "Spring/Summer", "active": True, "is_visible": True, "url": "https://x.ai/job/1",
           "locations": ["San Francisco, CA", "Toronto, Canada"], "sponsorship": "Offers Sponsorship",
           "date_posted": 1792261825}
    adapted = simplify.adapt_season_listing(raw)  # posted Oct 2026
    assert adapted["terms"] == ["Spring 2027", "Summer 2027"]
    listing = simplify.normalize(adapted)
    assert listing.category == "swe"
    assert simplify.filter_reason(listing, cfg) is None
    winter = simplify.normalize(simplify.adapt_season_listing({**raw, "season": "Winter"}))
    assert simplify.filter_reason(winter, cfg) == "term"


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://jobs.lever.co/acme/123?utm_source=Simplify&ref=Simplify", "https://jobs.lever.co/acme/123/"),
        ("https://www.acme.com/careers/1?gh_jid=55&utm_campaign=x", "https://acme.com/careers/1?gh_jid=55"),
        ("http://boards.greenhouse.io/acme/jobs/9?gh_src=abc", "https://boards.greenhouse.io/acme/jobs/9"),
    ],
)
def test_url_key_matches_same_posting(a, b):
    from src import tracker
    assert tracker.url_key(a) == tracker.url_key(b)


def test_url_key_keeps_meaningful_params():
    from src import tracker
    assert tracker.url_key("https://acme.icims.com/jobs/1/job?mode=job") != tracker.url_key("https://acme.icims.com/jobs/1/job")


def test_run_dedupes_across_lists(cfg, tmp_path):
    from src import tracker
    conn = tracker.connect(tmp_path / "t.db")
    base = {"id": "s1", "source": "Simplify", "category": "Software", "company_name": "Acme",
            "title": "Software Engineer Intern", "active": True, "is_visible": True, "terms": ["Summer 2027"],
            "date_posted": 1790000000, "url": "https://jobs.lever.co/acme/1?utm_source=Simplify",
            "locations": ["Austin, TX"], "sponsorship": "Other", "degrees": []}
    stats = simplify.run(cfg, conn, listings=[base, {**base, "id": "v1", "url": "https://jobs.lever.co/acme/1"}])
    assert stats["new"] == 1 and stats["drops"]["duplicate"] == 1
    # the other list's copy showing up later is still not new
    stats = simplify.run(cfg, conn, listings=[{**base, "id": "v2", "url": "https://jobs.lever.co/acme/1/"}])
    assert stats["new"] == 0


def _ts(y, m):
    from datetime import datetime, timezone
    return int(datetime(y, m, 15, tzinfo=timezone.utc).timestamp())


@pytest.mark.parametrize(
    "season,title,posted,year",
    [
        ("Summer", "SWE Intern", _ts(2026, 4), 2026),   # spring posting = this summer
        ("Summer", "SWE Intern", _ts(2026, 8), 2027),   # recruiting season = next summer
        ("Fall", "SWE Co-op", _ts(2026, 7), 2026),
        ("Fall", "SWE Co-op", _ts(2026, 10), 2027),
        ("Spring", "SWE Co-op", _ts(2026, 10), 2027),
        ("Winter", "SWE Co-op", _ts(2026, 3), 2026),
        ("Summer", "Software Intern - Summer 2028", _ts(2026, 8), 2028),  # year in title wins
        ("Summer", "SWE Intern", None, None),
    ],
)
def test_season_year(season, title, posted, year):
    assert simplify.season_year(season, title, posted) == year


def test_old_season_postings_are_filtered(cfg):
    raw = {"id": "v2", "company_name": "Old Co", "title": "Software Engineer Intern", "season": "Summer",
           "active": True, "is_visible": True, "url": "https://old.co/job", "locations": ["Austin, TX"],
           "date_posted": _ts(2026, 4)}
    listing = simplify.normalize(simplify.adapt_season_listing(raw))
    assert listing.terms == ["Summer 2026"] and simplify.filter_reason(listing, cfg) == "term"
