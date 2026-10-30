"""Typed domain objects (spec section 5.4).

These replace yt-dlp's untyped info dict. Source-supplied metadata lives in
``SourceMetadata``; app-generated records live in ``mediadl.domain.records``.
Nothing here depends on a third-party package.
"""
from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal


class MediaKind(enum.StrEnum):
    IMAGE = 'image'
    VIDEO = 'video'
    AUDIO = 'audio'


class CollectionKind(enum.StrEnum):
    CAROUSEL = 'carousel'
    PLAYLIST = 'playlist'
    UPLOAD_BATCH = 'upload_batch'


class Availability(enum.StrEnum):
    AVAILABLE = 'available'
    UNAVAILABLE_COPYRIGHT = 'unavailable_copyright'
    UNAVAILABLE_DRM = 'unavailable_drm'
    NOT_FOUND = 'not_found'
    NOT_ACCESSIBLE = 'not_accessible'
    UNSUPPORTED_TYPE = 'unsupported_type'


class RepresentationRole(enum.StrEnum):
    PRIMARY = 'primary'
    PREVIEW = 'preview'
    COMPONENT = 'component'


@dataclass(frozen=True, slots=True)
class SourceRef:
    connector_id: str
    media_id: str
    parent_id: str | None = None
    position: int | None = None
    canonical_url: str | None = None


# ---- Locators: how bytes are reached; the engine depends only on this type ----
@dataclass(frozen=True, slots=True)
class HttpLocator:
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None
    send_credentials: bool = False


@dataclass(frozen=True, slots=True)
class HlsLocator:
    playlist_url: str
    variant_uri: str | None = None
    audio_rendition_uri: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DashLocator:
    mpd_url: str
    representation_ids: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LocalFileLocator:
    relative_path: PurePosixPath


Locator = HttpLocator | HlsLocator | DashLocator | LocalFileLocator


@dataclass(frozen=True, slots=True)
class TechnicalInfo:
    """Properties *declared by the source*; None = unknown.

    Measured values live in ``ProbeResult`` and are never written back here.
    """

    mime_type: str | None = None
    container: str | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    bitrate_kbps: float | None = None
    audio_channels: int | None = None
    sample_rate_hz: int | None = None
    dynamic_range: Literal['SDR', 'HDR10', 'HLG', 'DV'] | None = None
    bit_depth: int | None = None
    filesize_bytes: int | None = None
    filesize_is_estimate: bool = False
    has_video: bool | None = None
    has_audio: bool | None = None
    is_live: bool = False


@dataclass(frozen=True, slots=True)
class Representation:
    rep_id: str
    kind: MediaKind
    role: RepresentationRole
    protocol: Literal['https', 'hls', 'dash', 'file']
    locator: Locator
    declared: TechnicalInfo = field(default_factory=TechnicalInfo)
    drm: bool = False
    source_preference: int = 0
    label: str = ''


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Verbatim provider fields. Never mixed with app-generated data."""

    fields: Mapping[str, object] = field(default_factory=dict)
    fetched_at: datetime | None = None
    api_version: str | None = None


@dataclass(frozen=True, slots=True)
class Asset:
    asset_id: str
    ref: SourceRef
    kind: MediaKind
    position: int
    availability: Availability
    representations: tuple[Representation, ...]
    metadata: SourceMetadata = field(default_factory=SourceMetadata)
    surface: str | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Collection:
    collection_id: str
    ref: SourceRef
    kind: CollectionKind
    assets: tuple[Asset, ...]
    position_basis: Literal['api_response_order', 'playlist_index', 'user_order']
    metadata: SourceMetadata = field(default_factory=SourceMetadata)


ResolvedItem = Asset | Collection
