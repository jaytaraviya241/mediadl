# 2. No browser cookies, ever

Status: accepted (2026-09-21)

## Context

The common way to download "your own" social media content is to lift the
browser's session cookie. yt-dlp implements this thoroughly: it reads and
decrypts cookie stores from Chromium, Firefox, and Safari using the OS
keyring, Keychain, or DPAPI, and attaches the result to media requests.

It works, and it is the single most effective shortcut available.

## Decision

mediadl does not import browser cookies or cookie files, and exposes no
option to do so. The guarded HTTP client keeps an ephemeral per-operation
cookie jar for CDNs that set technical cookies during a redirect; it is never
persisted and never shared across hosts or jobs.

## Why

1. **Blast radius.** A browser profile holds sessions for every site the
   person uses, not just the target. Reading that store hands all of them to
   this process.
2. **A session cookie is a bearer credential.** If it reaches a log, an
   info JSON, or a crash dump, the account is compromised. Scoped OAuth
   tokens fail far more gracefully.
3. **No scope limitation.** A session grants everything the user can see. An
   OAuth token grants what was asked for and approved.
4. **It is not an authorized integration.** Driving a personal session
   programmatically conflicts with Instagram's Terms of Use, which prohibit
   collecting information in an automated way without express permission.
5. **It is unstable in a way that looks like a bug.** Providers invalidate
   sessions constantly; yt-dlp carries dedicated code to detect exactly that.

## Consequences

- Media that is only reachable with a logged-in personal session cannot be
  downloaded. This is the largest functional difference from tools that do use
  cookies, and it is intentional.
- Users of personal accounts are directed to Instagram's official data export
  and the user-files connector.
- A future connector needing cookies must obtain them through the provider's
  documented authorization flow and pass a security review.
