# mediadl

A media downloader built around one rule: **it only retrieves what the user is
authorized to retrieve, through documented interfaces.** It keeps the bytes it
receives, records exactly what it did, and never guesses.

It is an independent implementation. yt-dlp was studied for its architecture —
the normalized result model, a connector registry, protocol-dispatched transfer
engines, lexicographic format ranking, staged post-processing — and none of its
code is used.

## What it does

- **Images, video, and audio are peers.** A carousel of three photos is as
  much a first-class download as a video, and a mixed carousel reports
  per-item status without renumbering what survived.
- **Instagram through the official Graph API**, scoped to the professional
  account that authorized the app.
- **Direct media URLs**, **local files and data exports**, and **unprotected
  on-demand HLS/DASH**.
- **Source-preserving by default.** FFmpeg is used to assemble streams
  (`-c copy`), never to silently re-encode. Two other policies exist and say
  plainly when they re-encode.
- **Resumable, idempotent jobs** with a persisted state machine, so an
  interrupted download continues instead of starting over.

## What it will not do

These are architectural constraints, not settings:

- no DRM circumvention (FairPlay, PlayReady, Widevine, SAMPLE-AES, DASH
  `ContentProtection` — all refused before a byte is fetched);
- no browser-cookie import or session reuse;
- no browser TLS fingerprint impersonation;
- no geo-restriction evasion, proxy rotation, or identity switching;
- no undocumented or private platform endpoints;
- no CAPTCHA, login-challenge, or age-verification bypass;
- no rate-limit evasion.

Consequently mediadl **cannot** download other people's Instagram posts, private
accounts, or personal (non-professional) accounts. That is the point, not a gap.
If you need your own media from a personal account, use Instagram's official
"Download your information" export and import it with the user-files connector.

## Quick start

Requires Python 3.12+. FFmpeg is optional but wanted: without it, single-file
downloads still work, while merging, stream assembly, transcoding and probing
do not.

```bash
git clone <this repo> && cd ig-downloader
python3 -m venv .venv
.venv/bin/pip install -e ".[server]"      # "server" adds the dashboard
brew install ffmpeg                       # macOS; or your package manager

.venv/bin/mediadl serve                   # opens http://127.0.0.1:8765
```

Then, in the browser:

1. **Start here → Run a self-test.** It generates a sample image and clip on
   your machine and runs them through the real pipeline. Green means the
   install works, with no account and no internet involved.
2. **Start here → step 3** lists the three source types that need no
   credentials at all. Pick one, press *Use this*, and press Download.
3. **Accounts** is only needed for Instagram, and the tab tells you exactly
   what Meta requires before it will work.

Prefer the terminal? Everything above is also:

```bash
.venv/bin/mediadl doctor                  # connectors, FFmpeg build and licence
.venv/bin/mediadl get ~/Pictures/photo.jpg
.venv/bin/mediadl jobs list
```

`doctor` prints your FFmpeg build's licence. A GPL build is fine to *use*;
shipping one inside a product carries GPL obligations, so distribute an LGPL
build instead (see [ADR 5](docs/adr/0005-ffmpeg-licensing.md)).

## Dashboard

`mediadl serve` is a local control panel over the same job store the CLI uses —
a job started in the browser appears in `mediadl jobs list` and vice versa.

- **Start here** — what this does, a live setup check, a one-click self-test,
  the sources that work with no credentials, and the six pipeline stages
  explained.
- **Download** — paste a source; it names the connector that will handle it
  before you press the button, and says so if that connector needs an account.
- **Jobs** — live progress per asset, per-item status, cancel and retry, and a
  detail drawer with the selection reasoning, files written with their
  SHA-256, and the full state history.
- **Accounts** — the guided two-step Instagram connection.
- **Credentials** — for your installation: what each source needs, where to
  add it, where to obtain it, and what you will never be asked for.

It binds to loopback and refuses any other bind address: there is no
authentication, and it assumes a single local user. Full walkthrough:
[docs/dashboard.md](docs/dashboard.md).

## Use

```bash
mediadl get https://cdn.example.com/photo.jpg          # a direct URL
mediadl get ~/Pictures/original.heic                   # your own file
mediadl get ~/instagram-export/media/posts             # a directory, in order
mediadl get https://cdn.example.com/stream/index.m3u8  # unprotected VOD HLS

mediadl formats <source>        # what is on offer, and what would be picked
mediadl jobs list               # recent jobs
mediadl jobs show <id>          # items, errors, full transition history
mediadl jobs retry <id>         # re-run the failed items of a partial job
```

Output policy:

```bash
mediadl get <source> --policy source      # keep the bytes exactly (default)
mediadl get <source> --policy compatible  # H.264/AAC MP4 that plays anywhere
mediadl get <source> --policy efficient   # smaller, and only if it really is
```

Exit codes: `0` complete, `3` partial success, `4` authorization required,
`5` rate limited, `1`/`2` failure.

## Credentials, in one table

