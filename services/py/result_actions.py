"""
TrailerTap result actions — Phase 3 orchestration.

Match -> result card payload:
  1. Calendar invite (ICS) for the day BEFORE release
  2. Primary CTA:
       movie -> Fandango affiliate ticket link
       tv    -> Watch/Subscribe streaming link (ALWAYS present — falls back
                to a Where-to-Watch search link if no direct URL, so the
                button can never silently disappear again; see bug #171234)

Deliberately excluded: affiliate discount copy (5%/10%). Do not surface
discounts in any CTA until affiliate codes are live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote_plus

from fingerprint_engine import build_affiliate_link


@dataclass
class MediaItem:
    media_id: str
    title: str
    media_type: str              # 'movie' | 'tv'
    release_date: date
    platform: str = ""           # e.g. 'Amazon Prime Video'
    slug: str = ""               # provider slug for deep links
    streaming_url: str = ""      # direct platform URL if known


def primary_cta(item: MediaItem, affiliate_ids: dict[str, str]) -> dict:
    """The revenue-bearing button. Branch on media type; both branches MUST
    return a link — returning None is a bug class, not an option."""
    if item.media_type == "movie":
        return {
            "label": "Buy Tickets",
            "url": build_affiliate_link(
                "fandango", item.slug or quote_plus(item.title),
                affiliate_ids.get("fandango", ""),
            ),
        }
    if item.media_type == "tv":
        if item.streaming_url:
            return {"label": f"Watch on {item.platform or 'streaming'}",
                    "url": item.streaming_url}
        # Fallback: never render a card without a watch path
        q = quote_plus(f"{item.title} {item.platform} watch".strip())
        return {"label": "Where to Watch",
                "url": f"https://www.google.com/search?q={q}"}
    raise ValueError(f"Unknown media_type: {item.media_type}")


def reminder_date(item: MediaItem, today: date) -> date | None:
    """Day-before-release reminder; None if release is today or past."""
    d = item.release_date - timedelta(days=1)
    return d if d >= today else None


def make_ics(item: MediaItem, remind_on: date, action_url: str) -> str:
    """RFC 5545 all-day event the email pipeline can attach as invite.ics."""
    d0 = remind_on.strftime("%Y%m%d")
    d1 = (remind_on + timedelta(days=1)).strftime("%Y%m%d")
    verb = "in theaters" if item.media_type == "movie" else \
           f"streaming on {item.platform}" if item.platform else "streaming"
    return "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TrailerTap//EN",
        "BEGIN:VEVENT",
        f"UID:{item.media_id}@trailertap.app",
        f"DTSTART;VALUE=DATE:{d0}",
        f"DTEND;VALUE=DATE:{d1}",
        f"SUMMARY:{item.title} — out tomorrow ({verb})",
        f"DESCRIPTION:{action_url}",
        "END:VEVENT",
        "END:VCALENDAR",
    ])


def build_result_actions(item: MediaItem, affiliate_ids: dict[str, str],
                         today: date) -> dict:
    """Everything the client needs after a successful identification."""
    cta = primary_cta(item, affiliate_ids)
    remind_on = reminder_date(item, today)
    released = item.release_date <= today
    return {
        "media_id": item.media_id,
        "title": item.title,
        "media_type": item.media_type,
        "release_date": item.release_date.isoformat(),
        "primary_cta": cta,                       # never None by construction
        "calendar_invite": (
            make_ics(item, remind_on, cta["url"]) if remind_on else None
        ),
        "status": "released" if released else "upcoming",
  }
