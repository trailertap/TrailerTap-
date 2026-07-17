"""Tests for result_actions.py — including a regression guard for the
TV-series-missing-watch-button bug (#171234 class)."""

from datetime import date
from result_actions import MediaItem, build_result_actions

AFF = {"fandango": "AFF123"}
TODAY = date(2026, 7, 17)


def main() -> None:
    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        passed += cond; failed += not cond
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    # Movie: ticket link + day-before invite
    movie = MediaItem("m1", "Dune Part Three", "movie",
                      date(2026, 12, 18), slug="dune-part-three")
    r = build_result_actions(movie, AFF, TODAY)
    check("movie CTA is Fandango affiliate", "fandango.com" in r["primary_cta"]["url"]
          and "a=AFF123" in r["primary_cta"]["url"])
    check("movie invite on day before", "DTSTART;VALUE=DATE:20261217" in (r["calendar_invite"] or ""))
    check("no discount copy in CTA", "%" not in r["primary_cta"]["label"]
          and "off" not in r["primary_cta"]["label"].lower())

    # TV with direct URL: REGRESSION GUARD — watch button must exist
    tv = MediaItem("t1", "Scarpetta", "tv", date(2026, 8, 1),
                   platform="Amazon Prime Video",
                   streaming_url="https://www.primevideo.com/detail/scarpetta")
    r = build_result_actions(tv, AFF, TODAY)
    check("TV CTA present (bug #171234 guard)", bool(r["primary_cta"]["url"]))
    check("TV CTA labeled with platform", "Amazon Prime Video" in r["primary_cta"]["label"])
    check("TV invite on day before", "DTSTART;VALUE=DATE:20260731" in (r["calendar_invite"] or ""))

    # TV with NO streaming URL: must fall back, never render buttonless
    tv2 = MediaItem("t2", "Unknown Show", "tv", date(2026, 9, 1), platform="Hulu")
    r = build_result_actions(tv2, AFF, TODAY)
    check("TV fallback CTA when URL missing", r["primary_cta"]["url"].startswith("http"),
          r["primary_cta"]["label"])

    # Already released: no invite, status flips
    old = MediaItem("m2", "Old Movie", "movie", date(2026, 1, 1), slug="old-movie")
    r = build_result_actions(old, AFF, TODAY)
    check("released: no invite", r["calendar_invite"] is None and r["status"] == "released")

    # Releases today: no day-before invite (it was yesterday)
    today_rel = MediaItem("m3", "Today Movie", "movie", TODAY, slug="today-movie")
    r = build_result_actions(today_rel, AFF, TODAY)
    check("releases today: no stale invite", r["calendar_invite"] is None)

    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
