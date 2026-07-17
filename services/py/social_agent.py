"""
TrailerTap social content agent.

Workflow (checkpoint model):
  1. generate_batch()  — agent produces N varied posts (templates x hooks,
     rotation + dedupe so the feed never repeats itself back-to-back)
  2. Human reviews the batch (the checkpoint)
  3. export_buffer_csv() — bulk-upload to Buffer's queue, OR
     post_to_meta()     — direct Meta Graph API publish (IG/FB), for full
     automation as a Render cron job once you trust the output.

Credentials: env vars only (META_PAGE_ID, META_ACCESS_TOKEN,
IG_USER_ID). Never hardcoded, never in chat.

Content rules baked in:
  - NO discount copy (5%/10%) until affiliate codes are live
  - TikTok excluded until URL verification clears
  - Trending posts only render when real chart data exists
"""

from __future__ import annotations

import csv
import json
import os
import random
from datetime import datetime, timedelta

# ------------------------- content ingredients ------------------------------

HOOKS = [
    "Ever seen a trailer and forgotten the name 10 seconds later?",
    "That trailer you saw last night? We can name it in 5 seconds.",
    "Stop screenshotting trailers and asking Reddit what movie it is.",
    "Your friend sends a trailer clip. No caption. No title. Now what?",
    "Too many streaming services. Too many trailers. One app.",
    "You watched the trailer. You forgot the release date. Classic.",
]

PROOF_POINTS = [
    "Hum it, screenshot it, or paste the link — TrailerTap IDs it instantly.",
    "Audio, screenshot, or social link. Three ways to identify any trailer.",
    "It even adds the release date to your calendar so you never miss it.",
    "One tap: identified, saved to your library, reminder set for release day.",
    "Free to use — no sign-up needed for your first identifications.",
]

CTAS = [
    "Try it free at trailertap.app",
    "trailertap.app — free forever with a personal trailer library",
    "Link in bio. Never lose a trailer again.",
    "Identify your first trailer at trailertap.app",
]

HASHTAGS = {
    "instagram": "#movies #trailers #newmovies #whattowatch #streaming #film",
    "facebook": "",
}

BANNED_TERMS = ["% off", "discount", "5%", "10%"]  # until codes are live


# ---------------------------- generation ------------------------------------

def _trending_post(chart: list[dict], region_label: str) -> str | None:
    if not chart or len(chart) < 3:
        return None
    top = chart[:3]
    lines = [f"Most identified trailers this week ({region_label}):"]
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    for medal, entry in zip(medals, top):
        lines.append(f"{medal} {entry['title']}")
    lines.append("")
    lines.append("What did YOU identify this week? trailertap.app")
    return "\n".join(lines)


def generate_batch(n: int = 12, seed: int | None = None,
                   trending_chart: list[dict] | None = None,
                   region_label: str = "worldwide") -> list[dict]:
    """Produce n posts across platforms with rotation and no near-repeats."""
    rng = random.Random(seed)
    posts: list[dict] = []
    used_pairs: set[tuple[int, int]] = set()

    # Lead with a trending post if real data exists — highest-signal content
    t = _trending_post(trending_chart or [], region_label)
    if t:
        posts.append({"platform": "instagram", "kind": "trending",
                      "text": t + "\n\n" + HASHTAGS["instagram"]})
        posts.append({"platform": "facebook", "kind": "trending", "text": t})

    while len(posts) < n:
        hi, pi = rng.randrange(len(HOOKS)), rng.randrange(len(PROOF_POINTS))
        if (hi, pi) in used_pairs:
            continue
        used_pairs.add((hi, pi))
        platform = "instagram" if len(posts) % 2 == 0 else "facebook"
        body = f"{HOOKS[hi]}\n\n{PROOF_POINTS[pi]}\n\n{rng.choice(CTAS)}"
        tags = HASHTAGS[platform]
        text = body + ("\n\n" + tags if tags else "")
        posts.append({"platform": platform, "kind": "evergreen", "text": text})

    for p in posts:  # hard guard: no discount copy leaves this function
        low = p["text"].lower()
        assert not any(b in low for b in BANNED_TERMS), "banned term in post"
    return posts[:n]


def schedule_batch(posts: list[dict], start: datetime,
                   per_day: int = 1) -> list[dict]:
    """Assign posting datetimes: spread across days at engagement hours."""
    slots = [11, 18, 20]  # local hours with decent reach; rotate
    out = []
    for i, p in enumerate(posts):
        day = i // per_day
        hour = slots[i % len(slots)]
        when = (start + timedelta(days=day)).replace(
            hour=hour, minute=0, second=0, microsecond=0)
        out.append({**p, "scheduled_at": when.isoformat()})
    return out


# ------------------------------ outputs -------------------------------------

def export_buffer_csv(scheduled: list[dict], path: str) -> str:
    """Buffer bulk-upload format: text + date columns."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["text", "scheduled_at", "platform"])
        for p in scheduled:
            w.writerow([p["text"], p["scheduled_at"], p["platform"]])
    return path


def export_review_file(scheduled: list[dict], path: str) -> str:
    """Human-checkpoint file: read this before anything gets queued."""
    with open(path, "w") as f:
        json.dump(scheduled, f, indent=2)
    return path


# --------------------- Meta Graph API poster (deploy-side) ------------------

META_GRAPH = "https://graph.facebook.com/v19.0"


def build_meta_requests(scheduled: list[dict]) -> list[dict]:
    """Construct the HTTP requests a Render cron job would fire. Returns
    request specs (no secrets embedded); the runner injects the token."""
    page_id = os.environ.get("META_PAGE_ID", "{META_PAGE_ID}")
    reqs = []
    for p in scheduled:
        if p["platform"] != "facebook":
            continue  # IG requires a media object flow; text-only goes to FB
        reqs.append({
            "method": "POST",
            "url": f"{META_GRAPH}/{page_id}/feed",
            "params": {
                "message": p["text"],
                "published": "false",
                "scheduled_publish_time": p["scheduled_at"],
                "access_token": "{META_ACCESS_TOKEN}",  # injected at runtime
            },
        })
    return reqs
