"""Per-asset processing pipeline (spec section 12.7).

Order: verify the bytes, probe, merge or remux with a stream copy, then
apply the policy's transcode if any. The unmodified source is kept whenever
``keep_source`` is on, so every transformation stays reversible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mediadl.domain.models import MediaKind
from mediadl.domain.records import ProbeResult, ProcessingStep, SelectedRendition
from mediadl.errors import ContentTypeMismatchError, ProcessingError, ToolMissingError
from mediadl.obs.logging import get_logger
from mediadl.policy.asset_policy import AssetPolicy
from mediadl.processing import operations as ops
from mediadl.processing import sniff
from mediadl.processing.ffmpeg_runner import FfmpegTools
from mediadl.processing.policies import ProcessingPolicy, keeps_smaller_output, plan
from mediadl.processing.probe import check_image_bomb
from mediadl.processing.probe import probe as run_probe
from mediadl.transfer.manager import TransferredFiles

log = get_logger(__name__)


@dataclass(slots=True)
class ProcessedFiles:
    path: Path
    kind: MediaKind
    probe: ProbeResult | None = None
    steps: list[ProcessingStep] = field(default_factory=list)
    retained_source: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def extension(self) -> str:
        detected = sniff.detect(self.path)
        return detected.ext or self.path.suffix.lstrip('.') or 'bin'

    @property
    def mime_type(self) -> str:
        return sniff.detect(self.path).mime or 'application/octet-stream'


class MediaProcessor:
    def __init__(
        self,
        tools: FfmpegTools | None,
        asset_policy: AssetPolicy,
        *,
        timeout_factor: float = 3.0,
        timeout_floor_s: float = 60.0,
    ) -> None:
        self.tools = tools
        self.asset_policy = asset_policy
        self.timeout_factor = timeout_factor
        self.timeout_floor_s = timeout_floor_s

    def _timeout(self, probe: ProbeResult | None) -> float:
        duration = (probe.duration_s if probe else None) or 0.0
        return max(self.timeout_floor_s, duration * self.timeout_factor + self.timeout_floor_s)

    def _require_tools(self, what: str) -> FfmpegTools:
        if self.tools is None:
            raise ToolMissingError(f'FFmpeg is required to {what}', operation=what)
        return self.tools

    async def apply(
        self,
        rendition: SelectedRendition,
        files: TransferredFiles,
        policy: ProcessingPolicy,
        kind: MediaKind,
        *,
        work_dir: Path,
    ) -> ProcessedFiles:
        current = files.primary.path
        detected = sniff.detect(current)
        if not sniff.compatible(detected, kind):
            raise ContentTypeMismatchError(
                f'downloaded bytes are {detected.kind}, expected {kind}',
                rep_id=rendition.primary.rep_id,
            )
        if kind is MediaKind.IMAGE:
            check_image_bomb(current, max_pixels=self.asset_policy.limits.max_image_pixels)

        probe_result: ProbeResult | None = None
        if self.tools is not None:
            try:
                probe_result = await run_probe(self.tools, current)
                self.asset_policy.check_probe(probe_result, kind)
            except ProcessingError as exc:
                if kind is MediaKind.IMAGE:
                    log.debug('image_probe_failed', error=str(exc))
                else:
                    raise

        result = ProcessedFiles(path=current, kind=kind, probe=probe_result)

        # 1. Assemble separate audio/video with a stream copy.
        if files.audio is not None:
            tools = self._require_tools('merge separate audio and video')
            container = rendition.container or 'mkv'
            merged = work_dir / f'merged.{container}'
            step = await ops.merge(
                tools, files.primary.path, files.audio.path, merged,
                timeout_s=self._timeout(probe_result),
            )
            result.steps.append(step)
            result.path = merged
            result.probe = await run_probe(tools, merged)

        # 2. Policy-driven transcode, if any.
        decision = plan(policy, result.probe, kind) if result.probe else 'keep'
        if decision == 'keep':
            result.notes.append('kept source bytes')
            return result

        tools = self._require_tools('apply the selected output policy')
        candidate = work_dir / f'processed.{decision.container}'
        try:
            if kind is MediaKind.IMAGE:
                step = await ops.convert_image(tools, result.path, candidate)
            elif decision.video_codec is None and decision.audio_codec is None:
                step = await ops.remux(tools, result.path, candidate)
            else:
                step = await ops.transcode(
                    tools, result.path, candidate, decision, timeout_s=self._timeout(result.probe)
                )
        except ProcessingError as exc:
            # A processing failure must not lose a good download.
            log.warning('processing_failed_keeping_source', error=str(exc))
            result.notes.append(f'processing failed, kept source: {exc.code}')
            return result

        if keeps_smaller_output(policy) and candidate.stat().st_size >= result.path.stat().st_size:
            candidate.unlink(missing_ok=True)
            result.notes.append('efficient encode was not smaller; kept source')
            return result

        new_probe = await run_probe(tools, candidate)
        result.steps.append(step)
        result.retained_source = result.path if policy.keep_source else None
        if not policy.keep_source and result.path != files.primary.path:
            result.path.unlink(missing_ok=True)
        result.path = candidate
        result.probe = new_probe
        return result
