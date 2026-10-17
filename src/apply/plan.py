"""Decide an answer for every application question: profile → rules → saved answers → Claude → you."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from src.ingest import Question
from src.schemas import BulletBank, Profile

ROOT = Path(__file__).resolve().parents[2]
ANSWERS_PATH = ROOT / "data" / "answers.json"

Source = Literal["profile", "rule", "saved", "claude", "you", "skip"]

MONTHS = {m[:3].lower(): m for m in ("January", "February", "March", "April", "May", "June", "July", "August",
                                     "September", "October", "November", "December")}


@dataclass
class Answer:
    name: str
    label: str
    type: str
    required: bool
    value: str | list[str] | None
    source: Source
    note: str = ""
    options: list[str] = field(default_factory=list)
    key: str | None = None
    widget: str = ""

    @property
    def missing(self) -> bool:
        return self.required and self.value in (None, "", [])


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def match_option(options: list[str], *wanted: str) -> str | None:
    """Best option for any of the wanted values: exact, then whole-word containment either way."""
    if not options:
        return None
    by_norm = {norm(o): o for o in options}
    for w in wanted:
        if norm(w) in by_norm:
            return by_norm[norm(w)]
    for w in wanted:
        nw = norm(w)
        if not nw:
            continue
        hits = [o for o in options
                if re.search(rf"\b{re.escape(nw)}\b", norm(o)) or re.search(rf"\b{re.escape(norm(o))}\b", nw)]
        if len(hits) == 1:
            return hits[0]
    return None


def load_answers(path: Path = ANSWERS_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"how_did_you_hear": [], "by_label": {}}


def remember(label: str, value: str | list[str], path: Path = ANSWERS_PATH) -> None:
    data = load_answers(path)
    data.setdefault("by_label", {})[norm(label)] = value
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _yes_no(q: Question, yes: bool) -> str | None:
    return match_option(q.options, "Yes" if yes else "No") if q.options else ("Yes" if yes else "No")


def _profile_answer(q: Question, profile: Profile, resume_path: Path | None) -> tuple[Any, str] | None:
    first, _, last = profile.name.partition(" ")
    ed = profile.education[0] if profile.education else None
    by_key = {
        "first_name": first,
        "last_name": last or first,
        "full_name": profile.name,
        "email": profile.email,
        "phone": profile.phone,
        "location": profile.location,
    }
    if q.key in by_key:
        return by_key[q.key], ""
    if q.key == "resume":
        return (str(resume_path) if resume_path else None), "tailored PDF"
    eeo = {
        "gender": profile.eeo.gender,
        "race": profile.eeo.race_ethnicity,
        "veteran": profile.eeo.veteran,
        "disability": profile.eeo.disability,
    }
    if q.key in eeo and eeo[q.key]:
        return match_option(q.options, eeo[q.key]), ""
    label = norm(q.label)
    if re.search(r"\blinkedin\b", label):
        return profile.links.linkedin, ""
    if re.search(r"\bgithub\b", label):
        return profile.links.github, ""
    if re.search(r"\b(portfolio|personal website|website)\b", label) and q.type == "text":
        return profile.links.portfolio, ""
    if ed and re.search(r"\b(school|university|college)\b", label) and "graduat" not in label and "gpa" not in label:
        if q.type == "text":
            return ed.school, ""
        opt = match_option(q.options, ed.school, "UT Dallas", "University of Texas Dallas")
        if opt:
            return opt, ""
    return None


def _rule_answer(q: Question, profile: Profile, answers: dict) -> tuple[Any, str] | None:
    label = norm(q.label)
    wa, av = profile.work_authorization, profile.availability
    ed = profile.education[0] if profile.education else None
    if "sponsor" in label and wa.requires_sponsorship is not None:
        return _yes_no(q, wa.requires_sponsorship), "work authorization"
    if re.search(r"\b(f 1|cpt|opt|visa)\b", label) and wa.us_citizen:
        return _yes_no(q, False), "US citizen"
    if re.search(r"legally authorized|authorized to work|eligible to work", label) and wa.authorized_us is not None:
        opt = _yes_no(q, wa.authorized_us)
        if opt is None and wa.authorized_us and not wa.requires_sponsorship:
            opt = match_option(q.options, "authorized to work in the United States for any employer")
        return opt, "work authorization"
    if "citizenship" in label and wa.us_citizen and q.options:
        opt = match_option(q.options, "U.S. Citizen", "US Citizen", "Citizen")
        if opt:
            return opt, "US citizen"
    # export-control "U.S. Person" = citizen, permanent resident, or protected individual
    if re.search(r"\bu s person\b|\bus person\b", label) and wa.us_citizen and len(q.options) <= 2:
        return _yes_no(q, True), "US citizen"
    if re.search(r"\bus citizen\b|citizen of the united states", label) and wa.us_citizen is not None:
        return _yes_no(q, wa.us_citizen), "work authorization"
    if "relocat" in label and av.willing_to_relocate is not None:
        return _yes_no(q, av.willing_to_relocate), "availability"
    if re.search(r"how did you (hear|learn|find)", label):
        prefs = answers.get("how_did_you_hear") or []
        if q.type == "text":
            return answers.get("how_did_you_hear_detail") or (prefs[0] if prefs else None), "answers.json"
        # whole-option match only: "Website" must not pick "Career Fair or Career Website"
        for pref in prefs:
            hits = [o for o in q.options if norm(o) == norm(pref) or norm(o).startswith(norm(pref) + " ")]
            if len(hits) > 1:  # e.g. "Other conferences or events" vs "Other (none of the above)"
                hits = [o for o in hits if re.search(r"none of the above|specify|indicate|explain", norm(o))]
            if len(hits) == 1:
                return hits[0], "answers.json preference"
    if av.earliest_start and re.search(r"\bstart\b", label) and q.options:
        month = av.earliest_start.split()[0]
        opt = match_option(q.options, av.earliest_start, month)
        if opt:
            return opt, "availability"
    if ed and re.search(r"degree.*(pursuing|seeking)|(pursuing|seeking).*degree|highest level of education", label):
        opt = match_option(q.options, "Bachelor's", "Bachelor's Degree", "Bachelors")
        if opt:
            return opt, "education"
    if ed and re.search(r"currently (enrolled|pursuing|a student)|enrollment status", label):
        if q.options and len(q.options) > 2:
            opt = match_option(q.options, "Undergraduate", "Bachelor's", "Full-time student", "Enrolled")
        else:
            opt = _yes_no(q, True)
        if opt:
            return opt, "education"
    if ed and re.search(r"graduat", label):
        month, _, year = ed.graduation.partition(" ")
        full_month = MONTHS.get(month[:3].lower(), month)
        if q.type == "text":
            return ed.graduation, "education"
        opt = match_option(q.options, ed.graduation, f"{full_month} {year}")
        has_words = any(re.search(r"[a-z]", norm(o)) for o in q.options)
        if opt is None and not has_words:
            opt = match_option(q.options, year)  # "Expected graduation year": 2026 / 2027 / ...
        if opt is None and has_words and not any(re.search(r"\d", o) for o in q.options):
            opt = match_option(q.options, full_month)  # "Expected graduation month": April/May/June, August/September
        if opt:
            return opt, "education"
    if ed and re.search(r"\b(major|field of study|area of study)\b", label) and q.type in ("text", "textarea"):
        m = re.search(r"\bin (.+)$", ed.degree)
        if m:
            return m.group(1).strip(), "education"
    return None


def resolve(
    questions: list[Question],
    profile: Profile,
    resume_path: Path | None,
    answers: dict | None = None,
) -> list[Answer]:
    """Deterministic pass. Anything unresolved comes back with source='you' for Claude/you to fill."""
    answers = answers if answers is not None else load_answers()
    saved = answers.get("by_label", {})
    out: list[Answer] = []
    for q in questions:
        a = Answer(q.name, q.label, q.type, q.required, None, "you", options=q.options, key=q.key, widget=q.widget)
        prev = out[-1] if out else None
        follows_other = (prev is not None and isinstance(prev.value, str) and norm(prev.value).startswith("other")
                         and re.search(r"how did you (hear|learn|find)", norm(prev.label)))
        if follows_other and q.type == "text" and answers.get("how_did_you_hear_detail"):
            a.value, a.source, a.note = answers["how_did_you_hear_detail"], "saved", "detail for 'Other'"
        elif q.type == "hidden" or q.name in ("resume_text", "cover_letter_text"):
            a.source, a.note = "skip", "filled by the page"
        elif q.type == "unsupported":
            a.source, a.note = ("you" if q.required else "skip"), f"fill in the browser ({q.widget} field)"
        elif q.key == "cover_letter" and not q.required:
            a.source, a.note = "skip", "optional"
        elif (hit := _profile_answer(q, profile, resume_path)) is not None and hit[0] not in (None, ""):
            a.value, a.source, a.note = hit[0], "profile", hit[1]
        elif norm(q.label) in saved:
            a.value, a.source, a.note = saved[norm(q.label)], "saved", "answers.json"
        elif (hit := _rule_answer(q, profile, answers)) is not None and hit[0] not in (None, ""):
            a.value, a.source, a.note = hit[0], "rule", hit[1]
        elif not q.required:
            a.source, a.note = "skip", "optional"
        out.append(a)
    return out


# ---- Claude fallback ----

class DraftedAnswer(BaseModel):
    name: str
    value: str | list[str] | None
    needs_human: bool
    note: str


class Drafts(BaseModel):
    answers: list[DraftedAnswer]


def facts_for_claude(profile: Profile, bank: BulletBank) -> dict:
    return {
        "profile": profile.model_dump(exclude={"eeo"}),
        "experience": [
            {"org": e.org, "title": e.title, "dates": e.dates, "bullets": [b.text for b in e.bullets]}
            for e in bank.experience
        ],
        "projects": [{"name": p.name, "tech": p.tech_line, "dates": p.dates,
                      "bullets": [b.text for b in p.bullets]} for p in bank.projects],
        "skills": bank.skills,
    }


def draft_unresolved(answers: list[Answer], jd_text: str, company: str, facts: dict, runner) -> list[Answer]:
    """Ask Claude for the remaining required questions; invalid select values fall back to 'you'."""
    from src.tailor import call

    todo = [a for a in answers if a.source == "you" and a.required]
    if not todo:
        return answers
    payload = {
        "company": company,
        "jd": jd_text[:8000],
        "facts": facts,
        "questions": [{"name": a.name, "label": a.label, "type": a.type, "options": a.options} for a in todo],
    }
    drafts = call(runner, "answer.md", json.dumps(payload), Drafts)
    by_name = {d.name: d for d in drafts.answers}
    for a in todo:
        d = by_name.get(a.name)
        if d is None or d.needs_human or d.value in (None, "", []):
            a.note = d.note if d else "not answered"
            continue
        if a.type == "select":
            value = match_option(a.options, d.value if isinstance(d.value, str) else d.value[0])
        elif a.type == "multiselect":
            vals = [d.value] if isinstance(d.value, str) else d.value
            picked = [match_option(a.options, v) for v in vals]
            value = picked if picked and all(picked) else None
        else:
            value = d.value if isinstance(d.value, str) else "\n".join(d.value)
        if value:
            a.value, a.source, a.note = value, "claude", d.note
        else:
            a.note = f"Claude answer {d.value!r} isn't one of the options"
    return answers


def page_extras(profile: Profile, ats: str = "greenhouse") -> dict[str, str | None]:
    """Fields the page shows that its API doesn't list, in fill order."""
    if ats == "greenhouse":
        return {"country": "United States", "hispanic_ethnicity": profile.eeo.hispanic_latino}
    if ats == "lever" and profile.eeo.disability:
        # Lever's voluntary disability form asks for a signature and date alongside the answer
        from datetime import date
        return {"eeo[disabilitySignature]": profile.name,
                "eeo[disabilitySignatureDate]": date.today().strftime("%m/%d/%Y")}
    return {}


def to_json(answers: list[Answer]) -> list[dict]:
    return [asdict(a) for a in answers]


def from_json(items: list[dict]) -> list[Answer]:
    return [Answer(**i) for i in items]
