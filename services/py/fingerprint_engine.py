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
    """In-memory
