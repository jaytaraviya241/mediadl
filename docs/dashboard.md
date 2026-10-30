# The dashboard

A local control panel over the same job store the CLI uses. A job started in
the browser appears in `mediadl jobs list`, and vice versa — there is one
engine, with two front ends.

```bash
mediadl serve                      # http://127.0.0.1:8765, opens a browser
mediadl serve --port 9000 --no-open
mediadl serve --config ./my.toml
```

It binds to loopback and **refuses any other bind address**. There is no
authentication, because it assumes one local user with access to their own
files. Exposing it on a network would need the multi-tenant controls in
[the spec](ARCHITECTURE_SPEC.md) §9.3: per-principal authorization, sessions,
and signed download URLs.

## Prove it works first

**Start here → Run a self-test.** It generates a 256x256 PNG (and, with
FFmpeg, a two-second clip) inside the app's data directory, then runs them
through the ordinary pipeline: resolve, select, transfer, probe, finalize,
sidecar. No network, no credentials.

A green result isolates the install from everything else. If it passes and a
real download then fails, the problem is that source or that account, not
mediadl. The same check runs in CI through `tests/integration/test_dashboard_ui.py`,
which loads the page in a real DOM and presses the button.

## The complete user flow

The six steps below are the strip across the top of the dashboard; the current
one is highlighted while a job runs.

### 1. Connect — only if you need a platform account

Skip this entirely for direct URLs, your own files, and open streams. It
matters only for Instagram. See **Credentials** below.

### 2. Choose a source

Paste one of:

| Input | Goes to | Needs an account? |
|---|---|---|
| `https://…/photo.jpg` | `direct_url` | no |
| `/path/to/file.mp4` | `user_files` | no |
| `/path/to/folder` | `user_files` (ordered collection) | no |
| `https://…/index.m3u8` or `.mpd` | `stream_manifest` | no |
| `https://www.instagram.com/p/<shortcode>/` | `instagram_graph` | yes |
| `ig-media:<id>` | `instagram_graph` | yes |

As you type, the dashboard names the connector that would handle it and warns
if that connector needs an account you have not connected. The rule it uses is
the same one the server applies, so the hint cannot disagree with reality.

### 3. Resolve and select

The connector turns your input into typed `Asset`/`Collection` objects, and the
selection engine ranks the representations and picks one. Open a job's
**Details** to see every representation that existed and the reason the winner
won, for example `picked 1080x1920 h264 aac; excluded 2 (EXCLUDED_DRM)`.

A carousel becomes one job with one item per photo or video, numbered in the
order the API returned them.

### 4. Transfer

Bytes stream to a `.part` file with a live progress bar per asset. Transfers
are resumable, size-capped, and verified: the content type is checked against
the magic bytes, and the final length against `Content-Length`.

**Cancel** stops the job between chunks and cleans up. Partial progress stays
on disk, so a later retry resumes rather than restarting.

### 5. Process

FFmpeg assembles what needs assembling — merging separate audio and video,
remuxing segments — always with a stream copy under the default policy. Every
file is then probed, and the measured properties are what get recorded.

### 6. Files on disk

The job drawer lists what was written, with thumbnails for images, the size and
SHA-256 of each file, a **Save** link, and a link to its `.meta.json` sidecar.

A collection whose items did not all succeed finishes as **PARTIAL SUCCESS**:
the ones that worked are written with their original position numbers, and the
manifest records why the others did not. That is a normal outcome, not an error
state — one withheld video in a carousel does not cost you the other four
photos.

## Which credentials are required

The dashboard's **Credentials** tab renders this live, with the status of your
own installation. In short:

| Source | Credential | Status |
|---|---|---|
| Direct media URL | **none** | works immediately |
| Your own files and folders | **none** | providing the file is the authorization |
| HLS / DASH manifest | **none** | DRM streams are refused, not unlocked |
| Instagram | **OAuth via Instagram Business Login** | see below |
| FFmpeg / FFprobe | not a credential — an installed program | needed for merge, remux, transcode, probe, and segmented streams |

### Instagram: the three values

| Value | Secret? | Where to add it | Where to obtain it |
|---|---|---|---|
| **Instagram App ID** | no | Dashboard → Connections → step 1; or `[instagram_graph] client_id`; or `MEDIADL_INSTAGRAM_GRAPH__CLIENT_ID` | developers.facebook.com → your app → Instagram → *API setup with Instagram login* → step 3 |
| **Instagram App Secret** | **yes** | Dashboard → Connections → step 2 only. Used once, never written to disk | Same screen as the App ID → *Instagram app secret* → Show |
| **Redirect URI** | no | Dashboard → Connections → step 1; or `[instagram_graph] redirect_uri` | You choose it, then register the exact string under *Set up Instagram business login* → OAuth redirect URIs |

**Before any of that works you need:**