| Source | Credential needed | Where to add it | Where to get it |
|---|---|---|---|
| Direct media URL | **none** | — | — |
| Your own files / data export | **none** | — | — |
| HLS / DASH manifest | **none** | — | — |
| Instagram | App ID, App Secret, Redirect URI | Dashboard → Connections, or `mediadl connect instagram`, or `[instagram_graph]` in config | [developers.facebook.com](https://developers.facebook.com/apps) → your app → Instagram → API setup with Instagram login |
| FFmpeg (a tool, not a credential) | the program itself | `[ffmpeg] path`, or leave empty for `PATH` | [ffmpeg.org](https://ffmpeg.org/download.html) or `brew install ffmpeg` |

You are never asked for an Instagram password, a browser session cookie, a
proxy, or DRM keys — there is no code path that accepts them.

## Instagram

You need an Instagram **professional** (Business or Creator) account and your
own Meta app. Personal accounts have had no API access since the Basic Display
API shut down on 2024-12-04.

```bash
mediadl connect instagram \
  --client-id <instagram-app-id> \
  --redirect-uri https://your-registered-redirect/callback
```

The command prints an authorization URL, you approve it in the browser, and
paste the redirect URL back. The app secret is used once, for the code and
long-lived-token exchange, and is never written to disk — refreshing a token
afterwards needs only the token itself. Tokens are stored encrypted with
AES-256-GCM under a key held in your OS keychain.

```bash
mediadl instagram list                                  # your recent media
mediadl get https://www.instagram.com/p/<shortcode>/    # one of your posts
mediadl connect list
mediadl connect disconnect <connection-id>              # deletes the ciphertext
```

Known limits, surfaced as typed errors rather than silent failures:

| Situation | What you get |
|---|---|
| Post belongs to another account | `NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT` |
| Older than the 10,000 most recent media | `NOT_IN_INDEX_WINDOW` |
| Meta withholds `media_url` (copyright/flagged) | `UNAVAILABLE_COPYRIGHT`, per item |
| Token expired or app deauthorized | `AUTH_EXPIRED`, with a reconnect prompt |

Serving accounts you do not own or manage additionally requires Advanced
Access, App Review, and Business Verification from Meta.

## Output layout

```text
instagram_graph/acme.studio/2026-07-04_17896450804038745/
  01-of-03_image_17889455560051451_primary.jpg
  01-of-03_image_17889455560051451_primary.jpg.meta.json
  02-of-03_video_17889455560051452_primary.mp4
  03-of-03_image_17889455560051453_primary.jpg
  collection.manifest.json
```

Each sidecar keeps provider data and our own data strictly apart: `source`
holds verbatim API fields, `app` holds what was selected, measured, and
written, including the SHA-256 and every processing step. Captions never
appear in filenames, and signed media URLs never appear in sidecars.

Outputs are labelled with a `rendition_basis`: `platform_rendition`,
`user_original`, or `direct_source`. Instagram serves transcodes and exposes no
source resolution or bitrate, so mediadl never claims a download is the
original upload.

## Configuration

Copy `config/mediadl.example.toml` and pass it with `--config`, or set
`MEDIADL_`-prefixed environment variables (`MEDIADL_OUTPUT__POLICY=...`).

## Architecture

```text
CLI / library facade
        |
   Job manager (SQLite state machine, leases, idempotency)
        |
   Access policy  ->  Source registry  ->  Connectors (plugins)
        |                                      |
   Selection engine  ->  Transfer manager  ->  Engines (http / hls / dash / file)
        |                                      |
   Media processor (FFmpeg)              Guarded HTTP client (SSRF, pinning, redirects)
        |
   Output manager (naming, atomic finalize, sidecars, manifest)
```

Connectors know platforms; the core knows protocols. A transfer engine is
chosen by **locator type**, never by site, so nothing downstream of a connector
branches on which platform the media came from.

Adding a platform takes no core changes: implement `SourceConnector`, register
it under the `mediadl.connectors` entry-point group, add it to
`sources.enabled`, and pass the contract suite in
`mediadl.testing.connector_contract`.

Full design rationale is in [docs/ARCHITECTURE_SPEC.md](docs/ARCHITECTURE_SPEC.md);
decisions are recorded in [docs/adr/](docs/adr/), the threat model in
[docs/threat-model.md](docs/threat-model.md), and the dashboard and its API in
[docs/dashboard.md](docs/dashboard.md).

## Tests

```bash
.venv/bin/pytest                  # everything except opt-in E2E
.venv/bin/pytest -m "not e2e"
```

Tests run entirely against local fixtures: an in-process HTTP server with
real `Range`/`If-Range`/416/429 behaviour and deliberate fault modes,
FFmpeg-generated media, and recorded, sanitized Graph API responses. Nothing
in the suite touches the public internet or a real account.

## Licence

Apache-2.0. FFmpeg is invoked as a separate program and carries its own
licence, which depends on how it was built.
