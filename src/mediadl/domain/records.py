"""App-generated records: what *we* decided, measured, and wrote."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from mediadl.domain.models import Representation

RenditionBasis = Literal['platform_rendition', 'user_original', 'direct_source']


@dataclass(frozen=True, slots=True)
class SelectedRendition:
    asset_id: str
    primary: Representation
    paired_audio: Representation | None
    policy_name: str
    score: tuple[float, ...]
    reasons: tuple[str, ...]
    container: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Measured after download by ffprobe or an image decoder."""

    format_name: str
    duration_s: float | None = None
    streams: tuple[Mapping[str, object], ...] = ()
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    pix_fmt: str | None = None
    size_bytes: int | None = None
    tool_version: str = ''


@dataclass(frozen=True, slots=True)
class ProcessingStep:
    operation: Literal['none', 'merge', 'remux', 'transcode', 'image_convert', 'metadata_embed']
    stream_copy: bool
    tool: str = ''
    argv_redacted: tuple[str, ...] = ()
    input_sha256: tuple[str, ...] = ()
    output_sha256: str = ''
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class OutputRecord:
    asset_id: str
    position: int
    relative_path: str
    size_bytes: int
    sha256: str
    mime_type: str
    selected_rep_id: str
    downloaded_at: datetime
    rendition_basis: RenditionBasis
    probe: ProbeResult | None = None
    processing: tuple[ProcessingStep, ...] = ()
    retained_source_path: str | None = None


@dataclass(frozen=True, slots=True)
class ItemResult:
    """Per-asset outcome. Failure is a value, not an exception, above item level."""

    position: int
    asset_id: str
    status: Literal['completed', 'failed', 'unavailable', 'skipped']
    record: OutputRecord | None = None
    error_code: str | None = None
    error_message: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 'completed'
