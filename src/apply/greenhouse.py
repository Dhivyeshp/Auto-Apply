"""Fill a Greenhouse application form with Playwright. Submitting is a separate, explicit call."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout

from src.apply.plan import Answer, norm
from src.ingest import parse_greenhouse_url

FORM_URL = "https://job-boards.greenhouse.io/{board}/jobs/{job}"
CONFIRM_RE = re.compile(r"thank you for applying|application (has been )?(received|submitted)|we.ve received your application", re.I)


@dataclass
class FillResult:
    name: str
    ok: bool
    error: str = ""


def form_url(url: str) -> str:
    board, job = parse_greenhouse_url(url)
    return FORM_URL.format(board=board, job=job)


def _by_id(page: Page, name: str):
    return page.locator(f'[id="{name}"]')


def choose_option(page: Page, name: str, value: str, timeout: int = 5000) -> None:
    """Open a react-select combobox and click the option whose text equals `value`."""
    box = _by_id(page, name)
    box.scroll_into_view_if_needed(timeout=timeout)
    box.click(timeout=timeout)
    options = page.locator(f'[id^="react-select-{name}-option-"]')
    options.first.wait_for(timeout=timeout)
    if options.count() > 25:  # long lists (countries, schools): filter by typing first
        box.fill(value[:30])
        page.wait_for_timeout(400)
    texts = [norm(options.nth(i).inner_text()) for i in range(options.count())]
    want = norm(value)
    pick = [i for i, t in enumerate(texts) if t == want]
    if not pick:  # e.g. "United States" vs "United States +1"
        pick = [i for i, t in enumerate(texts) if t.startswith(want + " ")]
    if len(pick) == 1:
        options.nth(pick[0]).click(timeout=timeout)
        return
    page.keyboard.press("Escape")
    raise ValueError(f"option {value!r} not found" if not pick else f"option {value!r} is ambiguous")


def choose_location(page: Page, value: str, timeout: int = 8000) -> None:
    """Type into the location autocomplete and pick the first suggestion containing the city."""
    box = page.locator("#candidate-location")
    box.click(timeout=timeout)
    city = value.split(",")[0].strip()
    box.fill(city)
    options = page.locator('[id^="react-select-candidate-location-option-"]')
    options.first.wait_for(timeout=timeout)
    for i in range(options.count()):
        if city.lower() in options.nth(i).inner_text().lower():
            options.nth(i).click(timeout=timeout)
            return
    options.first.click(timeout=timeout)


def check_boxes(page: Page, name: str, values: list[str], timeout: int = 5000) -> None:
    group = _by_id(page, name)
    for v in values:
        group.get_by_text(v, exact=True).click(timeout=timeout)


def fill_field(page: Page, a: Answer) -> None:
    v = a.value
    if a.type == "file":
        page.set_input_files(f'[id="{a.name}"]', str(v))
    elif a.name == "location":
        choose_location(page, str(v))
    elif a.type in ("text", "textarea"):
        _by_id(page, a.name).fill(str(v))
    elif a.type == "select":
        choose_option(page, a.name, str(v))
    elif a.type == "multiselect":
        check_boxes(page, a.name, list(v) if isinstance(v, list) else [str(v)])
    else:
        raise ValueError(f"unsupported field type {a.type!r}")


def fill_form(page: Page, answers: list[Answer], extra: dict[str, str | None] | None = None,
              url: str | None = None) -> list[FillResult]:
    """Fill every answered field (opening `url` first if given). `extra` covers page-only selects."""
    if url:
        page.goto(form_url(url), wait_until="networkidle", timeout=60000)
    results = []
    # page-only selects first: answering Hispanic/Latino is what reveals the race field
    for name, value in (extra or {}).items():
        if value and _by_id(page, name).count():
            try:
                choose_option(page, name, value)
                results.append(FillResult(name, True))
            except Exception as e:
                results.append(FillResult(name, False, str(e).splitlines()[0][:200]))
    for a in answers:
        if a.source == "skip" or a.value in (None, "", []):
            continue
        if a.name == "race" and not _by_id(page, "race").is_visible():
            results.append(FillResult(a.name, False, "hidden (the page shows race only for some Hispanic/Latino answers)"))
            continue
        try:
            fill_field(page, a)
            results.append(FillResult(a.name, True))
        except Exception as e:  # keep going; failures are shown in review
            results.append(FillResult(a.name, False, str(e).splitlines()[0][:200]))
    return results


def screenshot(page: Page, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=True)
    return path


def captcha_challenge_visible(page: Page) -> bool:
    for frame in page.locator('iframe[src*="recaptcha"][src*="bframe"]').all():
        try:
            if frame.is_visible():
                return True
        except Exception:
            pass
    return False


def submit(page: Page, wait_for_human, timeout_s: int = 120) -> bool:
    """Click submit, hand off to the human for CAPTCHA or email codes, and wait for confirmation."""
    page.get_by_role("button", name=re.compile(r"submit application", re.I)).click()
    deadline = timeout_s * 1000
    waited = 0
    while waited < deadline:
        body = page.locator("body").inner_text(timeout=5000)
        if CONFIRM_RE.search(body):
            return True
        if captcha_challenge_visible(page) or re.search(r"security code|verification code", body, re.I):
            wait_for_human("Finish the CAPTCHA / enter the emailed code in the browser, then press Enter here.")
            waited = 0
            continue
        try:
            page.wait_for_timeout(2000)
        except PWTimeout:
            pass
        waited += 2000
    return False
