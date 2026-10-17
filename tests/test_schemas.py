import pytest
from pydantic import ValidationError

from src import schemas


def test_data_files_load():
    bank = schemas.load_bullets()
    profile = schemas.load_profile()
    assert len(list(bank.all_bullets())) >= 15
    assert profile.education[0].school == "University of Texas at Dallas"


def test_every_section_has_a_priority_1_bullet():
    bank = schemas.load_bullets()
    for entry in bank.experience[:2] + bank.projects[:1]:
        assert any(b.priority == 1 for b in entry.bullets), entry.id


def test_has_metric_matches_text():
    for b in schemas.load_bullets().all_bullets():
        assert b.has_metric == any(ch.isdigit() for ch in b.text), b.id


def bullet(**over):
    b = {"id": "x-1", "text": "Built a Python service", "tech": ["Python"], "priority": 1}
    b.update(over)
    return b


def test_rejects_tech_not_in_text():
    with pytest.raises(ValidationError, match="tech not in text"):
        schemas.Bullet.model_validate(bullet(tech=["Rust"]))


def test_rejects_bold_not_in_text():
    with pytest.raises(ValidationError, match="bold phrase not in text"):
        schemas.Bullet.model_validate(bullet(bold=["99%"]))


def test_rejects_duplicate_ids():
    exp = {"id": "e", "org": "O", "title": "T", "location": "L", "dates": "D",
           "bullets": [bullet(), bullet()]}
    with pytest.raises(ValidationError, match="duplicate bullet ids"):
        schemas.BulletBank.model_validate({"experience": [exp], "skills": {}})


def test_missing_profile_fields_reported():
    missing = schemas.load_profile().missing_fields()
    assert "work_authorization.requires_sponsorship" in missing
