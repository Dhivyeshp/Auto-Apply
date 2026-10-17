"""Fill an Ashby application form (React: ids = field paths, radios named <formId>_<path>, Yes/No buttons)."""

from __future__ import annotations

from playwright.sync_api import Page

from src.apply.common import FillResult, first_line
from src.apply.plan import Answer, norm
from src.ingest import parse_ashby_url

FORM_URL = "https://jobs.ashbyhq.com/{org}/{pid}/application"


def form_url(url: str) -> str:
    org, pid = parse_ashby_url(url)
    return FORM_URL.format(org=org, pid=pid)


def _label_for(page: Page, input_id: str) -> str:
    label = page.locator(f'label[for="{input_id}"]')
    return label.first.inner_text().strip() if label.count() else ""


def _click_choice(page: Page, inputs, value: str) -> None:
    """Click the radio/checkbox whose label text equals `value`."""
    for i in range(inputs.count()):
        inp = inputs.nth(i)
        input_id = inp.get_attribute("id") or ""
        label = _label_for(page, input_id)
        if norm(label or inp.get_attribute("name") or "") == norm(value):
            if label:
                page.locator(f'label[for="{input_id}"]').first.click()
            else:
                inp.check(force=True)
            return
    raise ValueError(f"option {value!r} not found")


def _boolean_buttons(page: Page, path: str):
    """Yes/No questions keep a hidden checkbox named by path; the buttons sit in its nearest wrapper."""
    hidden = page.locator(f'input[type="checkbox"][name="{path}"]').first
    return hidden.locator("xpath=ancestor::*[.//button[normalize-space()='Yes']][1]")


def choose_school(page: Page, path: str, value: str, timeout: int = 8000) -> None:
    box = page.locator('input[role="combobox"][placeholder*="chool"]').first
    box.click(timeout=timeout)
    box.fill(value)
    opts = page.locator('[role="option"]')
    opts.first.wait_for(timeout=timeout)
    for i in range(opts.count()):
        if norm(value) in norm(opts.nth(i).inner_text()):
            opts.nth(i).click()
            return
    raise ValueError(f"school {value!r} not in suggestions")


def fill_field(page: Page, a: Answer) -> None:
    v = a.value
    if a.type == "file":
        page.set_input_files(f'input[type="file"][id="{a.name}"]', str(v))
    elif a.widget == "school":
        choose_school(page, a.name, str(v))
    elif a.widget == "boolean":
        _boolean_buttons(page, a.name).get_by_role("button", name=str(v), exact=True).click()
    elif a.widget == "radio":
        radios = page.locator(f'input[type="radio"][name$="_{a.name}"]')
        if not radios.count():
            raise ValueError("radio group not found")
        _click_choice(page, radios, str(v))
    elif a.widget == "checkbox" or a.type == "multiselect":
        boxes = page.locator(f'input[type="checkbox"][id*="_{a.name}-labeled-checkbox-"]')
        for item in (v if isinstance(v, list) else [v]):
            _click_choice(page, boxes, str(item))
    elif a.type in ("text", "textarea"):
        page.locator(f'[id="{a.name}"]').first.fill(str(v))
    else:
        raise ValueError(f"unsupported field type {a.type!r}/{a.widget!r}")


def fill_form(page: Page, answers: list[Answer], extra: dict[str, str | None] | None = None,
              url: str | None = None) -> list[FillResult]:
    if url:
        page.goto(form_url(url), wait_until="networkidle", timeout=60000)
    results = []
    for a in answers:
        if a.source == "skip" or a.value in (None, "", []):
            continue
        try:
            fill_field(page, a)
            results.append(FillResult(a.name, True))
        except Exception as e:
            results.append(FillResult(a.name, False, first_line(e)))
    return results
