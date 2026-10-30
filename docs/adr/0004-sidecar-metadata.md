# 4. Metadata goes beside the file, not inside it

Status: accepted (2026-09-21)

## Context

Descriptive metadata (title, source, timestamps) can be embedded into the
container with `ffmpeg -c copy -metadata ...`. The compressed streams survive
untouched, which makes embedding look free.

It is not free. Measured on FFmpeg 9.0.1: after a metadata-only stream copy the
per-stream packet MD5 is unchanged, but the file's SHA-256 changes. Both facts
are asserted in `tests/integration/test_ffmpeg_operations.py`.

## Decision

Metadata is written to a `*.meta.json` sidecar. Embedding exists behind
`output.embed_metadata`, off by default.

## Why

- Under a source-preserving default, "the bytes you received" must stay the
  bytes you received. A changed file hash breaks verification, deduplication,
  and any downstream integrity claim.
- A sidecar holds strictly more than a container can: selection reasoning,
  probe results, processing steps, position basis, and the provider's raw
  fields, all versioned by a schema.
- It keeps provider data and our data separate. Everything under `source` is
  verbatim from the API; everything under `app` is ours. Container tags blur
  that distinction.

## Consequences

- Moving a file without its sidecar loses the metadata. The collection
  manifest and the ZIP packager both keep them together.
- Players do not show the descriptive metadata unless embedding is enabled.
- Sidecars never contain tokens or signed URLs: the transfer section records
  a host and a hashed path, because a signed CDN URL is itself a credential.
