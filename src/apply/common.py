"""Browser helpers shared by the ATS fillers: screenshots, CAPTCHA detection, submit + confirm."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page

CONFIRM_RE = re.compile(
    r"thank(s| you) for (applying|your (application|interest))|"
    r"application (has been |was )?(received|submitted)|we.ve received your application|successfully submitted",
    re.I,
)
SUBMIT_RE = re.compile(r"submit( your)? application", re.I)


@dataclass
class FillResult:
    name: str
    ok: bool
    error: str = ""


def first_line(e: Exception) -> str:
    return str(e).splitlines()[0][:200] if str(e) else type(e).__name__


def screenshot(page: Page, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=True)
    return path


def captcha_challenge_visible(page: Page) -> bool:
    """A visible reCAPTCHA (bframe) or hCaptcha challenge frame."""
    for frame in page.locator('iframe[src*="recaptcha"][src*="bframe"], iframe[src*="hcaptcha"][src*="challenge"]').all():
        try:
            if frame.is_visible():
                return True
        except Exception:
            pass
    return False


def submit(page: Page, wait_for_human, timeout_s: int = 120) -> bool:
    """Click submit, hand off to the human for CAPTCHA or email codes, and wait for confirmation."""
    page.get_by_role("button", name=SUBMIT_RE).first.click()
    waited, deadline = 0, timeout_s * 1000
    while waited < deadline:
        body = page.locator("body").inner_text(timeout=5000)
        if CONFIRM_RE.search(body):
            return True
        if captcha_challenge_visible(page) or re.search(r"security code|verification code", body, re.I):
            wait_for_human("Finish the CAPTCHA / enter the emailed code in the browser, then press Enter here.")
            waited = 0
            continue
        page.wait_for_timeout(2000)
        waited += 2000
    return False
