"""
TrailerTap Fingerprint Engine
Shazam-style audio fingerprinting (Wang, 2003 constellation/combinatorial hashing).

Pipeline:
  audio -> STFT spectrogram -> peak constellation -> anchor/target hash pairs
        -> inverted index {hash: [(track_id, t_anchor)]}
  query -> same hashes -> offset-histogram vote -> aligned-cluster match

Storage: RedisLikeStore (in-memory, Redis-compatible API surface) with
optional SQLite persistence. Swap in redis.Redis with the same 3 methods
(sadd/smembers semantics via add/get) when Redis is available in prod.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np
from scipy import signal
from scipy.ndimage import maximum_filter

# ----------------------------- Tunables -------------------------------------

SAMPLE_RATE = 11025          # Hz. Downsampled: trailer ID needs < 5.5 kHz content
WINDOW_SIZE = 2048           # STFT window (~186 ms at 11025 Hz)
HOP_SIZE = 512               # 75% overlap
PEAK_NEIGHBORHOOD = (19, 7)  # (freq_bins, time_frames) local-max footprint
MIN_PEAK_AMPLITUDE_DB = -55  # discard peaks below this (relative to max)
FAN_OUT = 32                 # target-zone pairs per anchor
TARGET_ZONE_DT = (2, 80)     # frames ahead of anchor eligible for pairing
FREQ_QUANT = 2               # quantize freq bins: absorbs +/- jitter from noise
DT_QUANT = 2                 # quantize frame deltas for the same reason
MATCH_MIN_VOTES = 8          # min aligned hashes to declare a match
MATCH_MIN_MARGIN = 2.0       # winner must beat runner-up by this factor
MAX_POSTINGS = 24            # stop-hash cap: over-common hashes carry no signal
OFFSET_QUANT = 4             # bucket vote offsets so jittered anchors align


# ----------------------------- Storage --------------------------------------

class RedisLikeStore:
    """In-memory inverted index with the minimal API a Redis-backed
    implementation would expose. hash -> list[(track_id, t_anchor)]."""

    def __init__(self) -> None:
        self._index: dict[str, list[tuple[str, int]]] = defaultdict(list)
        self._tracks: dict[str, str] = {}  # track_id -> title

    def add(self, h: str, track_id: str, t_anchor: int) -> None:
        self._index[h].append((track_id, t_anchor))

    def get(self, h: str) -> list[tuple[str, int]]:
        return self._index.get(h, [])

    def register_track(self, track_id: str, title: str) -> None:
        self._tracks[track_id] = title

    def track_title(self, track_id: str) -> str:
        return self._tracks.get(track_id, track_id)

    @property
    def n_hashes(self) -> int:
        return sum(len(v) for v in self._index.values())

    # ---- persistence (SQLite stand-in for Redis RDB/AOF) ----

    def save(self, path: str) -> None:
        con = sqlite3.connect(path)
        con.executescript(
            "DROP TABLE IF EXISTS hashes; DROP TABLE IF EXISTS tracks;"
            "CREATE TABLE hashes (h TEXT, track_id TEXT, t INTEGER);"
            "CREATE TABLE tracks (track_id TEXT PRIMARY KEY, title TEXT);"
            "CREATE INDEX idx_h ON hashes (h);"
        )
        con.executemany(
            "INSERT INTO hashes VALUES (?,?,?)",
            [(h, tid, t) for h, lst in self._index.items() for tid, t in lst],
        )
        con.executemany("INSERT INTO tracks VALUES (?,?)", self._tracks.items())
        con.commit()
        con.close()

    @classmethod
    def load(cls, path: str) -> "RedisLikeStore":
        store = cls()
        con = sqlite3.connect(path)
        for h, tid, t in con.execute("SELECT h, track_id, t FROM hashes"):
            store._index[h].append((tid, t))
        for tid, title in con.execute("SELECT track_id, title FROM tracks"):
            store._tracks[tid] = title
        con.close()
        return store


# ------------------------- Signal processing --------------------------------

def spectrogram(samples: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Time-domain -> log-magnitude frequency-domain via STFT (FFT windows)."""
    _, _, sxx = signal.stft(
        samples, fs=sr, nperseg=WINDOW_SIZE, noverlap=WINDOW_SIZE - HOP_SIZE,
        window="hann", padded=False, boundary=None,
    )
    mag = np.abs(sxx)
    return 20 * np.log10(mag + 1e-10)  # dB scale


# Band edges over STFT bins. Start at bin 19 (~100 Hz): sub-100 Hz is
# noise floor / rumble with no trailer-identifying content, and its tiny
# quantized hash space floods the index with false collisions.
BAND_EDGES = [19, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1025]


def constellation(spec_db: np.ndarray) -> list[tuple[int, int]]:
    """Loudest energy peak per log-frequency band per time frame, kept only
    if it beats the band's rolling amplitude — stable under additive noise
    (the strongest bin in a wide band barely moves when noise is added)."""
    n_bins, n_frames = spec_db.shape
    floor = spec_db.max() + MIN_PEAK_AMPLITUDE_DB
    peaks: list[tuple[int, int]] = []
    for lo, hi in zip(BAND_EDGES[:-1], BAND_EDGES[1:]):
        band = spec_db[lo:hi, :]                    # (band_bins, frames)
        best_bin = band.argmax(axis=0)              # strongest bin per frame
        best_amp = band.max(axis=0)
        # Keep frames where the band peak is a local max in time and loud
        # enough vs. the band's own mean level (adaptive threshold).
        adaptive = best_amp.mean() - 0.25 * best_amp.std()
        keep = (best_amp > max(floor, adaptive))
        # Temporal non-max suppression within the band
        half_w = PEAK_NEIGHBORHOOD[1] // 2
        for t in np.nonzero(keep)[0]:
            a, b = max(0, t - half_w), min(n_frames, t + half_w + 1)
            if best_amp[t] >= best_amp[a:b].max():
                peaks.append((lo + int(best_bin[t]), int(t)))
    peaks.sort(key=lambda p: p[1])
    return peaks


