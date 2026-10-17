"""Pydantic models for data/profile.json and data/bullets.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, Field, model_validator

DATA = Path(__file__).resolve().parents[1] / "data"


class Bullet(BaseModel):
    id: str
    text: str
    bold: list[str] = Field(default_factory=list)  # substrings of text to render bold
    tags: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)
    has_metric: bool = False
    priority: int = Field(ge=1, le=3)  # 1 = always include if any relevance

    @model_validator(mode="after")
    def _grounded(self) -> "Bullet":
        # tech and bold must come from the bullet text itself, so nothing is invented
        lower = self.text.lower()
        missing = [t for t in self.tech if t.lower() not in lower]
        if missing:
            raise ValueError(f"{self.id}: tech not in text: {missing}")
        missing = [b for b in self.bold if b not in self.text]
        if missing:
            raise ValueError(f"{self.id}: bold phrase not in text: {missing}")
        return self


class Experience(BaseModel):
    id: str
    org: str
    title: str
    team: str | None = None
    location: str
    dates: str
    bullets: list[Bullet]


class Project(BaseModel):
    id: str
    name: str
    tech_line: list[str] = Field(default_factory=list)
    dates: str
    bullets: list[Bullet]


class BulletBank(BaseModel):
    experience: list[Experience]
    projects: list[Project] = Field(default_factory=list)
    leadership: list[Experience] = Field(default_factory=list)
    skills: dict[str, list[str]]
    skill_labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_ids(self) -> "BulletBank":
        ids = [b.id for b in self.all_bullets()]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate bullet ids: {sorted(dupes)}")
        unlabeled = set(self.skills) - set(self.skill_labels)
        if self.skill_labels and unlabeled:
            raise ValueError(f"skills without a label: {sorted(unlabeled)}")
        return self

    def all_bullets(self) -> Iterator[Bullet]:
        for section in (self.experience, self.projects, self.leadership):
            for entry in section:
                yield from entry.bullets


class Education(BaseModel):
    school: str
    location: str
    degree: str
    graduation: str
    gpa: str | None = None
    coursework: list[str] = Field(default_factory=list)
    leadership: list[str] = Field(default_factory=list)


class Links(BaseModel):
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None


class WorkAuthorization(BaseModel):
    authorized_us: bool | None = None
    requires_sponsorship: bool | None = None
    us_citizen: bool | None = None


class Availability(BaseModel):
    earliest_start: str | None = None
    terms: list[str] = Field(default_factory=list)
    willing_to_relocate: bool | None = None


class EEO(BaseModel):
    gender: str | None = None
    race_ethnicity: str | None = None
    veteran: str | None = None
    disability: str | None = None


class Profile(BaseModel):
    name: str
    email: str
    phone: str
    location: str
    links: Links
    education: list[Education]
    work_authorization: WorkAuthorization
    availability: Availability
    eeo: EEO

    def missing_fields(self) -> list[str]:
        """Dotted paths of fields still unset, so apply forms don't guess."""
        out = []

        def walk(obj: BaseModel, prefix: str) -> None:
            for name, value in obj:
                path = f"{prefix}{name}"
                if isinstance(value, BaseModel):
                    walk(value, path + ".")
                elif value is None:
                    out.append(path)

        walk(self, "")
        return out


def load_profile(path: Path = DATA / "profile.json") -> Profile:
    return Profile.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_bullets(path: Path = DATA / "bullets.json") -> BulletBank:
    return BulletBank.model_validate(json.loads(path.read_text(encoding="utf-8")))
