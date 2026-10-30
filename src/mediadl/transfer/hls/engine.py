"""Native HLS transfer (spec section 7.7, Phase 7b).

Segments download with bounded concurrency, each written to ``seg-N.part``
and renamed on success, with completed indices persisted so an interrupted
run resumes at segment granularity. Assembly is a byte concatenation
followed by an FFmpeg stream copy; nothing is ever re-encoded here.

Every segment and key URI goes through the same URL policy as any other
request, so a playlist cannot steer the downloader at an internal address.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from mediadl.domain.models import HlsLocator, Representation
from mediadl.errors import (
    DrmError,
    InputError,
    IntegrityError,
    SizeLimitError,
    ToolMissingError,
)
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
from mediadl.transfer.hls import parser
from mediadl.transfer.retry import RetryBudget, transfer_error_class, with_retries

log = get_logger(__name__)

STATE_FILE = 'stream.state.json'


@dataclass(slots=True)
class SegmentPlan:
    playlist: parser.MediaPlaylist
    directory: Path
    label: str


class HlsTransfer:
    engine_id: ClassVar[str] = 'hls'

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
        return isinstance(rep.locator, HlsLocator)

    async def transfer(
        self, plan: TransferPlan, progress: ProgressReporter, cancel: CancelToken
    ) -> TransferResult:
        locator = plan.rep.locator
        assert isinstance(locator, HlsLocator)
        if self.tools is None:
            raise ToolMissingError('FFmpeg is required to assemble HLS streams')

        work = plan.part_path.parent / f'{plan.part_path.stem}.hls'
        work.mkdir(parents=True, exist_ok=True)

        media, audio = await self._resolve_playlists(locator, plan)
        video_dir = work / 'video'
        video_file = await self._download_and_assemble(
            SegmentPlan(media, video_dir, 'video'), plan, progress, cancel
        )

        output = plan.part_path
        if audio is not None:
            audio_file = await self._download_and_assemble(
                SegmentPlan(audio, work / 'audio', 'audio'), plan, progress, cancel
            )
            merged = work / 'merged.mp4'
            await ops.merge(self.tools, video_file, audio_file, merged)
            await asyncio.to_thread(os.replace, merged, output)
        else:
            remuxed = work / 'remuxed.mp4'
            await ops.remux(
                self.tools, video_file, remuxed,
                adts_to_asc=video_file.suffix == '.ts',
            )
            await asyncio.to_thread(os.replace, remuxed, output)

        size = output.stat().st_size
        digest = await _sha256(output)
        progress(plan.rep.rep_id, size, size, force=True)
        return TransferResult(
            path=output, bytes_written=size, sha256=digest,
            validators=Validators(None, None, size),
            resumed=False, attempts=1,
            url_fingerprint=url_fingerprint(locator.playlist_url),
            content_type='video/mp4', rep_id=plan.rep.rep_id,
        )

    # -- playlists ---------------------------------------------------------
    async def _fetch_text(self, url: str, plan: TransferPlan) -> str:
        response = await self.client.request(
            'GET', url, headers=dict(plan.headers), allowed_hosts=plan.allowed_hosts
        )
        if response.status_code >= 400:
            raise InputError(f'playlist request failed with HTTP {response.status_code}')
        content = response.content
        if len(content) > plan.limits.max_playlist_bytes:
            raise SizeLimitError(
                'playlist is larger than the allowed size',
                size=len(content), limit=plan.limits.max_playlist_bytes,
            )
        return content.decode('utf-8', 'replace')

    async def _resolve_playlists(
        self, locator: HlsLocator, plan: TransferPlan
    ) -> tuple[parser.MediaPlaylist, parser.MediaPlaylist | None]:
        text = await self._fetch_text(locator.playlist_url, plan)
        if not parser.is_master(text):
            return parser.parse_media(text, locator.playlist_url), None

        master = parser.parse_master(text, locator.playlist_url)
        variant = self._pick_variant(master, locator)
        media_text = await self._fetch_text(variant.uri, plan)
        media = parser.parse_media(media_text, variant.uri)

        audio: parser.MediaPlaylist | None = None
        audio_uri = locator.audio_rendition_uri
        if audio_uri is None:
            rendition = master.audio_for(variant)
            audio_uri = rendition.uri if rendition else None
        if audio_uri:
            audio = parser.parse_media(await self._fetch_text(audio_uri, plan), audio_uri)
        return media, audio

    @staticmethod
    def _pick_variant(master: parser.MasterPlaylist, locator: HlsLocator) -> parser.Variant:
        if locator.variant_uri:
            for variant in master.variants:
                if variant.uri == locator.variant_uri or variant.uri.endswith(locator.variant_uri):
                    return variant
        return max(
            master.variants,
            key=lambda v: (
                (v.resolution or (0, 0))[0] * (v.resolution or (0, 0))[1],
                v.average_bandwidth or v.bandwidth or 0,
                v.uri,
            ),
        )

    # -- segments ----------------------------------------------------------
    async def _download_and_assemble(
        self, segment_plan: SegmentPlan, plan: TransferPlan,
        progress: ProgressReporter, cancel: CancelToken,
    ) -> Path:
        playlist = segment_plan.playlist
        directory = segment_plan.directory
        directory.mkdir(parents=True, exist_ok=True)
        state_path = directory / STATE_FILE
        done = _load_state(state_path)

        keys: dict[str, bytes] = {}
        semaphore = asyncio.Semaphore(self.concurrency)
        total_bytes = 0
        lock = asyncio.Lock()

        async def fetch(segment: parser.Segment) -> None:
            nonlocal total_bytes
            target = directory / f'seg-{segment.index:06d}'
            if segment.index in done and target.exists():
                async with lock:
                    total_bytes += target.stat().st_size
                return
            cancel.raise_if_cancelled()
            async with semaphore:
                # Retry per segment: one flaky segment must not throw away
                # the whole stream's progress.
                data = await with_retries(
                    lambda: self._fetch_segment(segment, plan),
                    classify=transfer_error_class,
                    budget=self.retry_budget,
                    label=f'segment:{segment.index}',
                )
                if segment.key and segment.key.encrypted:
                    data = await self._decrypt(data, segment, keys, plan)
                temp = target.with_suffix('.part')
                await asyncio.to_thread(temp.write_bytes, data)
                await asyncio.to_thread(os.replace, temp, target)
            async with lock:
                total_bytes += len(data)
                done.add(segment.index)
                _save_state(state_path, done)
                if total_bytes > plan.limits.max_bytes:
                    raise SizeLimitError(
                        'stream exceeded the size limit',
                        limit=plan.limits.max_bytes, written=total_bytes,
                    )
                progress(plan.rep.rep_id, total_bytes, None)

        try:
            async with asyncio.TaskGroup() as group:
                for segment in playlist.segments:
                    group.create_task(fetch(segment))
        except BaseException as exc:
            # Whatever finished stays on disk and in the state file, so the
            # next attempt resumes instead of starting over.
            _save_state(state_path, done)
            raise _unwrap(exc) from None

        return await self._assemble(segment_plan, plan)

    async def _fetch_segment(self, segment: parser.Segment, plan: TransferPlan) -> bytes:
        headers = dict(plan.headers)
        if segment.byte_range:
            offset, length = segment.byte_range
            headers['Range'] = f'bytes={offset}-{offset + length - 1}'
        response = await self.client.request(
            'GET', segment.uri, headers=headers, allowed_hosts=plan.allowed_hosts
        )
        if response.status_code >= 400:
            raise IntegrityError(
                f'segment {segment.index} failed with HTTP {response.status_code}',
                index=segment.index,
            )
        return response.content

    async def _decrypt(
        self, data: bytes, segment: parser.Segment, keys: dict[str, bytes], plan: TransferPlan
    ) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key_info = segment.key
        assert key_info is not None
        key_info.check_supported()
        if not key_info.uri:
            raise DrmError('encrypted segment without a key URI')
        if key_info.uri not in keys:
            response = await self.client.request(
                'GET', key_info.uri, headers=dict(plan.headers), allowed_hosts=plan.allowed_hosts
            )
            if response.status_code >= 400 or len(response.content) != 16:
                raise DrmError('AES-128 key could not be retrieved')
            keys[key_info.uri] = response.content
        iv = key_info.iv or parser.default_iv(segment.media_sequence)
        decryptor = Cipher(algorithms.AES(keys[key_info.uri]), modes.CBC(iv)).decryptor()
        plaintext = decryptor.update(data) + decryptor.finalize()
        # Strip PKCS#7 padding, which HLS applies to the whole segment.
        if plaintext:
            pad = plaintext[-1]
            if 1 <= pad <= 16 and plaintext[-pad:] == bytes([pad]) * pad:
                plaintext = plaintext[:-pad]
        return plaintext

    async def _assemble(self, segment_plan: SegmentPlan, plan: TransferPlan) -> Path:
        playlist = segment_plan.playlist
        directory = segment_plan.directory
        first = playlist.segments[0]
        suffix = '.ts' if first.uri.split('?')[0].endswith('.ts') else '.mp4'
        output = directory / f'assembled{suffix}'

        init_cache: dict[str, bytes] = {}
        for segment in playlist.segments:
            if segment.init_uri and segment.init_uri not in init_cache:
                response = await self.client.request(
                    'GET', segment.init_uri, headers=dict(plan.headers),
                    allowed_hosts=plan.allowed_hosts,
                )
                init_cache[segment.init_uri] = response.content

        def _write() -> None:
            written_init: str | None = None
            with open(output, 'wb') as out:
                for segment in playlist.segments:
                    # An EXT-X-MAP init goes in once per discontinuity.
                    if segment.init_uri and (
                        segment.init_uri != written_init or segment.discontinuity
                    ):
                        out.write(init_cache[segment.init_uri])
                        written_init = segment.init_uri
                    part = directory / f'seg-{segment.index:06d}'
                    if not part.exists():
                        raise IntegrityError(f'segment {segment.index} is missing')
                    out.write(part.read_bytes())
                out.flush()
                os.fsync(out.fileno())

        await asyncio.to_thread(_write)
        return output


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
