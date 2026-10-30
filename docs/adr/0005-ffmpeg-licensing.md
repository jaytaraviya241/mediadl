# 5. FFmpeg is an external program, and its build matters

Status: accepted (2026-09-21)

## Context

FFmpeg is LGPL-2.1-or-later at its core, with optional GPL components;
`--enable-version3` moves those to v3, and `--enable-nonfree` produces a build
that is not redistributable at all. The build used to verify this project
(Homebrew 9.0.1) reports GPL version 3 or later.

## Decision

FFmpeg is invoked as a separate program with an argument list, discovered on
`PATH` or from `[ffmpeg] path`. It is never bundled by default. The
`license_gate` module reads `-buildconf` and `-L`, `mediadl doctor` reports
the result, and `fail_on_nonfree_build` (default true) refuses an
`--enable-nonfree` build.

## Why

- Distribution obligations depend on what you ship and how it is combined. Not
  shipping FFmpeg keeps that question with the operator, who knows their
  situation.
- A build's licence is not visible from its version number. Reporting it makes
  an easy mistake visible before release rather than after.
- `--enable-nonfree` builds cannot be redistributed at all, which is worth
  failing loudly on.

## Consequences

- Users install FFmpeg themselves. Without it, progressive downloads still
  work; merging, remuxing, transcoding, probing, and segmented streams do not,
  and `TOOL_MISSING` says so.
- Anyone bundling FFmpeg should ship an LGPL build (no `--enable-gpl`, no
  `--enable-nonfree`) as a separate executable with its source and notices.
  That excludes libx264 and libx265, so the compatible and efficient policies
  would rely on encoders available in an LGPL build.
- Local processing runs with `-protocol_whitelist file,pipe`, so a crafted
  container or playlist cannot make FFmpeg fetch anything over the network.
