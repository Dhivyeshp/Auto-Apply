"""Re-parse the rendered PDF as an ATS would and measure JD keyword coverage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber


def pdf_text(pdf: Path) -> str:
    with pdfplumber.open(pdf) as doc:
        text = "\n".join(page.extract_text() or "" for page in doc.pages)
    return re.sub(r"\s+", " ", text)


def _pattern(term: str) -> re.Pattern:
    """Case-insensitive, hyphen/space-insensitive, optional plural, bounded by non-word chars."""
    t = term.strip().lower()
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    parts = [re.escape(p) for p in re.split(r"[\s-]+", t) if p]
    body = r"[\s-]?".join(parts)
    return re.compile(rf"(?<![\w+#]){body}(?:s|es)?(?![\w+#])", re.I)


def matches(term: str, text: str) -> bool:
    if _pattern(term).search(text):
        return True
    # "C/C++" or "Typescript/Javascript": any listed alternative counts, ignoring 1-char ones like "C"
    if "/" in term:
        alts = [a for a in term.split("/") if len(a.strip()) >= 2]
        return any(_pattern(a).search(text) for a in alts)
    return False


@dataclass
class Coverage:
    coverage_pct: float
    must_have_pct: float
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    must_have_missing: list[str] = field(default_factory=list)
    below_threshold: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _dedupe(terms: list[str]) -> list[str]:
    seen, out = set(), []
    for t in terms:
        k = t.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(t.strip())
    return out


def coverage(text: str, keywords: list[str], must_have: list[str] = (), threshold: float = 60.0) -> Coverage:
    keywords, must_have = _dedupe(list(keywords)), _dedupe(list(must_have))
    matched = [k for k in keywords if matches(k, text)]
    missing = [k for k in keywords if k not in matched]
    mh_missing = [k for k in must_have if not matches(k, text)]
    pct = round(100 * len(matched) / len(keywords), 1) if keywords else 100.0
    mh_pct = round(100 * (len(must_have) - len(mh_missing)) / len(must_have), 1) if must_have else 100.0
    return Coverage(pct, mh_pct, matched, missing, mh_missing, below_threshold=pct < threshold)


def check_pdf(pdf: Path, keywords: list[str], must_have: list[str] = (), threshold: float = 60.0) -> Coverage:
    return coverage(pdf_text(pdf), keywords, must_have, threshold)
