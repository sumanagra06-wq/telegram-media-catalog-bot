# Release Audit Report

This report records checks performed before packaging. It is not a claim that live Telegram/Railway integration can be proven without real credentials and private channels.

## Scope checked

- Telegram snapshot transaction order, manifest recovery, checksums and previous-revision fallback
- Dynamic categories, legacy channels and idempotent source-message updates
- User access states, exact watchlist statuses, manual/catalog entry flows, and read-only public sharing
- User-scoped watchlist mutation authorization and private-visibility enforcement
- Owner-only catalog removal, tombstones, Telegram deletion retry, and manual-cleanup confirmation
- Search ranking, pagination and callback-size constraints
- One persisted pinned dashboard plus one persisted/reused temporary workspace per user
- Sliding five-minute workspace expiry, callback/message activity resets, restart cleanup, and concurrent-open serialization
- Search/Browse/Recently Added checkbox emulation, cross-page selection, and atomic bulk Watchlist insertion
- Alphabetical Watchlist-library browsing, in-category search, unsaved filtering, A–Z/# jumps, pagination, Select Page/Clear/review power tools, persistent ticks and atomic status insertion
- Custom Watchlist batches of up to 25 normalized/deduplicated titles, preview ticks, one status, and one atomic commit
- Always-public community Watchlists with editable, HTML-safe display names and visible usernames, including owner edit/reset moderation
- Post-delivery workspace replacement below protected media without leaving two active workspaces
- Unified owner dashboard with an authorization-checked Admin Control Center entry
- Semantic Bot API button styles (`primary`, `success`, `danger`) with neutral secondary actions
- Unicode-emoji labels and consistent HTML card hierarchy across user and owner screens
- Absence of custom-emoji dependencies and preservation of existing callback contracts
- Series episodes, variants and split season packs
- Protected delivery, structured delivery captions and Telegram file-ID fallback
- Dashboard-final native command scope, emergency dashboard repost/rollback, hidden `/start` onboarding, and owner-only reply-based `/index` recovery
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
9. Fixed filename-style values after `Title:`/`Name:` so episode tokens never become part of the catalog identity; added persistence-boundary canonicalization and an idempotent startup migration that merges affected titles without re-uploading Telegram media.
10. Replaced direct catalog-screen watchlist controls with a dedicated panel supporting dynamic categories, manual and indexed titles, explicit status selection, public-by-default read-only community lists, and the privacy toggle used in that release. Added versioned migration for legacy entries. The privacy policy is superseded by finding 17 below.
11. Added owner-only, double-confirmed catalog-title removal. The catalog commit blocks delivery first, source tombstones prevent re-indexing, recent Telegram posts are deleted in batches, failures remain retryable, and old posts can be manually confirmed after Telegram's deletion window expires.
12. Reworked the native Telegram interface into concise card-style screens with clear hierarchy, standard Unicode emoji, official semantic button colors, richer search/media/Watchlist/admin states, neutral back/cancel controls, and prominent double-confirmed destructive actions. Existing callback expressions remain present and no custom-emoji IDs are used.
13. Added the command-optional interaction layer without removing the legacy layer: `/start` creates, recovers, updates and pins one dashboard; dashboard actions create or reuse one serialized workspace; all workspace callbacks and relevant text-input continuations reuse that message; inactivity deletes it after five minutes; startup removes stale workspace cards while preserving dashboards.
14. Added selectable Search, Browse and Recently Added results with separate checkbox/title buttons, selection retention across pages, a 25-title safety bound, one status picker using only `to_watch`, `on_hold` and `completed`, and one atomic user-snapshot commit for the full bulk operation.
15. Added per-user async locking so simultaneous dashboard/workspace opens cannot create duplicate new-system cards, plus message-reference compare-and-clear guards so an old expiry task cannot clear a newer workspace.
16. Set pytest-asyncio's fixture loop scope explicitly so the warnings-as-errors audit remains clean with current tooling.
17. Replaced the community privacy control with an editable 40-character community display name. Schema v4 converts every previously private list back to public, the repository rejects future private-mode requests, the old privacy callback remains handled for historical cards, and the real Telegram username remains visible in the community directory when available.
18. Replaced the new Watchlist catalog-entry screen with a complete category catalog: case-insensitive alphabetical ordering, six titles per page, A–Z/# direct filtering, checkbox/title-button toggles, selection retention across filters and pages, already-saved markers, a 25-title bound, and atomic bulk status insertion. The former typed-search callbacks remain registered for historical cards.
19. After successful protected delivery, the lifecycle manager deletes or disables the previous workspace and posts a fresh temporary dashboard below the file. The pinned dashboard remains untouched, the replacement becomes the sole active workspace, and delivery success is never rolled back if repositioning controls fails.
20. Replaced single custom-title entry with category-first batches of up to 25 one-per-line titles. Input is whitespace-normalized, deduplicated by normalized title, length/limit validated, selected by default in a tick preview, assigned one of the same three statuses, and inserted/updated in one rollback-safe snapshot transaction.
21. Strengthened the Watchlist library picker without replacing its alphabetical model: in-category search, unsaved-only filtering, Select Page, Clear Selection and selected-only review now compose with pagination, A–Z/`#`, saved markers, the 25-title bound and atomic bulk insertion.
22. Added owner moderation of another user's public Community display name from User Management, including edit, reset-to-Telegram-name, validation, escaped rendering and catalog audit events. Telegram profile fields and mandatory-public visibility remain unchanged.
23. Declared the dashboard final: removed legacy user/admin command handlers and command-menu entries, retained hidden Telegram `/start` onboarding/deep links and owner reply-based `/index`, and exposed only `/dashboard`. The emergency action posts and pins a fresh dashboard, retires the old card, and rolls back safely if snapshot persistence fails.

