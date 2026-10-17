"""Fetch a posting's JD text and application questions from public ATS APIs."""

from __future__ import annotations

import html
import json
import re
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.discover.simplify import detect_ats

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}?questions=true"


@dataclass
class Question:
    """One form field, ATS-agnostic."""
    name: str
    label: str
    type: str  # text | textarea | file | select | multiselect | hidden | unsupported
    required: bool
    options: list[str] = field(default_factory=list)
    section: str = "questions"  # questions | location | eeo
    key: str | None = None  # what the field means: first_name, full_name, email, resume, gender, race, ...
    widget: str = ""  # how the ATS renders it, for the filler: radio, checkbox, boolean, native_select, school, ...


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


_GH_KEYS = {
    "first_name": "first_name", "last_name": "last_name", "email": "email", "phone": "phone",
    "location": "location", "resume": "resume", "cover_letter": "cover_letter",
    "gender": "gender", "race": "race", "veteran_status": "veteran", "disability_status": "disability",
}


def _clean_label(raw: str) -> str:
    return re.sub(r"\s+", " ", html_to_text(raw or "")).strip()


def _gh_questions(items: list[dict], section: str) -> list[Question]:
    out = []
    for q in items or []:
        fields = q.get("fields") or []
        if not fields:
            continue
        f = fields[0]  # Resume/Cover Letter carry a file field first and a text fallback second
        out.append(Question(
            name=f["name"],
            label=_clean_label(q.get("label")),
            type=_GH_TYPES.get(f.get("type", ""), f.get("type", "")),
            required=bool(q.get("required")),
            options=[v["label"] for v in f.get("values") or []],
            section=section,
            key=_GH_KEYS.get(f["name"]),
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


# ---- Lever: JD from the postings API, form from the apply page's HTML ----

def parse_lever_url(url: str) -> tuple[str, str, str]:
    """(api_host, company, posting_id); jobs.eu.lever.co maps to api.eu.lever.co."""
    u = urlparse(url)
    m = re.match(r"/([^/]+)/([0-9a-f-]{36})", u.path)
    if not m:
        raise ValueError(f"not a recognizable Lever job URL: {url}")
    api_host = "api.eu.lever.co" if (u.hostname or "").endswith("eu.lever.co") else "api.lever.co"
    return api_host, m.group(1), m.group(2)


def lever_apply_url(url: str) -> str:
    u = urlparse(url)
    _, company, pid = parse_lever_url(url)
    return f"https://{u.hostname}/{company}/{pid}/apply"


class _LeverForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[dict] = []
        self.selects: dict[str, list[str]] = {}
        self.radios: dict[str, list[str]] = {}
        self._select: str | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag in ("input", "textarea"):
            a["_tag"] = tag
            self.inputs.append(a)
            if a.get("type") == "radio":
                self.radios.setdefault(a.get("name", ""), []).append(a.get("value", ""))
        elif tag == "select":
            self._select = a.get("name", "")
            self.selects[self._select] = []
            self.inputs.append({"_tag": "select", **a})
        elif tag == "option" and self._select is not None and a.get("value"):
            self.selects[self._select].append(a["value"].strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None


_LEVER_STANDARD = {
    # name: (label, type, key)
    "resume": ("Resume/CV", "file", "resume"),
    "name": ("Full name", "text", "full_name"),
    "email": ("Email", "text", "email"),
    "phone": ("Phone", "text", "phone"),
    "location": ("Current location", "text", "location"),
    "org": ("Current company", "text", None),
}
_LEVER_CARD_TYPES = {
    "multiple-choice": ("select", "radio"),
    "dropdown": ("select", "native_select"),
    "multiple-select": ("multiselect", "checkbox"),
    "text": ("text", ""),
    "textarea": ("textarea", ""),
}
_LEVER_EEO = {"eeo[gender]": ("Gender", "gender"), "eeo[race]": ("Race", "race"),
              "eeo[veteran]": ("Veteran status", "veteran"), "eeo[disability]": ("Disability", "disability")}


def parse_lever_form(page_html: str) -> list[Question]:
    form = _LeverForm()
    form.feed(page_html)
    by_name = {i.get("name"): i for i in form.inputs if i.get("name")}
    out: list[Question] = []
    for name, (label, typ, key) in _LEVER_STANDARD.items():
        if name in by_name:
            out.append(Question(name, label, typ, "required" in by_name[name] or name in ("name", "email", "resume"),
                                key=key))
    for name, attrs in by_name.items():
        m = re.fullmatch(r"urls\[(.+)\]", name)
        if m:
            out.append(Question(name, f"{m.group(1)} URL", "text", "required" in attrs))
    for attrs in form.inputs:
        name = attrs.get("name", "")
        m = re.fullmatch(r"cards\[([^\]]+)\]\[baseTemplate\]", name)
        if not m:
            continue
        template = json.loads(html.unescape(attrs.get("value", "{}")))
        for i, f in enumerate(template.get("fields") or []):
            typ, widget = _LEVER_CARD_TYPES.get(f.get("type", ""), ("unsupported", f.get("type", "")))
            out.append(Question(
                name=f"cards[{m.group(1)}][field{i}]",
                label=_clean_label(f.get("text", "")),
                type=typ,
                required=bool(f.get("required")),
                options=[o.get("text", "").strip() for o in f.get("options") or []],
                widget=widget,
            ))
    for name, (label, key) in _LEVER_EEO.items():
        if name in form.selects:
            out.append(Question(name, label, "select", False, form.selects[name], "eeo", key, "native_select"))
        elif name in form.radios:
            out.append(Question(name, label, "select", False, form.radios[name], "eeo", key, "radio"))
    return out


def fetch_lever(url: str) -> Posting:
    api_host, company, pid = parse_lever_url(url)
    d = _fetch_json(f"https://{api_host}/v0/postings/{company}/{pid}")
    req = urllib.request.Request(lever_apply_url(url), headers={"User-Agent": "Mozilla/5.0 autoapply"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    sections = "\n\n".join(
        f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}" for lst in d.get("lists") or []
    )
    jd = (f"{d.get('text', '')}\n{(d.get('categories') or {}).get('location', '')}\n\n"
          f"{d.get('descriptionPlain', '')}\n\n{sections}\n\n{d.get('additionalPlain', '')}")
    return Posting(
        ats="lever",
        url=d.get("hostedUrl") or url,
        title=d.get("text", ""),
        company=company,
        location=(d.get("categories") or {}).get("location", ""),
        jd_text=jd.strip(),
        questions=parse_lever_form(page),
        raw=d,
    )


# ---- Ashby: JD and form from the public job-board GraphQL endpoint ----

ASHBY_GQL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
_ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id title descriptionHtml locationName
    applicationForm { sections { title fieldEntries { ... on FormFieldEntry { id field isRequired } } } }
    surveyForms { sections { title fieldEntries { ... on FormFieldEntry { id field isRequired } } } }
  }
}"""
_ASHBY_TYPES = {
    "String": ("text", ""), "Email": ("text", ""), "Phone": ("text", ""), "Number": ("text", ""),
    "LongText": ("textarea", ""), "File": ("file", ""),
    "Boolean": ("select", "boolean"), "ValueSelect": ("select", "radio"),
    "MultiValueSelect": ("multiselect", "checkbox"), "EducationHistory": ("text", "school"),
    "Location": ("text", "location"),
}
_ASHBY_KEYS = {
    "_systemfield_name": "full_name", "_systemfield_email": "email", "_systemfield_resume": "resume",
    "_systemfield_eeoc_gender": "gender", "_systemfield_eeoc_race": "race",
    "_systemfield_eeoc_veteran_status": "veteran", "_systemfield_eeoc_disability_status": "disability",
}


def parse_ashby_url(url: str) -> tuple[str, str]:
    m = re.match(r"/([^/]+)/([0-9a-f-]{36})", urlparse(url).path)
    if not m:
        raise ValueError(f"not a recognizable Ashby job URL: {url}")
    return m.group(1), m.group(2)


def ashby_questions(posting: dict) -> list[Question]:
    out = []
    forms = [("questions", posting.get("applicationForm"))]
    forms += [("eeo", s) for s in posting.get("surveyForms") or []]
    for section, form in forms:
        for sec in (form or {}).get("sections") or []:
            for entry in sec.get("fieldEntries") or []:
                f = entry.get("field") or {}
                typ, widget = _ASHBY_TYPES.get(f.get("type", ""), ("unsupported", f.get("type", "")))
                path = f.get("path", "")
                key = _ASHBY_KEYS.get(path) or ("phone" if f.get("type") == "Phone" else None)
                options = ["Yes", "No"] if widget == "boolean" else [
                    v.get("label", "") for v in f.get("selectableValues") or []]
                out.append(Question(path, _clean_label(f.get("title", "")), typ, bool(entry.get("isRequired")),
                                    options, section, key, widget))
    return out


def fetch_ashby(url: str) -> Posting:
    org, pid = parse_ashby_url(url)
    body = json.dumps({"operationName": "ApiJobPosting", "query": _ASHBY_QUERY,
                       "variables": {"organizationHostedJobsPageName": org, "jobPostingId": pid}}).encode()
    req = urllib.request.Request(ASHBY_GQL, data=body, headers={"Content-Type": "application/json",
                                                                 "User-Agent": "autoapply"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jp = (data.get("data") or {}).get("jobPosting")
    if not jp:
        raise RuntimeError(f"Ashby posting not found (closed?): {url} {data.get('errors')}")
    return Posting(
        ats="ashby",
        url=f"https://jobs.ashbyhq.com/{org}/{pid}",
        title=jp.get("title", ""),
        company=org,
        location=jp.get("locationName", ""),
        jd_text=f"{jp.get('title', '')}\n{jp.get('locationName', '')}\n\n{html_to_text(jp.get('descriptionHtml', ''))}",
        questions=ashby_questions(jp),
        raw=jp,
    )


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def fetch_posting(url: str) -> Posting:
    ats = detect_ats(url)
    if ats not in FETCHERS:
        raise NotImplementedError(f"ingest for {ats!r} postings isn't built yet ({url})")
    return FETCHERS[ats](url)
