"""Tests for trending_service.py."""

from datetime import date, datetime, timedelta
from trending_service import TrendingService

MON = datetime(2026, 7, 13, 12, 0)   # Monday of the current week
REF = date(2026, 7, 17)              # Friday, same week


def main() -> None:
    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        passed += cond; failed += not cond
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    svc = TrendingService()
    for mid, title, mt in [
        ("m1", "Dune Part Three", "movie"),
        ("m2", "Scarpetta", "tv"),
        ("m3", "Avatar 4", "movie"),
        ("m4", "Indie Film", "movie"),
    ]:
        svc.register_media(mid, title, mt)

    # US: 40 users identify m1, 25 identify m2. One spammer hits m3 60 times.
    for i in range(40):
        svc.record_identification(f"us_{i}", "m1", "US", MON + timedelta(hours=i))
    for i in range(25):
        svc.record_identification(f"us_{i}", "m2", "US", MON + timedelta(hours=i))
    for _ in range(60):
        svc.record_identification("spammer", "m3", "US", MON + timedelta(minutes=5))

    t = svc.trending("US", "week", REF)
    check("US chart not fallback", t["fallback"] is False)
    check("rank 1 = m1 (40 users)", t["chart"][0]["media_id"] == "m1"
          and t["chart"][0]["unique_users"] == 40)
    spam_rank = next(c for c in t["chart"] if c["media_id"] == "m3")
    check("spammer counts once (60 events -> 1 user)",
          spam_rank["unique_users"] == 1, f"rank {spam_rank['rank']}")

    # Sparse region falls back to global
    for i in range(3):
        svc.record_identification(f"is_{i}", "m4", "IS", MON + timedelta(hours=i))
    t = svc.trending("IS", "week", REF)
    check("sparse region falls back to global", t["fallback"] is True
          and t["scope"] == "GLOBAL")
    check("fallback chart still ranked", t["chart"][0]["media_id"] == "m1")

    # Events outside the week are excluded
    svc.record_identification("old_user", "m4", "US", MON - timedelta(days=10))
    t = svc.trending("US", "week", REF)
    m4 = next((c for c in t["chart"] if c["media_id"] == "m4"), None)
    check("last week's events excluded", m4 is None or m4["unique_users"] == 0)

    # Monthly period includes both
    t = svc.trending("US", "month", REF)
    check("monthly chart returned", t["chart"][0]["unique_users"] >= 40)

    # Recommendations: co-occurrence, excludes seen
    svc.record_identification("us_1", "m3", "US", MON + timedelta(hours=2))
    recs = svc.recommend("us_0")
    check("recs exclude already-identified", all(r["media_id"] not in {"m1", "m2"} for r in recs))
    check("co-occurrence surfaces m3", any(r["media_id"] == "m3" for r in recs),
          str(recs))
    check("cold-start user gets empty recs", svc.recommend("nobody") == [])

    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