def hash_pairs(peaks: list[tuple[int, int]]) -> list[tuple[str, int]]:
    """Combine anchor->target peak pairs into (hash, t_anchor) vectors.
    Hash encodes (f_anchor, f_target, dt) — Wang's combinatorial scheme."""
    out: list[tuple[str, int]] = []
    lo, hi = TARGET_ZONE_DT
    for i, (f1, t1) in enumerate(peaks):
        fanned = 0
        for f2, t2 in peaks[i + 1:]:
            dt = t2 - t1
            if dt < lo:
                continue
            if dt > hi or fanned >= FAN_OUT:
                break
            raw = f"{f1 // FREQ_QUANT}|{f2 // FREQ_QUANT}|{dt // DT_QUANT}".encode()
            out.append((hashlib.sha1(raw).hexdigest()[:16], t1))
            fanned += 1
    return out


def fingerprint(samples: np.ndarray, sr: int = SAMPLE_RATE) -> list[tuple[str, int]]:
    """Full pipeline: raw audio -> list of (hash, anchor_time_frame)."""
    if sr != SAMPLE_RATE:
        n = int(len(samples) * SAMPLE_RATE / sr)
        samples = signal.resample(samples, n)
    peak_list = constellation(spectrogram(samples))
    return hash_pairs(peak_list)


# ----------------------------- Engine API -----------------------------------

@dataclass
class Match:
    track_id: str
    title: str
    votes: int
    offset_seconds: float   # position in the source trailer where clip starts
    confidence: float       # winner votes / runner-up votes


class FingerprintEngine:
    def __init__(self, store: RedisLikeStore | None = None) -> None:
        self.store = store or RedisLikeStore()

    def register(self, track_id: str, title: str,
                 samples: np.ndarray, sr: int = SAMPLE_RATE) -> int:
        """Phase 1: fingerprint full trailer audio into the index."""
        self.store.register_track(track_id, title)
        hashes = fingerprint(samples, sr)
        for h, t in hashes:
            self.store.add(h, track_id, t)
        return len(hashes)

    def identify(self, samples: np.ndarray, sr: int = SAMPLE_RATE) -> Match | None:
        """Phase 2: match a 5-10 s mic capture against the index."""
        query = fingerprint(samples, sr)
        if not query:
            return None

        # Vote at exact frame offsets; bucketed view finds candidates,
        # exact view verifies them.
        exact: Counter[tuple[str, int]] = Counter()
        votes: Counter[tuple[str, int]] = Counter()
        for h, t_q in query:
            postings = self.store.get(h)
            if len(postings) > MAX_POSTINGS:
                continue  # stop-hash: too common to be discriminative
            for track_id, t_db in postings:
                exact[(track_id, t_db - t_q)] += 1
        for (tid, o), c in exact.items():
            votes[(tid, o // OFFSET_QUANT)] += c
        if not votes:
            return None

        # Candidate generation: top bucketed clusters (merged over neighbors).
        merged: Counter[tuple[str, int]] = Counter()
        for (tid, o), c in votes.items():
            merged[(tid, o)] = (
                votes.get((tid, o - 1), 0) + c + votes.get((tid, o + 1), 0)
            )

        def tight_score(tid: str, bucket: int) -> tuple[int, int]:
            """Best +/-2-frame aligned vote concentration within the bucket
            neighborhood. True matches concentrate; noise clusters smear."""
            lo = (bucket - 1) * OFFSET_QUANT
            hi = (bucket + 2) * OFFSET_QUANT
            best, best_o = 0, lo
            for o in range(lo, hi):
                s = sum(exact.get((tid, o + d), 0) for d in (-2, -1, 0, 1, 2))
                if s > best:
                    best, best_o = s, o
            return best, best_o

        candidates = merged.most_common(8)
        scored = []
        for (tid, bucket), _ in candidates:
            s, o = tight_score(tid, bucket)
            scored.append((s, tid, o))
        scored.sort(reverse=True)

        top, track_id, offset_frames = scored[0]
        runner_up = next(
            (s for s, tid, o in scored[1:]
             if tid != track_id or abs(o - offset_frames) > 3 * OFFSET_QUANT), 0,
        )
        margin = top / max(runner_up, 1)

        if top < MATCH_MIN_VOTES or margin < MATCH_MIN_MARGIN:
            return None  # reject: no strong aligned cluster

        frame_seconds = HOP_SIZE / SAMPLE_RATE
        return Match(
            track_id=track_id,
            title=self.store.track_title(track_id),
            votes=top,
            offset_seconds=round(offset_frames * frame_seconds, 2),
            confidence=round(margin, 2),
        )


# --------------------- Phase 3: monetization routing -------------------------
# Deliberately credential-free: IDs come from environment/config at deploy
# time. Do NOT hardcode affiliate keys, and do not surface discount copy
# until codes are live.

AFFILIATE_TEMPLATES = {
    "fandango": "https://www.fandango.com/{slug}?a={affiliate_id}",
    "atom":     "https://www.atomtickets.com/movies/{slug}?ref={affiliate_id}",
}


def build_affiliate_link(provider: str, slug: str, affiliate_id: str) -> str:
    if provider not in AFFILIATE_TEMPLATES:
        raise ValueError(f"Unknown provider: {provider}")
    if not affiliate_id:
        raise ValueError("Affiliate ID missing — refusing to emit untracked link")
    return AFFILIATE_TEMPLATES[provider].format(slug=slug, affiliate_id=affiliate_id)
