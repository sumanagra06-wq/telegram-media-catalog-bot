# Release Audit Report

This report records checks performed before packaging. It is not a claim that live Telegram/Railway integration can be proven without real credentials and private channels.

## Scope checked

- Telegram snapshot transaction order, manifest recovery, checksums and previous-revision fallback
- Dynamic categories, legacy channels and idempotent source-message updates
- User access states and exact watchlist statuses
- Search ranking, pagination and callback-size constraints
- Series episodes, variants and split season packs
- Protected delivery and Telegram file-ID fallback
- Native user/owner command scopes
- Private-chat and owner authorization filters
- Caption metadata allowlisting and supplied real-world formats
- HTML escaping of user/catalog text
- Railway webhook secret, synchronous handler completion and one-replica configuration
- Secret-file exclusion and release archive contents
- Python compilation, lint, formatting, tests, dependency vulnerabilities and Bandit scan

## Findings fixed during the release audit

1. Upgraded `aiohttp` from 3.14.1 to 3.14.3 after the dependency audit found three published vulnerabilities in 3.14.1.
2. Corrected `/start` content deep-link parsing so stable IDs retain the `c_` prefix.
3. Owner-scoped native commands are now retried automatically when the owner sends `/start`; a redeploy is no longer required after first contact.
4. Added missing access checks to Browse-category and Recently Added callbacks.
5. Escaped catalog titles in owner error notifications to prevent malformed HTML injection.
6. Improved multiline caption parsing and scanning across caption plus filename.
7. Preserved content IDs/watchlist relationships when correcting the only file's title or year.
8. Added a versioned state base model, pinned production dependency versions, and clean Replit/Python runtime metadata.

## Automated results at package time

- Ruff lint: passed
- Ruff formatting check: passed
- Python compileall: passed
- Pytest with warnings treated as errors: passed (34 tests)
- Test coverage: 54% overall; parser 95%, snapshot storage 83%, services 79%, with live Telegram/Railway branches necessarily unexecuted without credentials
- Bandit: passed; the required Railway `0.0.0.0` bind is explicitly documented/suppressed
- pip-audit: passed, no known vulnerabilities in production requirements
- Mypy: core models, parser, storage, repositories, services, UI, commands and filters passed
- Placeholder scan: no TODO/FIXME/NotImplemented markers

The Aiogram handler layer is linted, compiled and exercised through domain/UI tests, but is not claimed as fully strict-mypy-clean because Aiogram's runtime filters narrow optional callback/message fields in ways its static types do not express.

## Tests cover

- Every supplied Maamla Legal Hai caption pattern
- LOST hyphenated episode syntax
- Game of Thrones spaced episode syntax
- Game of Thrones `.zip.001/.002/.003` season packs
- Labeled and filename-style movie metadata
- Multiline unlabeled title parsing
- Category creation and series grouping
- Duplicate channel-update idempotency
- Single-file metadata correction preserving content ID
- Exact watchlist status set
- Public/approval user state
- Failed state commit rollback
- Snapshot restart restore and corrupt-current fallback
- Search ranking
- Episode/pack navigation structures
- Telegram callback-data size limits
- Plain-text user search → content screen → episode protected-delivery handler flow
- Automatic channel-post indexing handler flow with allowlisted metadata only
- Security-sensitive environment-variable validation

## Honest remaining limits

- A real end-to-end Telegram and Railway smoke test still requires the owner's bot token, owner ID, private channel IDs and Railway domain. Those secrets were not available during this audit.
- Telegram-only persistence is appropriate for the stated 40–50-user scale, not high-write public scale.
- The standard Bot API cannot scan arbitrary old channel history; `/index` is required for missed old posts.
- Telegram protected content blocks normal official-client forwarding/saving but is not absolute DRM against modified clients or external capture.
- The normal Bot API snapshot download ceiling still applies; the app rejects compressed snapshots near that limit instead of silently corrupting recovery.
- Exactly one Railway replica must remain configured because there is no external distributed lock.
