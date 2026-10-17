"""Fetch a posting's JD text and application questions from public ATS APIs."""

from __future__ import annotations

import html
import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.discover.simplify import detect_ats

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}?questions=true"


@dataclass
class Question:
    """One form field, ATS-agnostic."""
    name: str
    label: str
    type: str  # text | textarea | file | select | multiselect | hidden
    required: bool
    options: list[str] = field(default_factory=list)
    section: str = "questions"  # questions | location | eeo


@dataclass
class Posting:
    ats: str
    url: str
    title: str
    company: str
    location: str
    jd_text: str
    questions: list[Question]
    raw: dict[str, Any] = field(default_factory=dict)


def html_to_text(raw_html: str) -> str:
    t = html.unescape(raw_html or "")
    t = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", t, flags=re.I)
    t = re.sub(r"<li[^>]*>", "- ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def parse_greenhouse_url(url: str) -> tuple[str, str]:
    """(board, job_id) from boards.greenhouse.io / job-boards.greenhouse.io URLs or ?for=&token= embeds."""
    u = urlparse(url)
    m = re.search(r"/([^/]+)/jobs/(\d+)", u.path)
    if m:
        return m.group(1), m.group(2)
    qs = parse_qs(u.query)
    if "for" in qs and "token" in qs:
        return qs["for"][0], qs["token"][0]
    raise ValueError(f"not a recognizable Greenhouse job URL: {url}")


_GH_TYPES = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
    "input_hidden": "hidden",
}


def _gh_questions(items: list[dict], section: str) -> list[Question]:
    out = []
    for q in items or []:
        fields = q.get("fields") or []
        if not fields:
            continue
        f = fields[0]  # Resume/Cover Letter carry a file field first and a text fallback second
        out.append(Question(
            name=f["name"],
            label=re.sub(r"\s+", " ", html_to_text(q.get("label") or "")).strip(),
            type=_GH_TYPES.get(f.get("type", ""), f.get("type", "")),
            required=bool(q.get("required")),
            options=[v["label"] for v in f.get("values") or []],
            section=section,
        ))
    return out


def _fetch_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "autoapply"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_greenhouse(url: str) -> Posting:
    board, job = parse_greenhouse_url(url)
    d = _fetch_json(GREENHOUSE_API.format(board=board, job=job))
    questions = _gh_questions(d.get("questions"), "questions")
    questions += _gh_questions(d.get("location_questions"), "location")
    for block in d.get("compliance") or []:
        questions += _gh_questions(block.get("questions"), "eeo")
    jd = f"{d.get('title', '')}\n{(d.get('location') or {}).get('name', '')}\n\n{html_to_text(d.get('content', ''))}"
    return Posting(
        ats="greenhouse",
        url=d.get("absolute_url") or url,
        title=d.get("title", ""),
        company=d.get("company_name", ""),
        location=(d.get("location") or {}).get("name", ""),
        jd_text=jd,
        questions=questions,
        raw=d,
    )


def fetch_posting(url: str) -> Posting:
    ats = detect_ats(url)
    if ats == "greenhouse":
        return fetch_greenhouse(url)
    raise NotImplementedError(f"ingest for {ats!r} postings isn't built yet ({url})")