1. An Instagram **professional** account (Business or Creator). Personal
   accounts have had no API access since the Basic Display API shut down on
   2024-12-04. Convert in the Instagram app: Settings → Account type and tools
   → Switch to professional account.
2. A Meta app (type *Business*) at
   [developers.facebook.com/apps](https://developers.facebook.com/apps) with
   the Instagram product added.
3. Your Instagram account attached to that app with a role — that is Standard
   Access, which is enough for your own media. Serving accounts you do not own
   or manage additionally requires **Advanced Access**, App Review, and
   Business Verification from Meta.

### What the redirect URI actually is

After you approve access, Meta sends your browser to this address with the
authorization code attached:

```
https://your-domain.example/auth/callback?code=AQBx7f2…&state=k3j9…
```

Meta makes you register it in advance, so a stolen App ID cannot redirect
codes to someone else's server. The match is exact — one extra slash is a
rejection.

**The page does not have to work by default.** You approve, your browser lands
there, and you copy the URL out of the address bar into step 2. A "site can't
be reached" error is fine: the URL with `?code=` is still in the address bar.
For a local callback page instead, use the isolated receiver described below.

So pick whichever is easier:

- **You own a domain** — anything under it, e.g.
  `https://yourdomain.com/mediadl/callback`.
- **You don't** — GitHub Pages gives free HTTPS in a couple of minutes: enable
  Pages on any repo and use `https://your-username.github.io/callback`.

Then paste the identical string in both places: your Meta app under
*Instagram → API setup with Instagram login → Business login settings → OAuth
redirect URIs*, and the dashboard field.

`http://localhost` will not work — the flow is HTTPS only, and Meta does not
document loopback redirects for it (open question 6 in the
[spec](ARCHITECTURE_SPEC.md)).

### Optional isolated local callback receiver

For local development, mediadl can run a deliberately narrow callback page in
a second terminal:

```bash
mediadl oauth callback
```

It listens only on `127.0.0.1:8766/oauth/instagram/callback`, accepts only a
browser `GET`, and does not expose the dashboard, API, files, jobs, app secret,
authorization code, or state. If you use an HTTPS tunnel, point the tunnel at
this listener **only** — never at dashboard port `8765`:

```bash
cloudflared tunnel --url http://127.0.0.1:8766
```

Register the resulting public HTTPS URL with
`/oauth/instagram/callback` appended in Meta, for example:

```text
https://temporary-name.trycloudflare.com/oauth/instagram/callback
```

After approval, the receiver confirms that the browser reached it. Copy the
complete address-bar URL, including `code` and `state`, into dashboard step 2
or the CLI prompt. The receiver never saves those values. Quick tunnel URLs
change whenever the tunnel restarts, so use a stable owned domain or named
tunnel for repeated use.

### Why the secret is not stored

The app secret is needed twice: to exchange the authorization code, and to
exchange the short-lived token for a 60-day one. Both happen in the same
request cycle. Refreshing that token afterwards uses
`ig_refresh_token`, which needs only the token itself. So mediadl uses the
secret once and discards it, and the only thing at rest is the token —
encrypted with AES-256-GCM under a key in your OS keychain, bound by AAD to
the connection it belongs to.

### What you will never be asked for

These have no input field anywhere, because no code path could consume them:

- **your Instagram password** — authorization happens on Meta's own login page;
- **a browser session cookie** — see [ADR 2](adr/0002-no-browser-cookies.md);
- **a proxy or VPN** — geo-restriction evasion is not implemented;
- **DRM keys or a Widevine CDM** — protected streams are refused up front.

## API

The UI is a client of a documented HTTP API; OpenAPI is at `/docs` while the
server runs.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | output root, policy, connectors, FFmpeg build and licence, limits |
| GET | `/api/credentials` | requirements, live status, where to add and obtain each value |
| GET | `/api/jobs` | recent jobs with live progress |
| POST | `/api/jobs` | submit `{source, connection_id?}` and start it |
| GET | `/api/jobs/{id}` | items, events, error detail |
| GET | `/api/jobs/{id}/outputs` | files written, with size and SHA-256 |
| POST | `/api/jobs/{id}/retry` \| `/cancel` | re-queue or stop |
| GET | `/api/events` | SSE: `progress`, `job.*`, and reconciling `snapshot` frames |
| GET | `/api/files?path=` | serve a file, refusing anything outside the output root |
| GET | `/api/connections` | connected accounts and token expiry |
| POST | `/api/connections/instagram/start` \| `/complete` | the two-step OAuth flow |
| DELETE | `/api/connections/{id}` | delete the stored token |

Domain errors arrive as `{code, message, retryable, detail}` with the code the
rest of the system uses — `NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT`,
`UNAVAILABLE_COPYRIGHT`, `AUTH_EXPIRED` — so the UI explains what happened
instead of showing a status number.
