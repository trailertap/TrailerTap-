"""
TrailerTap Python services API — deploys as a second Render web service
alongside the existing Node.js backend.

Endpoints:
  GET  /health                     — uptime probe (point Better Stack here)
  POST /admin/register             — index a trailer (WAV upload; token-gated)
  POST /identify                   — WAV clip -> match + result actions
  POST /events                     — record an identification event
  GET  /trending?region=US&period=week
  GET  /recommend?user_id=...

Env vars (set in Render dashboard, never in code):
  ADMIN_TOKEN          — required for /admin/* routes
  FANDANGO_AFFILIATE_ID — affiliate tracking id (leave unset until live)
  DB_DIR               — persistent disk mount path (default /var/data)
"""

from __future__ import annotations

import os
import io
import wave
from datetime import date, datetime, timezone

import numpy as np
from flask import Flask, jsonify, request

from fingerprint_engine import FingerprintEngine, RedisLikeStore
from result_actions import MediaItem, build_result_actions
from trending_service import TrendingService

DB_DIR = os.environ.get("DB_DIR", "/var/data")
FP_DB = os.path.join(DB_DIR, "fingerprints.db")
TR_DB = os.path.join(DB_DIR, "trending.db")

app = Flask(__name__)
os.makedirs(DB_DIR, exist_ok=True)

engine = FingerprintEngine(
    RedisLikeStore.load(FP_DB) if os.path.exists(FP_DB) else None
)
trending = TrendingService(TR_DB)
MEDIA: dict[str, dict] = {}  # media_id -> metadata (mirror of Node's catalog)


def _wav_to_samples(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data)) as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        width = w.getsampwidth()
        ch = w.getnchannels()
    dtype = {1: np.int8, 2: np.int16, 4: np.int32}[width]
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float64)
    if ch > 1:
        samples = samples.reshape(-1, ch).mean(axis=1)
    return samples / np.abs(samples).max(), sr


def _require_admin() -> bool:
    want = os.environ.get("ADMIN_TOKEN")
    return bool(want) and request.headers.get("X-Admin-Token") == want


@app.get("/health")
def health():
    return jsonify(status="ok", indexed_hashes=engine.store.n_hashes)


@app.post("/admin/register")
def register():
    if not _require_admin():
        return jsonify(error="unauthorized"), 401
    meta = {k: request.form[k] for k in
            ("media_id", "title", "media_type", "release_date")}
    meta["platform"] = request.form.get("platform", "")
    meta["slug"] = request.form.get("slug", "")
    meta["streaming_url"] = request.form.get("streaming_url", "")
    samples, sr = _wav_to_samples(request.files["audio"].read())
    n = engine.register(meta["media_id"], meta["title"], samples, sr)
    MEDIA[meta["media_id"]] = meta
    trending.register_media(meta["media_id"], meta["title"], meta["media_type"])
    engine.store.save(FP_DB)
    return jsonify(indexed=n, media_id=meta["media_id"])


@app.post("/identify")
def identify():
    if "audio" not in request.files:
        return jsonify(error="upload a WAV file as 'audio'"), 400
    samples, sr = _wav_to_samples(request.files["audio"].read())
    m = engine.identify(samples, sr)
    if m is None:
        return jsonify(matched=False), 200
    meta = MEDIA.get(m.track_id)
    payload = {"matched": True, "media_id": m.track_id, "title": m.title,
               "votes": m.votes, "confidence": m.confidence,
               "offset_seconds": m.offset_seconds}
    if meta:
        item = MediaItem(
            media_id=m.track_id, title=meta["title"],
            media_type=meta["media_type"],
            release_date=date.fromisoformat(meta["release_date"]),
            platform=meta["platform"], slug=meta["slug"],
            streaming_url=meta["streaming_url"],
        )
        aff = {"fandango": os.environ.get("FANDANGO_AFFILIATE_ID", "")}
        try:
            payload["actions"] = build_result_actions(item, aff, date.today())
        except ValueError:
            payload["actions"] = None  # affiliate id not configured yet
    return jsonify(payload)


@app.post("/events")
def record_event():
    body = request.get_json(force=True)
    # Region derived server-side: Render sets client IP headers; resolve
    # via your geo lookup in the Node layer and forward, or default here.
    region = body.get("region", "US")
    trending.record_identification(
        body["user_id"], body["media_id"], region,
        datetime.now(timezone.utc),
    )
    return jsonify(recorded=True)


@app.get("/trending")
def get_trending():
    return jsonify(trending.trending(
        request.args.get("region", "US"),
        request.args.get("period", "week"),
    ))


@app.get("/recommend")
def get_recommend():
    uid = request.args.get("user_id", "")
    return jsonify(recommendations=trending.recommend(uid))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
