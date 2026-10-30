"""Contract checks applied to every connector's output (spec section 5.3).

A violation is a connector bug, never a user error, so it raises
``ConnectorContractError`` and is not retryable.
"""
from __future__ import annotations

from mediadl.domain.models import (
    Asset,
    Availability,
    Collection,
    CollectionKind,
    MediaKind,
    Representation,
    RepresentationRole,
    ResolvedItem,
    SourceRef,
)
from mediadl.errors import ConnectorContractError


def validate_representation(rep: Representation, *, asset_id: str) -> None:
    if not rep.rep_id:
        raise ConnectorContractError('representation has an empty rep_id', asset_id=asset_id)
    if not isinstance(rep.kind, MediaKind):
        raise ConnectorContractError('representation kind is not a MediaKind', asset_id=asset_id)
    if not isinstance(rep.role, RepresentationRole):
        raise ConnectorContractError('representation role is not a RepresentationRole', asset_id=asset_id)
    if rep.protocol not in ('https', 'hls', 'dash', 'file'):
        raise ConnectorContractError(f'unknown protocol {rep.protocol!r}', asset_id=asset_id)


def validate_asset(asset: Asset) -> Asset:
    if not asset.asset_id:
        raise ConnectorContractError('asset has an empty asset_id')
    if not isinstance(asset.kind, MediaKind):
        raise ConnectorContractError('asset kind is not a MediaKind', asset_id=asset.asset_id)
    if asset.position < 1:
        raise ConnectorContractError('asset position must be 1-based', asset_id=asset.asset_id)
    if not isinstance(asset.ref, SourceRef) or not asset.ref.media_id:
        raise ConnectorContractError('asset.ref must carry a provider media_id', asset_id=asset.asset_id)
    rep_ids = [r.rep_id for r in asset.representations]
    if len(rep_ids) != len(set(rep_ids)):
        raise ConnectorContractError('duplicate rep_id within an asset', asset_id=asset.asset_id)
    for rep in asset.representations:
        validate_representation(rep, asset_id=asset.asset_id)
    usable_roles = (RepresentationRole.PRIMARY, RepresentationRole.COMPONENT)
    if asset.availability is Availability.AVAILABLE and not any(
        r.role in usable_roles for r in asset.representations
    ):
        raise ConnectorContractError(
            'an AVAILABLE asset needs at least one primary or component representation',
            asset_id=asset.asset_id,
        )
    if asset.availability is not Availability.AVAILABLE and not asset.unavailable_reason:
        raise ConnectorContractError(
            'an unavailable asset must explain why', asset_id=asset.asset_id
        )
    return asset


def validate_collection(collection: Collection) -> Collection:
    if not collection.collection_id:
        raise ConnectorContractError('collection has an empty collection_id')
    if not isinstance(collection.kind, CollectionKind):
        raise ConnectorContractError('collection kind is not a CollectionKind')
    if not collection.assets:
        raise ConnectorContractError('collection has no assets', collection_id=collection.collection_id)
    ids = [a.asset_id for a in collection.assets]
    if len(ids) != len(set(ids)):
        raise ConnectorContractError(
            'duplicate asset_id in collection', collection_id=collection.collection_id
        )
    positions = [a.position for a in collection.assets]
    if positions != list(range(1, len(positions) + 1)):
        raise ConnectorContractError(
            f'positions must be contiguous 1..n in order, got {positions}',
            collection_id=collection.collection_id,
        )
    for asset in collection.assets:
        validate_asset(asset)
    return collection


def validate(item: ResolvedItem) -> ResolvedItem:
    if isinstance(item, Collection):
        return validate_collection(item)
    if isinstance(item, Asset):
        return validate_asset(item)
    raise ConnectorContractError(f'connector returned {type(item).__name__}, not Asset or Collection')


def as_collection(item: ResolvedItem) -> Collection:
    """Normalize a single asset into a one-item collection so the pipeline is uniform."""
    if isinstance(item, Collection):
        return item
    return Collection(
        collection_id=item.asset_id,
        ref=item.ref,
        kind=(
            CollectionKind.UPLOAD_BATCH
            if item.ref.connector_id == 'user_files'
            else CollectionKind.PLAYLIST
        ),
        assets=(item,),
        position_basis='api_response_order',
        metadata=item.metadata,
    )
