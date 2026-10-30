"""Native DASH transfer (spec section 7.7, Phase 7b).

Per track: fetch the init segment, then the media segments in manifest
order, byte-concatenate them (valid for fragmented MP4 of one
representation), then merge video and audio with an FFmpeg stream copy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import ClassVar

from mediadl.domain.models import DashLocator, Representation
from mediadl.errors import InputError, IntegrityError, SizeLimitError, ToolMissingError
from mediadl.net.guarded_client import GuardedHttpClient
from mediadl.net.redact import url_fingerprint
from mediadl.obs.logging import get_logger
from mediadl.processing import operations as ops
from mediadl.processing.ffmpeg_runner import FfmpegTools
from mediadl.transfer.base import (
    CancelToken,
    ProgressReporter,
    TransferPlan,
    TransferResult,
    Validators,
)
from mediadl.transfer.dash import mpd
from mediadl.transfer.retry import RetryBudget, transfer_error_class, with_retries

log = get_logger(__name__)

STATE_FILE = 'stream.state.json'


class DashTransfer:
    engine_id: ClassVar[str] = 'dash'

    def __init__(
        self,
        client: GuardedHttpClient,
        tools: FfmpegTools | None = None,
        *,
        concurrency: int = 4,
        retry_budget: RetryBudget | None = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self.concurrency = concurrency
        self.retry_budget = retry_budget or RetryBudget(max_attempts=3, base_s=0.2, cap_s=5.0)

    def supports(self, rep: Representation) -> bool:
        return isinstance(rep.locator, DashLocator)

    async def transfer(
        self, plan: TransferPlan, progress: ProgressReporter, cancel: CancelToken
    ) -> TransferResult:
        locator = plan.rep.locator
        assert isinstance(locator, DashLocator)
        if self.tools is None:
            raise ToolMissingError('FFmpeg is required to assemble DASH streams')

        work = plan.part_path.parent / f'{plan.part_path.stem}.dash'
        work.mkdir(parents=True, exist_ok=True)

        manifest = await self._fetch_manifest(locator, plan)
        video, audio = self._pick(manifest, locator)
        if video is None and audio is None:
            raise InputError('manifest has neither a video nor an audio representation')

        video_file = (
            await self._download_track(video, work / 'video', plan, progress, cancel)
            if video is not None else None
        )
        audio_file = (
            await self._download_track(audio, work / 'audio', plan, progress, cancel)
            if audio is not None else None
        )

        output = plan.part_path
        if video_file is not None and audio_file is not None:
            merged = work / 'merged.mp4'
            await ops.merge(self.tools, video_file, audio_file, merged)
            await asyncio.to_thread(os.replace, merged, output)
        else:
            single = video_file or audio_file
            assert single is not None
            remuxed = work / 'remuxed.mp4'
            await ops.remux(self.tools, single, remuxed)
            await asyncio.to_thread(os.replace, remuxed, output)

        size = output.stat().st_size
        digest = await _sha256(output)
        progress(plan.rep.rep_id, size, size, force=True)
        return TransferResult(
            path=output, bytes_written=size, sha256=digest,
            validators=Validators(None, None, size),
            resumed=False, attempts=1,
            url_fingerprint=url_fingerprint(locator.mpd_url),
            content_type='video/mp4', rep_id=plan.rep.rep_id,
        )

    async def _fetch_manifest(self, locator: DashLocator, plan: TransferPlan) -> mpd.DashManifest:
        response = await self.client.request(
            'GET', locator.mpd_url, headers=dict(plan.headers), allowed_hosts=plan.allowed_hosts
        )
        if response.status_code >= 400:
            raise InputError(f'manifest request failed with HTTP {response.status_code}')
        if len(response.content) > plan.limits.max_playlist_bytes:
            raise SizeLimitError(
                'manifest is larger than the allowed size', size=len(response.content)
            )
        return mpd.parse(response.content.decode('utf-8', 'replace'), locator.mpd_url)

    @staticmethod
    def _pick(
        manifest: mpd.DashManifest, locator: DashLocator
    ) -> tuple[mpd.DashRepresentation | None, mpd.DashRepresentation | None]:
        if locator.representation_ids:
            wanted = set(locator.representation_ids)
            chosen = [r for r in manifest.representations if r.rep_id in wanted]
            video = next((r for r in chosen if r.is_video), None)
            audio = next((r for r in chosen if r.is_audio), None)
            if video or audio:
                return video, audio
        return manifest.best_video(), manifest.best_audio()

    async def _download_track(
        self,
        representation: mpd.DashRepresentation,
        directory: Path,
        plan: TransferPlan,
        progress: ProgressReporter,
        cancel: CancelToken,
    ) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        state_path = directory / STATE_FILE
        done = _load_state(state_path)
        semaphore = asyncio.Semaphore(self.concurrency)
        lock = asyncio.Lock()
        total = 0

        async def fetch_one(url: str, target: Path, index: int) -> None:
            nonlocal total
            if index in done and target.exists():
                async with lock:
                    total += target.stat().st_size
                return
            cancel.raise_if_cancelled()
            async with semaphore:
                data = await with_retries(
                    lambda: self._fetch(url, plan),
                    classify=transfer_error_class,
                    budget=self.retry_budget,
                    label=f'dash-segment:{index}',
                )
                temp = target.with_suffix('.part')
                await asyncio.to_thread(temp.write_bytes, data)
                await asyncio.to_thread(os.replace, temp, target)
            async with lock:
                total += len(data)
                done.add(index)
                _save_state(state_path, done)
                if total > plan.limits.max_bytes:
                    raise SizeLimitError(
                        'stream exceeded the size limit', limit=plan.limits.max_bytes
                    )
                progress(plan.rep.rep_id, total, None)

        try:
            async with asyncio.TaskGroup() as group:
                if representation.init_url:
                    group.create_task(fetch_one(representation.init_url, directory / 'init', -1))
                for segment in representation.segments:
                    group.create_task(
                        fetch_one(
                            segment.url, directory / f'seg-{segment.index:06d}', segment.index
                        )
                    )
        except BaseException as exc:
            _save_state(state_path, done)
            raise _unwrap(exc) from None

        output = directory / 'assembled.mp4'

        def _concat() -> None:
            with open(output, 'wb') as out:
                init = directory / 'init'
                if representation.init_url and init.exists():
                    out.write(init.read_bytes())
                for segment in representation.segments:
                    part = directory / f'seg-{segment.index:06d}'
                    if not part.exists():
                        raise IntegrityError(f'segment {segment.index} is missing')
                    out.write(part.read_bytes())
                out.flush()
                os.fsync(out.fileno())

        await asyncio.to_thread(_concat)
        return output

    async def _fetch(self, url: str, plan: TransferPlan) -> bytes:
        response = await self.client.request(
            'GET', url, headers=dict(plan.headers), allowed_hosts=plan.allowed_hosts
        )
        if response.status_code >= 400:
            raise IntegrityError(f'segment failed with HTTP {response.status_code}')
        return response.content


def _unwrap(exc: BaseException) -> BaseException:
    """Flatten a TaskGroup's ExceptionGroup to its first real exception.

    Callers classify transfer failures by type; an ExceptionGroup would be
    read as an unexpected internal error and never retried.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _load_state(path: Path) -> set[int]:
    try:
        return set(json.loads(path.read_text('utf-8'))['completed'])
    except (OSError, ValueError, KeyError):
        return set()


def _save_state(path: Path, done: set[int]) -> None:
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'completed': sorted(done)}), encoding='utf-8')
    temp.replace(path)


async def _sha256(path: Path) -> str:
    def _hash() -> str:
        digest = hashlib.sha256()
        with open(path, 'rb') as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    return await asyncio.to_thread(_hash)
