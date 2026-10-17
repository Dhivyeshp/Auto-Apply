"""Fill a Lever application form (plain HTML: inputs, native selects, radios, checkboxes by name)."""

from __future__ import annotations

from playwright.sync_api import Page

from src.apply.common import FillResult, first_line
from src.apply.plan import Answer, norm
from src.ingest import lever_apply_url


def _named(page: Page, name: str, extra: str = ""):
    return page.locator(f'[name="{name}"]{extra}')


def _pick(page: Page, name: str, kind: str, value: str) -> None:
    """Check the radio/checkbox in group `name` whose value equals `value`."""
    boxes = _named(page, name, f'[type="{kind}"]')
    for i in range(boxes.count()):
        box = boxes.nth(i)
        if norm(box.get_attribute("value") or "") == norm(value):
            box.check(force=True)  # Lever styles hide the native control behind its label
            return
    raise ValueError(f"option {value!r} not found")


def fill_field(page: Page, a: Answer) -> None:
    v = a.value
    if a.type == "file":
        page.set_input_files(f'input[name="{a.name}"]', str(v))
    elif a.widget == "native_select":
        _named(page, a.name).select_option(value=str(v), timeout=5000)  # options were parsed from value attrs
    elif a.widget == "radio":
        _pick(page, a.name, "radio", str(v))
    elif a.widget == "checkbox" or a.type == "multiselect":
        for item in (v if isinstance(v, list) else [v]):
            _pick(page, a.name, "checkbox", str(item))
    elif a.type in ("text", "textarea"):
        _named(page, a.name).first.fill(str(v))
    else:
        raise ValueError(f"unsupported field type {a.type!r}/{a.widget!r}")


def fill_form(page: Page, answers: list[Answer], extra: dict[str, str | None] | None = None,
              url: str | None = None) -> list[FillResult]:
    if url:
        page.goto(lever_apply_url(url), wait_until="networkidle", timeout=60000)
    results = []
    for a in answers:
        if a.source == "skip" or a.value in (None, "", []):
            continue
        try:
            fill_field(page, a)
            results.append(FillResult(a.name, True))
        except Exception as e:
            results.append(FillResult(a.name, False, first_line(e)))
    for name, value in (extra or {}).items():
        if value and _named(page, name).count():
            try:
                _named(page, name).first.fill(value)
                results.append(FillResult(name, True))
            except Exception as e:
                results.append(FillResult(name, False, first_line(e)))
    return results
