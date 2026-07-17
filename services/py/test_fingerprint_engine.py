"""Validation suite for fingerprint_engine.py — synthetic audio, no network."""

import os
import numpy as np
from fingerprint_engine import (
    FingerprintEngine, RedisLikeStore, build_affiliate_link, SAMPLE_RATE,
)

rng = np.random.default_rng(42)


def synth_trailer(seed: int, seconds: int = 45) -> np.ndarray:
    """Synthetic 'trailer': dense chirp events over a per-seed harmonic music
    bed + noise floor. Density approximates real trailer audio (continuous
    music/VO/FX), which is what makes 8 s windows discriminative."""
    r = np.random.default_rng(seed)
    n = seconds * SAMPLE_RATE
    t = np.arange(n) / SAMPLE_RATE
    audio = 0.02 * r.standard_normal(n)
    # Harmonic bed: chord tones changing every ~2 s, unique per seed
    for seg in range(0, seconds, 2):
        a, b = seg * SAMPLE_RATE, min(n, (seg + 2) * SAMPLE_RATE)
        root = r.uniform(110, 440)
        for mult in (1.0, 1.5, 2.0, 3.0):
            audio[a:b] += 0.15 * np.sin(2 * np.pi * root * mult * t[a:b]
                                        + r.uniform(0, 2 * np.pi))
    # Dense events (~8/second)
    for _ in range(seconds * 8):
        start = r.integers(0, n - SAMPLE_RATE)
        dur = int(r.uniform(0.1, 0.6) * SAMPLE_RATE)
        f0, f1 = r.uniform(100, 4500, 2)
        seg_t = t[:dur]
        sweep = np.sin(2 * np.pi * (f0 + (f1 - f0) * seg_t / seg_t[-1] / 2) * seg_t)
        audio[start:start + dur] += r.uniform(0.3, 1.0) * sweep * np.hanning(dur)
    return audio / np.abs(audio).max()


def mic_capture(trailer: np.ndarray, start_s: float, clip_s: float = 8.0,
                snr_db: float = 10.0) -> np.ndarray:
    """Simulate a phone-mic capture: excerpt + additive noise at given SNR."""
    a, b = int(start_s * SAMPLE_RATE), int((start_s + clip_s) * SAMPLE_RATE)
    clip = trailer[a:b].copy()
    sig_pow = np.mean(clip ** 2)
    noise_pow = sig_pow / (10 ** (snr_db / 10))
    return clip + np.sqrt(noise_pow) * rng.standard_normal(len(clip))


def main() -> None:
    passed = failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        status = "PASS" if cond else "FAIL"
        passed += cond
        failed += not cond
        print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))

    # ---- Phase 1: build corpus ----
    engine = FingerprintEngine()
    corpus = {f"mov_{i:03d}": (f"Trailer #{i}", synth_trailer(seed=i)) for i in range(1, 6)}
    for tid, (title, audio) in corpus.items():
        n = engine.register(tid, title, audio)
        check(f"register {tid}", n > 500, f"{n} hashes")
    print(f"       index size: {engine.store.n_hashes} hashes\n")

    # ---- Phase 2: identify noisy clips ----
    for tid, (_, audio) in corpus.items():
        start = float(rng.uniform(3, 30))
        m = engine.identify(mic_capture(audio, start_s=start, snr_db=10))
        ok = m is not None and m.track_id == tid and abs(m.offset_seconds - start) < 1.0
        check(f"identify {tid} @ {start:.1f}s, 10 dB SNR", ok,
              f"got {m.track_id} @ {m.offset_seconds}s, {m.votes} votes, "
              f"conf {m.confidence}x" if m else "no match")

    # Harsher noise (0 dB SNR — noise as loud as signal)
    tid = "mov_003"
    m = engine.identify(mic_capture(corpus[tid][1], start_s=12.0, snr_db=0))
    check("identify at 0 dB SNR", m is not None and m.track_id == tid,
          f"{m.votes} votes" if m else "no match")

    # ---- Rejection: unknown audio must NOT match ----
    unknown = synth_trailer(seed=999)
    m = engine.identify(mic_capture(unknown, start_s=5.0))
    check("reject unknown trailer", m is None, "no false positive")

    m = engine.identify(0.05 * rng.standard_normal(8 * SAMPLE_RATE))
    check("reject pure noise", m is None)

    # ---- Persistence round-trip (SQLite standing in for Redis) ----
    db = "/home/claude/fingerprints.db"
    engine.store.save(db)
    engine2 = FingerprintEngine(RedisLikeStore.load(db))
    m = engine2.identify(mic_capture(corpus["mov_001"][1], start_s=8.0))
    check("persistence round-trip", m is not None and m.track_id == "mov_001",
          f"{os.path.getsize(db)//1024} KB db")

    # ---- Strategic risk demo: licensed-music collision ----
    song = synth_trailer(seed=777, seconds=30)          # a "hit song"
    trailer_with_song = synth_trailer(seed=5, seconds=45)
    trailer_with_song[:30 * SAMPLE_RATE] += 0.8 * song  # trailer uses the song
    trailer_with_song /= np.abs(trailer_with_song).max()
    eng3 = FingerprintEngine()
    eng3.register("song_777", "Hit Song (label catalog)", song)
    eng3.register("mov_777", "Trailer using Hit Song", trailer_with_song)
    m = eng3.identify(mic_capture(trailer_with_song, start_s=10.0))
    print(f"[INFO] music-collision demo: clip from trailer matched "
          f"'{m.title}' ({m.votes} votes, conf {m.confidence}x)"
          if m else "[INFO] music-collision demo: ambiguous — match rejected")

    # ---- Phase 3: affiliate link guardrails ----
    url = build_affiliate_link("fandango", "dune-part-three", "AFF123")
    check("affiliate link built", "a=AFF123" in url, url)
    try:
        build_affiliate_link("fandango", "x", "")
        check("refuse untracked link", False)
    except ValueError:
        check("refuse untracked link", True, "raises without affiliate ID")

    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
