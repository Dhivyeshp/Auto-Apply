"""Pull SimplifyJobs listings.json, filter by config.yaml, queue new matches in the tracker."""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from src import tracker

REQUIRED_FIELDS = ("id", "url", "title")

CATEGORY_ALIASES = {
    "software": "swe",
    "software engineering": "swe",
    "ai/ml/data": "ml",
    "data science, ai & machine learning": "ml",
    "hardware": "hardware",
    "hardware engineering": "hardware",
    "product": "pm",
    "product management": "pm",
    "quant": "quant",
    "quantitative finance": "quant",
}

ATS_DOMAINS = {
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "myworkdayjobs.com": "workday",
}

ADVANCED_DEGREES = {"Master's", "PhD", "MBA"}

US_STATE_CODES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire",
    "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia", "puerto rico",
}
US_NAMES = {"united states", "usa", "us", "u.s.", "remote", "remote in usa", "remote in us",
            "remote in united states"}
US_SHORTHANDS = {"nyc", "sf", "la", "south sf", "dc", "bay area"}


class Listing(BaseModel):
    listing_id: str
    source: str = ""
    company: str
    role: str
    url: str
    ats: str
    category: str | None  # normalized, None if unrecognized
    raw_category: str = ""
    terms: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    sponsorship: str = ""
    degrees: list[str] = Field(default_factory=list)
    active: bool = True
    is_visible: bool = True
    date_posted: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


def fetch_listings(urls: list[str], timeout: int = 60, etag_file: Path | None = None) -> list[dict[str, Any]] | None:
    """Fetch the first working source. With `etag_file`, returns None when the feed hasn't changed."""
    etags = json.loads(etag_file.read_text(encoding="utf-8")) if etag_file and etag_file.exists() else {}
    errors = []
    for url in urls:
        headers = {"User-Agent": "autoapply-discovery"}
        if etag_file and etags.get(url):
            headers["If-None-Match"] = etags[url]
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                etag = resp.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None
            errors.append(f"{url}: {e}")
            continue
        except Exception as e:  # try the next source
            errors.append(f"{url}: {e}")
            continue
        _check_schema(data, url)
        if etag_file and etag:
            etag_file.parent.mkdir(parents=True, exist_ok=True)
            etag_file.write_text(json.dumps({**etags, url: etag}), encoding="utf-8")
        return data
    raise RuntimeError("could not fetch listings from any source:\n  " + "\n  ".join(errors))


def _check_schema(data: Any, url: str) -> None:
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RuntimeError(f"{url}: expected a non-empty JSON list of objects, got {type(data).__name__}")
    missing = [f for f in REQUIRED_FIELDS if f not in data[0]]
    if missing:
        raise RuntimeError(
            f"{url}: listing schema changed, missing {missing}; fields present: {sorted(data[0])}"
        )


