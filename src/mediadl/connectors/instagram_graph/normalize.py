"""IG Media node -> domain objects (spec section 4.5.6).

The rules that matter:
  * ``CAROUSEL_ALBUM`` becomes a Collection whose children keep the API's
    response order, recorded as ``position_basis='api_response_order'``
    because Meta does not document that order [OPEN];
  * a missing ``media_url`` means the provider withheld the media (copyright
    or a flag), which is per-asset unavailability, not a job failure;
  * ``thumbnail_url`` is a PREVIEW and is never a stand-in for the video;
  * outputs are labelled ``platform_rendition``: Instagram serves transcodes
    and exposes no source resolution, bitrate, or codec fields.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from mediadl.domain.models import (
    Asset,
    Availability,
    Collection,
    CollectionKind,
    HttpLocator,
    MediaKind,
    Representation,
    RepresentationRole,
    ResolvedItem,
    SourceMetadata,
    SourceRef,
    TechnicalInfo,
)

CONNECTOR_ID = 'instagram_graph'

#: Fields copied verbatim into the sidecar's ``source`` section.
SOURCE_FIELDS = (
    'id', 'media_type', 'media_product_type', 'permalink', 'shortcode', 'timestamp',
    'caption', 'username', 'alt_text', 'is_shared_to_feed', 'media_audio_type',
    'like_count', 'comments_count', 'is_ai_generated', 'copyright_check_information',
)

KIND_BY_MEDIA_TYPE = {'IMAGE': MediaKind.IMAGE, 'VIDEO': MediaKind.VIDEO}


def pick(node: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: node[key] for key in keys if key in node}


def normalize_node(
    node: Mapping[str, Any], *, fetched_at: datetime, api_version: str
) -> ResolvedItem:
    metadata = SourceMetadata(
        fields=pick(node, SOURCE_FIELDS), fetched_at=fetched_at, api_version=api_version
    )
    if node.get('media_type') == 'CAROUSEL_ALBUM':
        children = (node.get('children') or {}).get('data', []) or []
        assets = tuple(
            normalize_child(
                child, position=index, parent_id=str(node.get('id', '')),
                fetched_at=fetched_at, api_version=api_version,
            )
            for index, child in enumerate(children, start=1)
        )
        return Collection(
            collection_id=f'{CONNECTOR_ID}:{node["id"]}',
            ref=SourceRef(
                CONNECTOR_ID, str(node['id']), canonical_url=node.get('permalink')
            ),
            kind=CollectionKind.CAROUSEL,
            assets=assets,
            position_basis='api_response_order',
            metadata=metadata,
        )
    return normalize_child(
        node, position=1, parent_id=None, fetched_at=fetched_at,
        api_version=api_version, metadata=metadata,
    )


def normalize_child(
    node: Mapping[str, Any],
    *,
    position: int,
    parent_id: str | None,
    fetched_at: datetime,
    api_version: str,
    metadata: SourceMetadata | None = None,
) -> Asset:
    media_id = str(node.get('id', ''))
    asset_id = f'{CONNECTOR_ID}:{media_id}'
    meta = metadata or SourceMetadata(
        fields=pick(node, SOURCE_FIELDS), fetched_at=fetched_at, api_version=api_version
    )
    ref = SourceRef(
        CONNECTOR_ID, media_id, parent_id=parent_id, position=position,
        canonical_url=node.get('permalink'),
    )
    kind = KIND_BY_MEDIA_TYPE.get(str(node.get('media_type')))
    if kind is None:
        return Asset(
            asset_id=asset_id, ref=ref, kind=MediaKind.IMAGE, position=position,
            availability=Availability.UNSUPPORTED_TYPE, representations=(),
            metadata=meta, surface=node.get('media_product_type'),
            unavailable_reason=f'unsupported media_type {node.get("media_type")!r}',
        )

    representations: list[Representation] = []
    media_url = node.get('media_url')
    if media_url:
        representations.append(
            Representation(
                rep_id='primary',
                kind=kind,
                role=RepresentationRole.PRIMARY,
                protocol='https',
                # Instagram's CDN URLs are signed; treat them as short-lived.
                locator=HttpLocator(url=str(media_url), send_credentials=False),
                declared=TechnicalInfo(),
                label='primary',
            )
        )
    thumbnail = node.get('thumbnail_url')
    if kind is MediaKind.VIDEO and thumbnail:
        representations.append(
            Representation(
                rep_id='thumbnail',
                kind=MediaKind.IMAGE,
                role=RepresentationRole.PREVIEW,
                protocol='https',
                locator=HttpLocator(url=str(thumbnail), send_credentials=False),
                label='cover',
            )
        )

    available = bool(media_url)
    return Asset(
        asset_id=asset_id,
        ref=ref,
        kind=kind,
        position=position,
        availability=Availability.AVAILABLE if available else Availability.UNAVAILABLE_COPYRIGHT,
        representations=tuple(representations),
        metadata=meta,
        surface=node.get('media_product_type'),
        unavailable_reason=(
            None if available
            else 'media_url omitted by the provider (copyright or flagged content)'
        ),
    )