## Automated results at package time

- Ruff lint: passed
- Ruff formatting check: passed
- Python compileall: passed
- Pytest with warnings treated as errors: passed (95 tests)
- Test coverage: 64% overall; parser 98%, snapshot storage 84%, repositories 74%, services 87%, panel handlers 61%, Watchlist handlers 60%, delivery/search handlers 48%, panel lifecycle manager 66%, UI 77%, and presentation styles 96%, with credential-dependent Telegram/Railway branches necessarily unexecuted
- Bandit: passed; the required Railway `0.0.0.0` bind is explicitly documented/suppressed
- pip-audit: passed, no known vulnerabilities in production requirements
- Mypy: core configuration, models, parser, storage, repositories, services, panel lifecycle, UI, presentation and command modules passed; the panel handler module also passed independently with imported handler modules skipped because Aiogram runtime narrowing is not represented statically
- Command/callback audit: only hidden `/start`, visible `/dashboard`, and hidden owner `/index` command handlers remain; new callbacks are bounded to Telegram's 64-byte limit and legacy privacy callbacks remain safe for historical cards
- Representative rendering audit: custom batches at the 25-title/160-character boundary and the expanded picker passed Telegram text/button length, callback-size, HTML escaping, style-value and no-custom-emoji checks
- Placeholder scan: no TODO/FIXME/XXX/HACK/NotImplemented markers
- Secret and sensitive-file scans: clean

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
- Official button-style serialization and semantic blue/green/red/default assignment
- Neutral cancel/back/suspend/disable controls and destructive red confirmations
- Optional style metadata preserving text and callback behavior on older clients
- Main, Watchlist and admin dashboard callback-contract preservation
- No custom-emoji IDs on primary dashboards
- Plain-text user search → content screen → episode protected-delivery handler flow
- Automatic channel-post indexing handler flow with allowlisted metadata only
- Six separate `Operation Safed Sagar ... S01E01`–`S01E06` messages, including filename-style labels and forwarding warnings, grouping as one title with six files
- Idempotent repair of a persisted six-title/six-file snapshot into one title/six files while preserving Telegram file IDs and source message references
- Corrected File Database repair cards using one shared content ID
- Custom multi-title and existing-catalog Watchlist panel flows with dynamic category selection
- Custom batch normalization/deduplication, preview ticks, 25-title/160-character bounds, duplicate updates, one-commit insertion and failed-commit rollback
- Public read-only community lists, user/owner editable display names, owner reset/audit, always-public enforcement, and bounded callback data
- Legacy watchlist schema migration and idempotent rekeying
- Owner title removal deleting all catalog files, preserving unrelated watchlists, and blocking old-source re-indexing
- Failed Telegram source deletion remaining safely tombstoned, retry succeeding, and manual cleanup confirmation
- Failed catalog-removal snapshot commit rolling back without partial in-memory deletion
- Security-sensitive environment-variable validation
- Pinned-dashboard persistence, in-place reuse, deleted-message recovery and repinning
- Concurrent workspace opens producing only one temporary message
- Sliding inactivity expiry, manual activity reset and automatic deletion
- Restart cleanup clearing only workspace references while preserving dashboards
- Search queries editing the active workspace instead of creating a third result card
- Selectable Search, Browse and Recently Added result rendering
- Checkbox state across pages, callback-driven toggling and the 25-title bound
- Bulk status selection and one-commit insertion/update behavior
- Failed bulk snapshot commit rolling back every selected title atomically
- Non-owner rejection at the Admin Control Center handler even when a valid dashboard message is supplied
- Legacy dashboard callback contracts continuing alongside the new panel routes, except the intentionally removed privacy button; its callback handler remains safe for old cards
- Watchlist-library pagination, alphabetical ordering, A–Z/# picker contents and direct filtering
- In-category search, unsaved-only filtering, Select Page, Clear Selection and selected-only review
- Tick selection surviving filter changes and bulk status insertion from the dedicated Watchlist add flow
- Community display-name editing, length validation, HTML escaping and username retention
- Schema-v4 conversion of previously private Watchlists to always-public state
- Successful media delivery replacing the old workspace with one new dashboard below the file while preserving the pinned dashboard
- Dashboard-only command registration, emergency repost retirement of the old pinned card, and failed-snapshot rollback without losing the previous dashboard reference

## Honest remaining limits

- A real end-to-end Telegram and Railway smoke test still requires the owner's bot token, owner ID, private channel IDs and Railway domain. Those secrets were not available during this audit.
- Telegram-only persistence is appropriate for the stated 40–50-user scale, not high-write public scale.
- The five-minute timers are intentionally in-process. Workspace message IDs are persisted, and every restart attempts to delete or disable those stale cards before clearing their references rather than trying to resume uncertain pre-restart deadlines.
- Dashboard pinning is attempted through the Bot API. Hidden `/start` recovers onboarding/deep-link dashboards, while `/dashboard` deliberately reposts the emergency replacement; if Telegram denies replacement pinning, an existing tracked dashboard is preserved.
- The standard Bot API cannot scan arbitrary old channel history; `/index` is required for missed old posts.
- Telegram normally limits bot message deletion to messages sent within 48 hours. Older source posts require manual owner deletion; catalog tombstones still block delivery and re-indexing immediately.
- Telegram protected content blocks normal official-client forwarding/saving but is not absolute DRM against modified clients or external capture.
- Older Telegram clients that predate semantic button styles render the same buttons with neutral/default styling; callback behavior is unchanged.
- The normal Bot API snapshot download ceiling still applies; the app rejects compressed snapshots near that limit instead of silently corrupting recovery.
- Exactly one Railway replica must remain configured because there is no external distributed lock.
