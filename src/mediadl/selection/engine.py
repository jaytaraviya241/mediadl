"""Filter -> enumerate -> rank -> explain (spec sections 7.1, 12.5).

Deterministic by construction: ties break on source_preference then rep_id,
so the same input always yields the same choice. Every exclusion is recorded
with a reason code and surfaced in the sidecar.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mediadl.domain.models import (
    Asset,
    Availability,
    MediaKind,
    Representation,
    RepresentationRole,
)
from mediadl.domain.records import SelectedRendition
from mediadl.errors import DrmError, NoAcceptableRepresentationError
from mediadl.selection.containers import container_for_single, pair_container
from mediadl.selection.policies import Option, SelectionPolicy, sort_key

#: Protocols with an engine wired up. HLS/DASH arrive in phase 7.
DEFAULT_ENABLED_PROTOCOLS = frozenset({'https', 'file'})


@dataclass(frozen=True, slots=True)
class Exclusion:
    rep_id: str
    reason: str
    detail: str = ''


def hard_filter(
    rep: Representation,
    policy: SelectionPolicy,
    *,
    enabled_protocols: frozenset[str],
) -> Exclusion | None:
    if rep.drm:
        return Exclusion(rep.rep_id, 'EXCLUDED_DRM', 'representation is DRM protected')
    if rep.declared.is_live:
        return Exclusion(rep.rep_id, 'EXCLUDED_LIVE', 'live streams are out of scope')
    if rep.protocol not in enabled_protocols:
        return Exclusion(rep.rep_id, 'EXCLUDED_PROTOCOL', f'no engine enabled for {rep.protocol}')
    size = rep.declared.filesize_bytes
    if policy.max_bytes is not None and size is not None and size > policy.max_bytes:
        return Exclusion(rep.rep_id, 'EXCLUDED_SIZE', f'{size} > {policy.max_bytes}')
    height = rep.declared.height
    if policy.max_height is not None and height is not None and height > policy.max_height:
        # Only a filter for compatibility mode; other policies pass max_height=None.
        return Exclusion(rep.rep_id, 'EXCLUDED_POLICY', f'height {height} > {policy.max_height}')
    return None


def is_complete(rep: Representation, kind: MediaKind) -> bool:
    """True when the representation alone yields the whole asset."""
    if rep.role is RepresentationRole.COMPONENT:
        return False
    declared = rep.declared
    if kind is MediaKind.VIDEO:
        if declared.has_video is False:
            return False
        # Unknown audio is treated as present: a single progressive file is
        # the normal case and must not be excluded for lack of metadata.
        return declared.has_audio is not False or rep.kind is MediaKind.VIDEO
    if kind is MediaKind.AUDIO:
        return declared.has_video is not True
    return rep.kind is MediaKind.IMAGE or declared.has_video is not True


def _video_components(candidates: Sequence[Representation]) -> list[Representation]:
    return [
        r for r in candidates
        if r.declared.has_video is True and r.declared.has_audio is False
    ]


def _audio_components(candidates: Sequence[Representation]) -> list[Representation]:
    return [
        r for r in candidates
        if r.kind is MediaKind.AUDIO or (r.declared.has_audio is True and r.declared.has_video is False)
    ]


def enumerate_options(
    asset: Asset, candidates: Sequence[Representation]
) -> list[Option]:
    options = [
        Option(primary=rep, container=container_for_single(rep))
        for rep in candidates
        if is_complete(rep, asset.kind)
    ]
    if asset.kind is MediaKind.VIDEO:
        for video in _video_components(candidates):
            for audio in _audio_components(candidates):
                container = pair_container(video, audio)
                if container is not None:
                    options.append(Option(primary=video, audio=audio, container=container))
    return options


def explain(
    best: Option, options: Sequence[Option], excluded: Sequence[Exclusion]
) -> tuple[str, ...]:
    rep = best.primary
    label = rep.label or _describe(rep)
    paired = ''
    if best.audio is not None:
        paired = f' + {best.audio.label or _describe(best.audio)} (pair)'
    reasons = [f'picked {label}{paired}']
    if best.container:
        reasons.append(f'container {best.container}')
    if len(options) > 1:
        reasons.append(f'ranked first of {len(options)} playable options')
    else:
        reasons.append('single playable option')
    by_reason: dict[str, int] = {}
    for item in excluded:
        by_reason[item.reason] = by_reason.get(item.reason, 0) + 1
    for reason, count in sorted(by_reason.items()):
        reasons.append(f'excluded {count} ({reason})')
    return tuple(reasons)


def _describe(rep: Representation) -> str:
    d = rep.declared
    parts: list[str] = []
    if d.width and d.height:
        parts.append(f'{d.width}x{d.height}')
    if d.video_codec:
        parts.append(d.video_codec)
    if d.audio_codec and d.audio_codec != 'none':
        parts.append(d.audio_codec)
    return ' '.join(parts) or rep.rep_id


def choose(
    asset: Asset,
    policy: SelectionPolicy | None = None,
    *,
    enabled_protocols: frozenset[str] = DEFAULT_ENABLED_PROTOCOLS,
    preferred_rep_id: str | None = None,
) -> SelectedRendition:
    """Pick the rendition to download, or explain why nothing qualifies."""
    policy = policy or SelectionPolicy()
    if asset.availability is Availability.UNAVAILABLE_DRM:
        raise DrmError('asset is DRM protected', asset_id=asset.asset_id)
    if asset.availability is not Availability.AVAILABLE:
        raise NoAcceptableRepresentationError(
            f'asset is {asset.availability}',
            asset_id=asset.asset_id, availability=str(asset.availability),
        )

    excluded: list[Exclusion] = []
    candidates: list[Representation] = []
    for rep in asset.representations:
        if rep.role is RepresentationRole.PREVIEW:
            continue  # a preview is never a substitute for the asset
        reason = hard_filter(rep, policy, enabled_protocols=enabled_protocols)
        if reason is not None:
            excluded.append(reason)
        else:
            candidates.append(rep)

    options = enumerate_options(asset, candidates)
    if not options:
        if excluded and all(e.reason == 'EXCLUDED_DRM' for e in excluded):
            raise DrmError('every representation is DRM protected', asset_id=asset.asset_id)
        raise NoAcceptableRepresentationError(
            'no acceptable representation',
            asset_id=asset.asset_id,
            excluded=[(e.rep_id, e.reason) for e in excluded],
        )

    if preferred_rep_id is not None:
        preferred_options = [
            option for option in options if option.primary.rep_id == preferred_rep_id
        ]
        if not preferred_options:
            raise NoAcceptableRepresentationError(
                'selected format is no longer available',
                asset_id=asset.asset_id,
                format_id=preferred_rep_id,
            )
        options = preferred_options

    key = sort_key(policy, asset.kind)
    best = max(
        options,
        key=lambda o: (key(o), float(o.primary.source_preference), o.primary.rep_id),
    )
    return SelectedRendition(
        asset_id=asset.asset_id,
        primary=best.primary,
        paired_audio=best.audio,
        policy_name=policy.name,
        score=tuple(float(x) for x in key(best)),
        reasons=explain(best, options, excluded),
        container=best.container,
    )
