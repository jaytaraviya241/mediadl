"""Temp sweeping and the metrics facade (spec sections 7.9, 9.5)."""
from __future__ import annotations

import os
import time

from mediadl.obs import metrics
from mediadl.storage.retention import sweep_temp


async def test_sweep_removes_only_old_orphans(tmp_path):
    root = tmp_path / '.mediadl-tmp'
    root.mkdir()
    old = root / 'old-job'
    old.mkdir()
    (old / 'part.bin').write_bytes(b'x' * 1024)
    fresh = root / 'fresh-job'
    fresh.mkdir()
    active = root / 'active-job'
    active.mkdir()

    stale = time.time() - 48 * 3600
    os.utime(old, (stale, stale))
    os.utime(active, (stale, stale))

    result = await sweep_temp(root, ttl_s=24 * 3600, active_job_ids=frozenset({'active-job'}))

    assert result.removed == ('old-job',)
    assert result.bytes_freed >= 1024
    assert not old.exists()
    assert fresh.exists(), 'a recent directory may still be an in-flight job'
    assert active.exists(), 'an active job must never be swept, whatever its age'


async def test_sweep_is_safe_on_a_missing_root(tmp_path):
    result = await sweep_temp(tmp_path / 'nope')
    assert result.removed == ()


def test_metrics_counters_and_histograms_record():
    metrics.reset()
    metrics.increment('mediadl_jobs_total', connector='direct_url', final_state='COMPLETED')
    metrics.increment('mediadl_jobs_total', connector='direct_url', final_state='COMPLETED')
    metrics.observe('mediadl_job_duration_seconds', 1.5, connector='direct_url', policy='source')

    snapshot = metrics.snapshot()
    assert snapshot['mediadl_jobs_total{connector=direct_url}{final_state=COMPLETED}'] == 2.0
    assert snapshot['mediadl_job_duration_seconds{connector=direct_url}{policy=source}_count'] == 1.0
    metrics.reset()
    assert metrics.snapshot() == {}
