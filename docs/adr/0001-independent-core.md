# 1. An independent core, not a yt-dlp fork

Status: accepted (2026-09-21)

## Context

yt-dlp solves an overlapping problem and is public domain (Unlicense), so
copying it is legally permissive. Three options were considered: reimplement
independently, depend on yt-dlp at runtime, or vendor parts of its source.

## Decision

Reimplement independently. yt-dlp's *architecture* is reused as a set of ideas:
a normalized result model, an extractor/connector registry, transfer engines
dispatched by protocol, lexicographic format ranking, and staged
post-processing. None of its code is.

## Why

- **Behavioural control.** yt-dlp defaults to `geo_bypass=True`, ships browser
  cookie extraction and TLS impersonation, has no SSRF guard, and tolerates a
  +/-100 byte mismatch when completing a resumed download. Every one of those
  is a decision this project makes differently. Inheriting them and then
  disabling them one at a time is a worse position than not having them.
- **Coupling.** Copied code depends on `yt_dlp.utils` (~5,700 lines),
  `traverse_obj`, and untyped info-dict conventions. Lifting one manifest
  parser drags in a large surface with its own assumptions.
- **Maintenance.** Site extractors are volatile by nature; `instagram.py` was
  reworked and patched four times in four weeks. Spec-driven code (RFC 9110,
  RFC 8216, ISO/IEC 23009-1) and documented APIs change far more slowly.
- **Licensing of what we ship.** The yt-dlp source is Unlicense, but its
  `default` extra pulls in mutagen (GPL-2.0-or-later) and its official
  executables aggregate GPL/LGPL/MPL components. Not depending on it keeps the
  distribution question simple.

## Consequences

- More work up front, concentrated in HTTP transfer, an HLS/DASH subset, and
  the Instagram Graph connector.
- We own every behaviour, and every one of them is tested here.
- A yt-dlp *runtime adapter* remains possible behind the connector boundary
  for operators who want broad site coverage for sources they are authorized
  to use. It is not implemented, would be off by default, would be restricted
  with `allowed_extractors`, and would never be used for Instagram.
