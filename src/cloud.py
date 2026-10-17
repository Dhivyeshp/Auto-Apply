"""Laptop-independent alerts: run by GitHub Actions, pushes new matching jobs to your phone.

State is just the set of listing ids already seen (data/cloud_seen.json, kept in the Actions cache).
No profile, resume, or Claude needed here; tailoring happens on the laptop.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.discover import simplify
from src.tracker import url_key

MAX_SINGLE_PUSHES = 5  # more new jobs than this at once → one summary push


@dataclass
class CloudResult:
    status: str  # unchanged | seeded | checked
    new: list[simplify.Listing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def matching(cfg: dict, raw: list[dict]) -> list[simplify.Listing]:
    out, keys = [], set()
    for r in raw:
        try:
            listing = simplify.normalize(r)
        except Exception:
            continue
        key = url_key(listing.url)
        if simplify.filter_reason(listing, cfg) is None and key not in keys:
            keys.add(key)
            out.append(listing)
    return out


def seen_markers(listing: simplify.Listing) -> set[str]:
    """A job counts as seen if either its id or its normalized link was seen (lists overlap)."""
    return {listing.listing_id, "u:" + url_key(listing.url)}


def describe(listing: simplify.Listing, cfg: dict) -> str:
    star = "★ " if simplify.is_preferred(listing, cfg) else ""
    locs = listing.locations[0] + (f" +{len(listing.locations) - 1}" if len(listing.locations) > 1 else "") \
        if listing.locations else ""
    return f"{star}{listing.company} — {listing.role} ({locs})"


def pushes(new: list[simplify.Listing], cfg: dict) -> list[tuple[str, str, str | None]]:
    """(title, message, click_url) per push: one per job, or a single summary for a big batch."""
    new = sorted(new, key=lambda l: (simplify.is_preferred(l, cfg), l.date_posted or 0), reverse=True)
    if len(new) <= MAX_SINGLE_PUSHES:
        return [(f"New: {l.company}", f"{describe(l, cfg)}\n{l.ats} · tap to open", l.url) for l in new]
    lines = [describe(l, cfg) for l in new[:10]] + ([f"+{len(new) - 10} more"] if len(new) > 10 else [])
    return [(f"{len(new)} new internships", "\n".join(lines) + "\nOpen on your laptop: python main.py inbox", None)]


def env_sender(cfg: dict) -> Callable[[str, str, str | None], None]:
    from src import notify

    topic = os.environ.get("NTFY_TOPIC") or (cfg.get("notify") or {}).get("ntfy_topic")
    webhook = os.environ.get("DISCORD_WEBHOOK") or (cfg.get("notify") or {}).get("discord_webhook")
    if not (topic or webhook):
        raise RuntimeError("no alert channel: set the NTFY_TOPIC or DISCORD_WEBHOOK secret")

    def send(title: str, message: str, click: str | None) -> None:
        if topic:
            notify.ntfy(topic, title, message, click=click)
        if webhook:
            notify.discord(webhook, f"**{title}**\n{message}" + (f"\n{click}" if click else ""))

    return send


def check(cfg: dict, seen_path: Path, send: Callable[[str, str, str | None], None],
          listings: list[dict] | None = None, etag_file: Path | None = None) -> CloudResult:
    raw = listings if listings is not None else simplify.fetch_all(cfg, etag_file=etag_file)
    if raw is None:
        return CloudResult("unchanged")
    current = matching(cfg, raw)
    ids = set().union(*(seen_markers(l) for l in current)) if current else set()
    if not seen_path.exists():  # first run: remember everything, alert nothing
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps(sorted(ids)), encoding="utf-8")
        return CloudResult("seeded")
    seen = set(json.loads(seen_path.read_text(encoding="utf-8")))
    if seen and not any(s.startswith("u:") for s in seen):
        # state from before link-matching (and before extra lists): re-seed instead of alerting the backlog
        seen_path.write_text(json.dumps(sorted(seen | ids)), encoding="utf-8")
        return CloudResult("seeded")
    res = CloudResult("checked", [l for l in current if not (seen_markers(l) & seen)])
    for title, message, click in pushes(res.new, cfg):
        try:
            send(title, message, click)
        except Exception as e:
            res.errors.append(str(e))
    # only mark as seen once alerts went out, so a failed push is retried next run
    if not res.errors:
        seen_path.write_text(json.dumps(sorted(seen | ids)), encoding="utf-8")
    return res
