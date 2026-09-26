"""Bounded, secret-free monotonic-clock measurement for the RAG query path.

WP1 ("once olc, sonra duzelt") requires per-query counters over the critical
repeated-cost findings without pulling a second observability framework in and
without changing the public command JSON contract.  This module provides:

* a lightweight :class:`QueryCounters` accumulator,
* a thread-local active-scope context manager (:func:`scope`,
  :func:`active`), and
* a tolerance-aware vector compatibility helper used to lock the B06
  profile-identity contract.

Every value is derived from ``time.monotonic()`` and publicly safe identifiers
and digests.  No secrets, credentials, raw query/source text, token data or raw
provider responses are ever stored here; these counters must never be folded
into a semantic identity or authority digest (see AKTIF_GOREV section 5).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class QueryCounters:
    """Monotonic counters and per-channel status for one query execution.

    ``channel`` names are the retrieval channel strings (``exact``,
    ``lexical``, ``dense``).  A channel that was attempted but failed is
    recorded under ``attempted`` without a corresponding ``completed`` entry,
    so "dense is open" and "dense actually completed" stay distinct.
    """

    # B01: how many times provider qualification (probe) ran for this query.
    qualification_count: int = 0
    # B03: how many index deep validations (quick_check/foreign_key_check) ran.
    index_deep_validate_count: int = 0
    # WP3 (B02/B03 freshness): how many bounded source-freshness scans ran for a
    # query (a git/status/manifest comparison or a bounded changed-content hash).
    # Diagnostic only; never folded into a semantic identity or authority digest.
    source_freshness_scan_count: int = 0
    # Whether each channel was attempted and whether it completed.
    channel_attempted: dict[str, bool] = field(default_factory=dict)
    channel_completed: dict[str, bool] = field(default_factory=dict)
    # Monotonic latency snapshots (ms).  None when the stage never ran.
    probe_latency_ms: int | None = None
    embed_latency_ms: int | None = None

    def mark_channel_attempted(self, channel: str) -> None:
        self.channel_attempted[channel] = True

    def mark_channel_skipped(self, channel: str) -> None:
        """Record that a channel was deterministically skipped for this query.

        A skipped channel is recorded under ``attempted`` as ``False`` and gets
        no ``completed`` entry, so "dense was never attempted" (fast path) stays
        distinct from "dense was attempted but failed".
        """
        self.channel_attempted[channel] = False

    def mark_channel_completed(self, channel: str) -> None:
        self.channel_attempted[channel] = True
        self.channel_completed[channel] = True

    def snapshot(self) -> dict[str, Any]:
        """Secret-free diagnostic dict; never include in a semantic digest."""
        return {
            "qualification_count": self.qualification_count,
            "index_deep_validate_count": self.index_deep_validate_count,
            "source_freshness_scan_count": self.source_freshness_scan_count,
            "channel_attempted": dict(self.channel_attempted),
            "channel_completed": dict(self.channel_completed),
            "probe_latency_ms": self.probe_latency_ms,
            "embed_latency_ms": self.embed_latency_ms,
        }


_thread_local = threading.local()


class _Scope:
    """Context-managed active counter scope; the snapshot is retained on exit."""

    def __init__(self) -> None:
        self.counters = QueryCounters()
        self._snapshot: dict[str, Any] | None = None

    def __enter__(self) -> QueryCounters:
        previous = getattr(_thread_local, "active", None)
        self._previous = previous
        _thread_local.active = self.counters
        return self.counters

    def __exit__(self, *_args: object) -> None:
        _thread_local.active = self._previous
        self._snapshot = self.counters.snapshot()
        _thread_local.last = self._snapshot

    def snapshot(self) -> dict[str, Any]:
        if self._snapshot is None:
            self._snapshot = self.counters.snapshot()
        return self._snapshot


def scope() -> _Scope:
    """Enter a fresh per-query measurement scope (thread-local, nested-safe)."""
    return _Scope()


def active() -> QueryCounters | None:
    """Return the counters of the active scope, or ``None`` outside one."""
    return getattr(_thread_local, "active", None)


def last_counters() -> dict[str, Any] | None:
    """Return the most recently *completed* scope snapshot (test seam).

    This is a diagnostic back-channel only; it never feeds the public command
    output contract and holds no secret or raw-transcript data.
    """
    return getattr(_thread_local, "last", None)


def now_ms() -> int:
    """Monotonic wall-free elapsed milliseconds since an arbitrary origin."""
    return max(0, time.monotonic_ns() // 1_000_000)


def record_qualification() -> None:
    counter = active()
    if counter is not None:
        counter.qualification_count += 1


def record_deep_validate() -> None:
    counter = active()
    if counter is not None:
        counter.index_deep_validate_count += 1


def record_source_freshness_scan() -> None:
    """Record one bounded source-freshness scan on the query path (WP3)."""
    counter = active()
    if counter is not None:
        counter.source_freshness_scan_count += 1


def record_probe_latency(ms: int) -> None:
    counter = active()
    if counter is not None:
        counter.probe_latency_ms = ms


def record_embed_latency(ms: int) -> None:
    counter = active()
    if counter is not None:
        counter.embed_latency_ms = ms


def quantized_component(
    value: float,
    scale: int = 500,
    bucket_boundary: bool = True,
) -> int:
    """Quantize a single component with a tolerance-stable bucket.

    ``scale`` is buckets per unit; the bucket width ``1/scale`` is chosen wider
    than the accepted probe tolerance (BGE probe jitter ~ max_delta <= 5e-4).
    Components are mapped to the nearest bucket centre (``round``).  Centring
    the buckets keeps the *maximum* normalized component (1.0) stable under
    sub-bucket jitter: ``round(0.99999999*scale) == round(1.0*scale)``, which a
    plain ``floor`` quantization breaks because 1.0 always sits on the top
    boundary.  The baseline ``round(value*1000)`` was neither centred-safe nor
    tolerance-aware (it flipped 0.4e-3 vs 0.5e-3 into different buckets).
    """
    del bucket_boundary
    return round(value * scale)


def quantized_vector_fingerprint(
    vector: tuple[float, ...],
    *,
    scale: int = 500,
    tolerance: float | None = None,
) -> tuple[int, ...]:
    """Tolerance-aware per-component fingerprint (stable under accepted jitter).

    Two vectors accepted as tolerance-compatible must NOT produce a different
    profile identity (task B06 #1: do NOT use raw ``round(value*1000)`` vector
    hash equality as tolerance compatibility).  Each component is mapped to the
    nearest bucket centre with bucket width ``1/scale`` wider than the accepted
    probe tolerance, so:
      * numeric jitter (probe repeat / batch jitter, max_delta <= 5e-4) stays
        inside one bucket and does not flip the identity, including at the unit
        maximum (``round`` centring), and
      * a genuine model/embedding-space change (material shift) moves a
        component across one or more bucket boundaries and changes the
        fingerprint.

    Default ``scale`` (500) is intentionally coarser than the naive 1000 so the
    bucket width absorbs the accepted jitter while still resolving a material
    shift (a 0.2 shift moves the component by ~100 buckets).  ``tolerance`` is
    accepted for symmetry but the bucket width is the operative bound.
    """
    del tolerance
    return tuple(round(value * scale) for value in vector)


def vectors_compatible(
    left: tuple[float, ...],
    right: tuple[float, ...],
    *,
    max_delta: float,
    min_cosine: float,
) -> bool:
    """Tolerance-aware compatibility: accept small numeric jitter, reject real drift.

    This is the WP2 intended mechanism for B06.  Two normalized vectors that
    differ only within ``max_delta`` while keeping cosine at or above
    ``min_cosine`` belong to the same embedding space and must not change the
    profile identity.  A genuine space change (model/endpoint/dimension) will
    violate one of the bounds and must be rejected rather than silently folded.
    """
    if len(left) != len(right):
        return False
    if any(not math.isfinite(v) for v in (*left, *right)):
        return False
    max_delta_observed = max(abs(a - b) for a, b in zip(left, right, strict=True))
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return False
    cosine = dot / (left_norm * right_norm)
    return max_delta_observed <= max_delta and cosine >= min_cosine
