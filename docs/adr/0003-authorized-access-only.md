# 3. Authorized access only, for Instagram specifically

Status: accepted (2026-09-21)

## Context

yt-dlp's Instagram extractor reaches media through undocumented web endpoints
(`/api/v1/media/{pk}/info/`, `/api/graphql` with a hard-coded `doc_id`), a
scraped LSD token, browser TLS impersonation for the logged-out path, and an
HTML fallback that parses `data-sjs` JSON. It also does not download images:
only `video_versions` become formats, and a single-image post raises "There is
no video in this post".

Meta's official Instagram Platform covers professional accounts that have
authorized your app, exposes `media_type`, `media_url`, and a `children` edge,
and returns images as first-class media.

## Decision

The Instagram connector uses the official Graph API exclusively. Private
endpoints are not used, not as a fallback, and not behind a flag.

## Why

- The private path cannot do the main thing this tool is for: downloading
  images and mixed carousels.
- It depends on hard-coded client identifiers and a GraphQL `doc_id` that
  break without notice.
- It requires either a personal session cookie or TLS impersonation, both of
  which this project rules out (ADR 2).
- The official path returns exactly the metadata the domain model needs,
  including a documented reason why media is sometimes withheld.

## Consequences

- Only the authorizing professional account's own media is reachable. A URL
  for anyone else's post returns
  `NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT`, and the connector makes no request
  to any non-Graph host while establishing that — asserted by a test.
- The 10,000 most-recent-media cap on the user media edge becomes a real
  limit, reported as `NOT_IN_INDEX_WINDOW` rather than a confusing "not found".
- When Meta withholds `media_url` for copyright reasons, that asset alone is
  marked `UNAVAILABLE_COPYRIGHT`; its siblings in a carousel still download,
  and the cover image is never substituted for the video.
