"""Asset and probe admission checks (spec sections 5.3, 9.3)."""
from __future__ import annotations

from dataclasses import dataclass

from mediadl.config import LimitsConfig
from mediadl.domain.models import Asset, Availability, MediaKind
from mediadl.domain.records import ProbeResult
from mediadl.errors import (
    ContentUnavailableError,
    DrmError,
    MaliciousInputError,
    NotFoundError,
    PolicyDeniedError,
    SizeLimitError,
)


@dataclass(frozen=True, slots=True)
class AssetPolicy:
    limits: LimitsConfig
    allowed_kinds: frozenset[MediaKind] = frozenset(MediaKind)

    def check_asset(self, asset: Asset) -> None:
        if asset.kind not in self.allowed_kinds:
            raise PolicyDeniedError(
                f'{asset.kind} downloads are disabled', asset_id=asset.asset_id, kind=str(asset.kind)
            )
        match asset.availability:
            case Availability.AVAILABLE:
                pass
            case Availability.UNAVAILABLE_DRM:
                raise DrmError('asset is DRM protected', asset_id=asset.asset_id)
            case Availability.NOT_FOUND:
                raise NotFoundError('asset no longer exists', asset_id=asset.asset_id)
            case _:
                raise ContentUnavailableError(
                    asset.unavailable_reason or 'asset is unavailable',
                    asset_id=asset.asset_id,
                    availability=str(asset.availability),
                )
        for rep in asset.representations:
            size = rep.declared.filesize_bytes
            if size is not None and size > self.limits.max_asset_bytes:
                raise SizeLimitError(
                    'declared asset size exceeds the limit',
                    asset_id=asset.asset_id, size=size, limit=self.limits.max_asset_bytes,
                )
            duration = rep.declared.duration_s
            if duration is not None and duration > self.limits.max_duration_s:
                raise PolicyDeniedError(
                    'media is longer than the configured limit',
                    asset_id=asset.asset_id, duration_s=duration,
                )

    def check_collection_size(self, count: int) -> None:
        if count > self.limits.max_assets_per_collection:
            raise PolicyDeniedError(
                'collection has more assets than the configured limit',
                count=count, limit=self.limits.max_assets_per_collection,
            )

    def check_probe(self, probe: ProbeResult, kind: MediaKind) -> None:
        """Post-download limits, applied to *measured* properties."""
        if probe.duration_s is not None and probe.duration_s > self.limits.max_duration_s:
            raise PolicyDeniedError(
                'media is longer than the configured limit', duration_s=probe.duration_s
            )
        if probe.width and probe.height:
            pixels = probe.width * probe.height
            cap = self.limits.max_image_pixels if kind is MediaKind.IMAGE else self.limits.max_video_pixels
            if pixels > cap:
                raise MaliciousInputError(
                    'media exceeds the pixel limit', pixels=pixels, limit=cap,
                )
