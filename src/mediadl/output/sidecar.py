"""Per-asset sidecar JSON and the collection manifest (spec section 7.9).

The schema keeps two top-level sections that are never mixed: ``source``
holds verbatim provider fields, ``app`` holds what this program decided,
measured, and wrote. Metadata is written beside the media rather than into
it, because embedding changes the file's bytes even with a stream copy.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from mediadl.domain.models import Asset, Collection
from mediadl.domain.records import ItemResult, OutputRecord, SelectedRendition
from mediadl.net.redact import redact_mapping, url_fingerprint

ASSET_SCHEMA = 'mediadl.asset/1'
COLLECTION_SCHEMA = 'mediadl.collection/1'


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default)


def build_asset_sidecar(
    asset: Asset,
    collection: Collection,
    rendition: SelectedRendition,
    record: OutputRecord,
    *,
    transfer_host: str | None = None,
    resumed: bool = False,
    attempts: int = 1,
) -> dict[str, Any]:
    parent: dict[str, Any] | None = None
    if collection.collection_id != asset.asset_id:
        parent = {
            'id': collection.ref.media_id,
            'kind': str(collection.kind),
            'permalink': collection.ref.canonical_url,
            **dict(collection.metadata.fields or {}),
        }

    return {
        'schema': ASSET_SCHEMA,
        'source': {
            'connector_id': asset.ref.connector_id,
            'api_version': asset.metadata.api_version,
            'fetched_at': asset.metadata.fetched_at,
            'fields': dict(asset.metadata.fields or {}),
            'parent': redact_mapping(parent) if parent else None,
        },
        'app': {
            'asset_id': asset.asset_id,
            'position': asset.position,
            'position_basis': collection.position_basis,
            'collection_count': len(collection.assets),
            'kind': str(asset.kind),
            'surface': asset.surface,
            'rendition_basis': record.rendition_basis,
            'selected_representation': {
                'rep_id': rendition.primary.rep_id,
                'protocol': rendition.primary.protocol,
                'label': rendition.primary.label,
                'paired_audio_rep_id': rendition.paired_audio.rep_id if rendition.paired_audio else None,
                'container': rendition.container,
            },
            'available_representations': [
                {'rep_id': r.rep_id, 'role': str(r.role), 'kind': str(r.kind), 'drm': r.drm}
                for r in asset.representations
            ],
            'selection_policy': rendition.policy_name,
            'selection_reasons': list(rendition.reasons),
            'output': {
                'relative_path': record.relative_path,
                'size_bytes': record.size_bytes,
                'sha256': record.sha256,
                'mime_type': record.mime_type,
                'retained_source_path': record.retained_source_path,
            },
            'probe': asdict(record.probe) if record.probe else None,
            'processing': [asdict(step) for step in record.processing],
            'downloaded_at': record.downloaded_at,
            'transfer': {
                # Never the full URL: media URLs are signed credentials.
                'url_host': transfer_host,
                'resumed': resumed,
                'attempts': attempts,
            },
        },
    }


def write_asset_sidecar(media_path: Path, payload: dict[str, Any]) -> Path:
    path = media_path.with_name(media_path.name + '.meta.json')
    tmp = path.with_suffix('.tmp')
    tmp.write_text(dumps(payload), encoding='utf-8')
    tmp.replace(path)
    return path


def build_collection_manifest(
    collection: Collection,
    results: list[ItemResult],
    *,
    job_id: str,
    policy_name: str,
    final_state: str,
) -> dict[str, Any]:
    items = []
    for result in sorted(results, key=lambda r: r.position):
        entry: dict[str, Any] = {
            'position': result.position,
            'asset_id': result.asset_id,
            'status': result.status,
        }
        if result.record is not None:
            entry['relative_path'] = result.record.relative_path
            entry['sha256'] = result.record.sha256
            entry['size_bytes'] = result.record.size_bytes
            entry['sidecar'] = result.record.relative_path + '.meta.json'
        if result.error_code:
            entry['error_code'] = result.error_code
            entry['error_message'] = result.error_message
        items.append(entry)

    counts = {
        status: sum(1 for r in results if r.status == status)
        for status in ('completed', 'failed', 'unavailable', 'skipped')
    }
    return {
        'schema': COLLECTION_SCHEMA,
        'collection_id': collection.collection_id,
        'kind': str(collection.kind),
        'position_basis': collection.position_basis,
        'source': {
            'connector_id': collection.ref.connector_id,
            'media_id': collection.ref.media_id,
            'permalink': collection.ref.canonical_url,
            'fields': dict(collection.metadata.fields or {}),
        },
        'job': {'job_id': job_id, 'policy': policy_name, 'final_state': final_state},
        'counts': counts,
        'items': items,
    }


def write_collection_manifest(directory: Path, payload: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'collection.manifest.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(dumps(payload), encoding='utf-8')
    tmp.replace(path)
    return path


def transfer_host_of(url: str) -> str:
    return url_fingerprint(url).split('/')[0]
