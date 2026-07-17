"""
TrailerTap trending & recommendations service.

The data moat layer: every successful identification becomes an event
(user, media, region, time). From those events:

  1. trending()   — "most identified this week/month in your region"
     * counts DISTINCT users per title per period (one user tapping the
       same trailer 50x counts once — resists gaming/spam)
     * falls back to the global chart when a region is too sparse to be
       meaningful (early days, small markets)

  2. recommend()  — "users who identified X also identified Y"
     item co-occurrence over identification history; excludes what the
     user has already identified.

Storage: SQLite (swap for Postgres on Render with identical SQL).
Region should be derived server-side (IP geo) at match time — never
trust a client-supplied region for chart integrity.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta

MIN_REGION_EVENTS = 30   # below this, region chart falls back to global
DEFAULT_LIMIT = 10


class TrendingService:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.con = sqlite3.connect(db_path)
        self.con.executescript(
            """
            CREATE TABLE IF NOT EXISTS ident_events (
                user_id  TEXT NOT NULL,
                media_id TEXT NOT NULL,
                region   TEXT NOT NULL,   -- ISO country, server-derived
                ts       TEXT NOT NULL    -- ISO datetime UTC
            );
            CREATE INDEX IF NOT EXISTS idx_ev_region_ts
                ON ident_events (region, ts);
            CREATE INDEX IF NOT EXISTS idx_ev_user
                ON ident_events (user_id);
            CREATE TABLE IF NOT EXISTS media (
                media_id TEXT PRIMARY KEY,
                title    TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'movie'
            );
            """
        )

    # ------------------------------ ingest -------------------------------

    def register_media(self, media_id: str, title: str,
                       media_type: str = "movie") -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO media VALUES (?,?,?)",
            (media_id, title, media_type),
        )
        self.con.commit()

    def record_identification(self, user_id: str, media_id: str,
                              region: str, ts: datetime) -> None:
        self.con.execute(
            "INSERT INTO ident_events VALUES (?,?,?,?)",
            (user_id, media_id, region.upper(), ts.isoformat()),
        )
        self.con.commit()

    # ----------------------------- trending ------------------------------

    @staticmethod
    def _period_bounds(period: str, ref: date) -> tuple[str, str]:
        if period == "week":
            start = ref - timedelta(days=ref.weekday())      # Monday
            end = start + timedelta(days=7)
        elif period == "month":
            start = ref.replace(day=1)
            end = (start + timedelta(days=32)).replace(day=1)
        else:
            raise ValueError("period must be 'week' or 'month'")
        return start.isoformat(), end.isoformat()

    def trending(self, region: str, period: str = "week",
                 ref: date | None = None,
                 limit: int = DEFAULT_LIMIT) -> dict:
        ref = ref or date.today()
        lo, hi = self._period_bounds(period, ref)
        region = region.upper()

        def chart(where_region: str | None) -> list[dict]:
            clause = "AND region = ?" if where_region else ""
            params = [lo, hi] + ([where_region] if where_region else [])
            rows = self.con.execute(
                f"""
                SELECT e.media_id,
                       COALESCE(m.title, e.media_id) AS title,
                       COALESCE(m.media_type, 'movie') AS media_type,
                       COUNT(DISTINCT e.user_id) AS unique_users
                FROM ident_events e
                LEFT JOIN media m USING (media_id)
                WHERE e.ts >= ? AND e.ts < ? {clause}
                GROUP BY e.media_id
                ORDER BY unique_users DESC, e.media_id
                LIMIT {int(limit)}
                """,
                params,
            ).fetchall()
            return [
                {"rank": i + 1, "media_id": r[0], "title": r[1],
                 "media_type": r[2], "unique_users": r[3]}
                for i, r in enumerate(rows)
            ]

        n_region = self.con.execute(
            "SELECT COUNT(*) FROM ident_events "
            "WHERE ts >= ? AND ts < ? AND region = ?",
            (lo, hi, region),
        ).fetchone()[0]

        if n_region >= MIN_REGION_EVENTS:
            return {"scope": region, "period": period,
                    "fallback": False, "chart": chart(region)}
        return {"scope": "GLOBAL", "period": period,
                "fallback": True, "requested_region": region,
                "chart": chart(None)}

    # -------------------------- recommendations --------------------------

    def recommend(self, user_id: str, limit: int = 5) -> list[dict]:
        """Item co-occurrence: score titles identified by users who share
        identifications with this user; exclude titles already seen."""
        seen = {r[0] for r in self.con.execute(
            "SELECT DISTINCT media_id FROM ident_events WHERE user_id = ?",
            (user_id,),
        )}
        if not seen:
            return []
        marks = ",".join("?" * len(seen))
        rows = self.con.execute(
            f"""
            SELECT e2.media_id, COUNT(DISTINCT e2.user_id) AS score
            FROM ident_events e1
            JOIN ident_events e2
              ON e1.user_id = e2.user_id
             AND e2.media_id NOT IN ({marks})
            WHERE e1.media_id IN ({marks}) AND e1.user_id != ?
            GROUP BY e2.media_id
            ORDER BY score DESC, e2.media_id
            LIMIT {int(limit)}
            """,
            list(seen) + list(seen) + [user_id],
        ).fetchall()
        out = []
        for media_id, score in rows:
            title_row = self.con.execute(
                "SELECT title FROM media WHERE media_id = ?", (media_id,)
            ).fetchone()
            out.append({
                "media_id": media_id,
                "title": title_row[0] if title_row else media_id,
                "score": score,
            })
        return out