def detect_ats(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for domain, name in ATS_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return "other"


def normalize(raw: dict[str, Any]) -> Listing:
    raw_cat = (raw.get("category") or "").strip()
    return Listing(
        listing_id=str(raw["id"]),
        source=raw.get("source") or "",
        company=(raw.get("company_name") or "").strip(),
        role=(raw.get("title") or "").strip(),
        url=raw["url"].strip(),
        ats=detect_ats(raw["url"]),
        category=CATEGORY_ALIASES.get(raw_cat.lower()),
        raw_category=raw_cat,
        terms=raw.get("terms") or [],
        locations=raw.get("locations") or [],
        sponsorship=raw.get("sponsorship") or "",
        degrees=raw.get("degrees") or [],
        active=bool(raw.get("active", True)),
        is_visible=bool(raw.get("is_visible", True)),
        date_posted=raw.get("date_posted"),
        raw=raw,
    )


def is_us_location(loc: str) -> bool:
    s = loc.strip().lower()
    if s in US_NAMES or s in US_SHORTHANDS or s in US_STATE_NAMES:
        return True
    parts = [p.strip() for p in loc.split(",")]
    last = parts[-1]
    if len(parts) >= 2 and last.upper() in US_STATE_CODES and len(last) == 2:
        return True
    if len(parts) >= 2 and last.lower() in (US_STATE_NAMES | {"united states", "usa", "us"}):
        return True
    return False


def _word_match(terms: list[str], text: str) -> bool:
    return any(re.search(rf"(?<![\w-]){re.escape(t)}s?(?![\w-])", text, re.I) for t in terms)


def is_preferred(listing: Listing, cfg: dict[str, Any]) -> bool:
    prefs = cfg.get("locations", {}).get("preferred", [])
    return any(_word_match(prefs, loc) for loc in listing.locations)


def filter_reason(listing: Listing, cfg: dict[str, Any]) -> str | None:
    """Return why a listing is dropped, or None if it should be kept."""
    if not listing.active:
        return "inactive"
    if not listing.is_visible:
        return "hidden"
    if not set(listing.terms) & set(cfg.get("terms", [])):
        return "term"
    if not listing.category or not cfg.get("categories", {}).get(listing.category, False):
        return "category"
    if listing.degrees and set(listing.degrees) <= ADVANCED_DEGREES:
        return "advanced_degree_only"
    title_cfg = cfg.get("title", {})
    if title_cfg.get("include") and not _word_match(title_cfg["include"], listing.role):
        return "title_include"
    if title_cfg.get("exclude") and _word_match(title_cfg["exclude"], listing.role):
        return "title_exclude"
    if cfg.get("locations", {}).get("us_only", True) and listing.locations:
        if not any(is_us_location(loc) for loc in listing.locations):
            return "non_us"
    sp = cfg.get("sponsorship", {})
    if sp.get("exclude_no_sponsorship") and listing.sponsorship == "Does Not Offer Sponsorship":
        return "no_sponsorship"
    if sp.get("exclude_citizenship_required") and listing.sponsorship == "U.S. Citizenship is Required":
        return "citizenship_required"
    return None


def to_job(listing: Listing, cfg: dict[str, Any]) -> dict[str, Any]:
    posted = None
    if listing.date_posted:
        posted = datetime.fromtimestamp(listing.date_posted, timezone.utc).date().isoformat()
    return {
        "listing_id": listing.listing_id,
        "source": listing.source,
        "company": listing.company,
        "role": listing.role,
        "url": listing.url,
        "ats": listing.ats,
        "category": listing.category,
        "terms": listing.terms,
        "locations": listing.locations,
        "sponsorship": listing.sponsorship,
        "degrees": listing.degrees,
        "preferred": int(is_preferred(listing, cfg)),
        "date_posted": posted,
        "raw_json": listing.raw,
    }


def run(
    cfg: dict[str, Any],
    conn: sqlite3.Connection | None,
    dry_run: bool = False,
    listings: list[dict[str, Any]] | None = None,
    etag_file: Path | None = None,
) -> dict[str, Any]:
    """Fetch, filter, and queue. Returns stats plus the kept/new jobs."""
    raw_listings = listings if listings is not None else fetch_listings(cfg["source"]["urls"], etag_file=etag_file)
    if raw_listings is None:  # feed unchanged since the last fetch
        return {"total": 0, "kept": 0, "new": 0, "drops": {}, "kept_jobs": [], "new_jobs": [], "unchanged": True}
    drops: Counter[str] = Counter()
    kept: list[dict[str, Any]] = []
    new: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for raw in raw_listings:
        try:
            listing = normalize(raw)
        except Exception:
            drops["malformed"] += 1
            continue
        reason = filter_reason(listing, cfg)
        if reason:
            drops[reason] += 1
            continue
        if listing.url in seen_urls:
            drops["duplicate_in_feed"] += 1
            continue
        seen_urls.add(listing.url)
        job = to_job(listing, cfg)
        kept.append(job)
        if not dry_run and conn is not None and tracker.insert_job(conn, job):
            new.append(job)
    return {
        "total": len(raw_listings),
        "kept": len(kept),
        "new": len(new),
        "drops": dict(drops.most_common()),
        "kept_jobs": kept,
        "new_jobs": new,
    }
