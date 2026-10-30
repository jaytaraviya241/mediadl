"""Metrics facade (spec section 9.5).

Counters and histograms are recorded through this module so the core never
imports an exporter. Without ``prometheus-client`` installed the values are
kept in memory, which keeps the dependency optional and makes the counters
assertable in tests.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Mapping

try:  # pragma: no cover - exercised by whichever branch is installed
    from prometheus_client import Counter as _Counter
    from prometheus_client import Histogram as _Histogram

    _PROMETHEUS = True
except ImportError:  # pragma: no cover
    _PROMETHEUS = False

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_observations: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
_registered: dict[str, object] = {}

#: name -> (documentation, label names)
DEFINITIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    'mediadl_jobs_total': ('Jobs by final state', ('connector', 'final_state')),
    'mediadl_job_duration_seconds': ('Job wall-clock duration', ('connector', 'policy')),
    'mediadl_connector_errors_total': ('Connector errors', ('connector', 'error_code')),
    'mediadl_token_refresh_total': ('Token refresh attempts', ('connector', 'result')),
    'mediadl_transfer_bytes_total': ('Bytes transferred', ('engine',)),
    'mediadl_retries_total': ('Retries performed', ('engine', 'reason')),
    'mediadl_resume_total': ('Resume outcomes', ('outcome',)),
    'mediadl_rate_limited_total': ('Rate-limit events', ('connector',)),
    'mediadl_processing_total': ('Processing operations', ('operation', 'stream_copy', 'result')),
    'mediadl_collection_items_total': ('Collection items by status', ('status',)),
    'mediadl_quota_rejections_total': ('Quota rejections', ('tenant_tier',)),
    'mediadl_lease_expired_total': ('Leases reclaimed after expiry', ()),
}


def _key(name: str, labels: Mapping[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return name, tuple(sorted((k, str(v)) for k, v in labels.items()))


def increment(name: str, value: float = 1.0, **labels: str) -> None:
    with _lock:
        _counters[_key(name, labels)] += value
    if _PROMETHEUS and name in DEFINITIONS:  # pragma: no cover - optional path
        metric = _registered.get(name)
        if metric is None:
            doc, label_names = DEFINITIONS[name]
            metric = _Counter(name, doc, label_names)
            _registered[name] = metric
        (metric.labels(**labels) if labels else metric).inc(value)  # type: ignore[union-attr]


def observe(name: str, value: float, **labels: str) -> None:
    with _lock:
        _observations[_key(name, labels)].append(value)
    if _PROMETHEUS and name in DEFINITIONS:  # pragma: no cover - optional path
        metric = _registered.get(name)
        if metric is None:
            doc, label_names = DEFINITIONS[name]
            metric = _Histogram(name, doc, label_names)
            _registered[name] = metric
        (metric.labels(**labels) if labels else metric).observe(value)  # type: ignore[union-attr]


def snapshot() -> dict[str, float]:
    """Flat view of the in-memory counters, for tests and `mediadl doctor`."""
    with _lock:
        flat = {
            name + (''.join(f'{{{k}={v}}}' for k, v in labels) if labels else ''): total
            for (name, labels), total in _counters.items()
        }
        for (name, labels), values in _observations.items():
            suffix = ''.join(f'{{{k}={v}}}' for k, v in labels) if labels else ''
            flat[f'{name}{suffix}_count'] = float(len(values))
            flat[f'{name}{suffix}_sum'] = float(sum(values))
    return flat


def reset() -> None:
    with _lock:
        _counters.clear()
        _observations.clear()
