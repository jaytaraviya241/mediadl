# Threat model

Scope: the mediadl core, its connectors, and a local or single-tenant
deployment. Multi-tenant hosting adds the controls marked *backend*.

The program's defining property is that it only fetches what a user is
authorized to fetch. Most of what follows protects that property, or protects
the machine from the data it fetches.

## Assets worth protecting

| Asset | Why it matters | Where it lives |
|---|---|---|
| Provider access tokens | Bearer credentials for someone's account | `connections` table, AES-256-GCM, key in OS keychain |
| The app secret | Mints tokens for every user of the app | Never persisted; used once during connect |
| Downloaded media | Often private to the user | Output root, `0700` temp dirs |
| Signed CDN URLs | Short-lived credentials in URL form | In memory; never logged or written to sidecars |
| The host network position | An SSRF turns this into an internal client | Guarded HTTP client |
| The job database | Reveals what was downloaded and by whom | App data dir, `0600` |

## Trust boundaries

1. **User input → core.** URLs and paths are untrusted. Nothing user-supplied
   becomes a filesystem path without sanitization, and no provider string
   becomes a path component at all.
2. **Core → provider API.** Responses are untrusted data. They are normalized
   into typed objects and validated against the domain contract; a violation
   is a connector bug, not a user-visible failure mode.
3. **Core → CDN.** Media bytes are hostile input. They are sniffed, size
   capped, and probed before anything else touches them.
4. **Core → FFmpeg.** A subprocess handed untrusted files, run with an
   argument list, a protocol allowlist, rlimits, a timeout, and its own
   process group.

## Threats and controls

### T1 — SSRF via a supplied or redirected URL
An attacker supplies a URL, or a server redirects, to `127.0.0.1`,
`169.254.169.254`, or an RFC 1918 address.

*Controls.* `vet_url` resolves the host itself and requires **every** returned
address to be public; one private answer rejects the host. IP literals
(including decimal, octal, and hex shorthand, IPv4-mapped IPv6, 6to4, Teredo,
NAT64, CGNAT, and known metadata endpoints) are vetted directly rather than
sent to DNS. The connection is then **pinned** to a vetted IP with the Host
header and SNI preserved, which closes DNS rebinding. Redirects are followed
manually, at most five hops, re-vetted per hop, with HTTPS→HTTP downgrades
refused. Connectors may further restrict hosts via `allowed_media_hosts`,
matched on a dot boundary so `evilcdninstagram.com` does not pass for
`cdninstagram.com`.

*Residual.* When FFmpeg is delegated a remote manifest it does its own DNS and
cannot be pinned. The native HLS/DASH engines are used instead, and every
segment and key URI goes through the same guard.

### T2 — Credential leakage
A token reaches a log, a sidecar, an exception, or a CDN.

*Controls.* Tokens are attached only for the connector's own API hosts and are
dropped on any cross-origin redirect; `HttpLocator.send_credentials` defaults
to false, so media requests carry none. A logging filter masks ~20 secret key
patterns in messages, structured fields, and exception text. `MediaDLError.public()`
runs the same filter. Sidecars record a host and a hashed path instead of a
URL, because signed URLs are credentials. `AccessCredential.__repr__` and
`__str__` both redact. Tests assert a placeholder token appears in no log, no
error payload, and no sidecar.

### T3 — Path traversal and unsafe filenames
A provider returns `../../etc/passwd` as a username, or a name that is
hostile on Windows.

*Controls.* Path components are never taken raw: each templated field is NFC
normalized, stripped of `/\:*?"<>|` and control characters, collapsed,
de-dotted, byte-capped at 80, and suffixed if it is a Windows reserved name.
The final path is resolved and required to be under the output root, symlinked
parents are rejected, and files are created with `O_CREAT|O_EXCL|O_NOFOLLOW`.
Extensions come from an allowlist per media kind, not from the URL. Property
tests over arbitrary text assert the sanitizer always yields one safe segment.

### T4 — Malicious media
A decompression bomb, a truncated container, or a file whose declared type
lies.

*Controls.* Magic bytes must agree with the declared kind, checked on the first
chunk while streaming rather than after the fact. Size caps apply before
(`Content-Length`) and during (byte count) the transfer. `ffprobe` measures
dimensions and duration against configured caps; Pillow's decompression-bomb
guard runs on images. FFmpeg gets `-nostdin`, `-protocol_whitelist file,pipe`,
CPU and file-size rlimits, and a wall-clock timeout that kills the whole
process group.

### T5 — Manifest-driven attacks
A playlist or MPD points at internal addresses, expands entities, or demands
DRM.

*Controls.* Manifests are size capped. MPDs are parsed with `defusedxml`, so
external entities and entity expansion do not apply. Every segment and key URI
goes through the URL policy. DRM is refused at parse time — SAMPLE-AES, any
non-identity `KEYFORMAT`, `skd://`, `EXT-X-SESSION-KEY`, and any
`ContentProtection` element — before a single segment is requested, which a
test asserts by checking the segment's hit count is zero.

### T6 — Authorization bypass between users (*backend*)
One principal reaching another's connection, job, or file.

*Controls.* Connection lookups are always scoped by `principal_id`; passing a
correct connection id belonging to someone else returns nothing. A ciphertext
is bound by AAD to `connection_id`, `principal_id`, and key version, so a row
moved between connections fails to decrypt. Disconnect hard-deletes the row.

### T7 — OAuth flow attacks
CSRF on the callback, code replay, or a redirect to an attacker's endpoint.

*Controls.* `state` is mandatory (the docs call it optional), 256 bits, stored
hashed, single use, and expires in 10 minutes. The redirect URI must match the
registered value exactly. The `#_` suffix is stripped before exchange. The
authorization code is used once, immediately. Only `instagram_business_basic`
is requested; no publishing, messaging, or comment scopes.

### T8 — Forged platform callbacks
A fake deauthorize or data-deletion POST.

*Controls.* `signed_request` is verified with HMAC-SHA256 over the exact
encoded payload using a constant-time comparison, before anything is acted on.
An unverified payload is never parsed for meaning.

### T9 — Resource exhaustion
A very large file, a huge collection, or a retry storm.

*Controls.* Per-asset byte caps, per-collection asset caps, duration and pixel
caps, per-host and per-job concurrency limits, bounded retries with full-jitter
backoff, `Retry-After` honoured, and a circuit breaker per connector. A
provider asking for a wait longer than the park threshold releases the worker
instead of sleeping on it.

### T10 — Corruption across a resume
A partial file is completed with bytes from a different representation.

*Controls.* Resume requires a strong validator and sends `If-Range`; a 200
response means the validator changed, so the file restarts. A 206 must report
exactly the requested offset. A 416 completes the file only on an exact length
match *and* matching validators — unlike yt-dlp, which tolerates +/-100 bytes.
The final length is checked against `Content-Length`. Every row of this table
has a test.

## Deliberate non-goals

DRM circumvention, cookie import, TLS fingerprint impersonation, geo-bypass,
proxy rotation, CAPTCHA solving, and private endpoint use are not implemented
and not configurable. They are not omitted for lack of time; the program is
organized so that they have nowhere to live.
