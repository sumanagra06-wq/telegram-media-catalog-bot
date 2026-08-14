# Telegram Media Catalog Bot

A private-channel media catalog for movies and series. Telegram stores both the media and the database snapshots; Railway runs only the application and does not need PostgreSQL, Redis, or a persistent disk.

## Implemented behaviour

- Dynamic private storage categories; no category channel IDs are hardcoded
- Automatic indexing of new and edited video/document channel posts
- Burst-safe catalog ingestion that coalesces up to 100 concurrent channel files into one atomic Telegram snapshot commit and one rate-limited human audit summary
- Owner-assisted `/index` import for an older forwarded channel post
- Messy filename-style metadata parsing with tested support for:
  - `S02E01`, `S02-E19`, and `S03 E01`
  - 360p/480p/720p/1080p/2160p, FHD, UHD, and 4K
  - common language names and abbreviations
  - split season archives such as `.zip.001`, `.002`, `.003`
  - leading uploader mentions, emojis, technical codec text, and forwarding warnings
  - complete filename-style values after labels such as `Name:` or `Title:`
  - `MOVIES.IN:` headers and `Movie No.<n>: <title>` captions with separate metadata lines
  - mixed captions containing multiple filenames, with the descriptive Telegram attachment filename authoritative for media identity and metadata
  - combined episode ranges such as `S18.Ep.1to15` and `Ep31To40`
  - `Eng`/`Jap` language aliases and `FHD → 1080p` normalization
- Only title, year, language, quality, season, episode/range, and pack part are indexed from captions
- Exact, prefix, word, substring, and typo-tolerant title ranking
- Four search results per page
- Series navigation: title → season → episode → variant → protected file; season numbers up to 999 and episode numbers up to 9,999 are parsed, with 20 episodes per UI page
- Season packs: split archives remain Part 1/2/3; combined files are labeled by exact episode range (for example, Episodes 1–15)
- Movie navigation: title → language/quality variant → protected file
- Dedicated Watchlist panel with manual-title and existing-catalog add flows
- Dynamic category selection and exactly three statuses: To Watch, On Hold, Completed
- Always-public, read-only community watchlists with a user-editable community display name
- Owner-only, double-confirmed catalog-title removal that deletes every file record, attempts source-post deletion, and tracks required manual cleanup
- Removed source tombstones prevent edited posts from silently re-indexing; failed Telegram deletions remain retryable
- Public access initially; owner can switch to approval or allowlist
- Dashboard-final interaction model with one emergency `/dashboard` repost command; `/start` remains hidden for Telegram onboarding/deep links and reply-based `/index` is owner-only recovery
- Exactly one recoverable pinned dashboard per user and one temporary interactive workspace
- Sliding five-minute workspace expiry with activity resets and restart cleanup
- Delivery-only Search, Browse, Recently Added, and title/file detail flows; they cannot add to Watchlists
- Checkbox multi-selection only inside Watchlist → Add a title → Choose from the library, with atomic bulk insertion
- One unified owner dashboard with an authorization-checked Admin Control Center entry
- Owner-only Admin Control Center broadcast composer for text/photo/video/document sources, exact all-profile recipient count, preview, one confirmation, paced retry-aware copying, per-recipient dashboard refresh, audit, and sent/failed totals
- Broadcast recipients include every stored registered profile regardless of active, pending, suspended, or banned status; unreachable accounts are counted as failures
- Flat private-chat delivery for every catalog category, with protected files sent directly through `protect_content=True` and no topic routing
- Permanent, button-free delivered files with premium metadata captions; catalog source posts remain unaffected
- Destructive, retry-safe retirement of legacy bot-created delivery/category topics during migration
- Automatic typed-query and temporary-workspace cleanup, followed by one fresh pinned dashboard directly beneath every successful delivery
- Versioned, checksummed gzip snapshots in two private Telegram database channels
- Current and previous snapshot fallback through a pinned manifest
- Idempotent startup repair for legacy episode-specific title records; media is not re-uploaded
- Owner backups, database status, audit, user controls, unavailable-file management, and indexing-failure reporting
- Synchronous webhook processing so a successful HTTP response follows handler/database completion

## Data architecture

```text
Private category channels
├── Movies
├── Series
└── Categories added later from Admin Control Center → Categories

Private File Database channel
├── One pinned TDB_MANIFEST_V1 control message
├── Current catalog snapshot document
├── Previous catalog snapshot document
└── Human-readable indexing audit messages

Private User Database channel
├── One pinned TDB_MANIFEST_V1 control message
├── Current user/watchlist snapshot document
└── Previous user/watchlist snapshot document
```

The catalog stores Telegram source channel/message references and Telegram file IDs. Media is copied or reused server-side by Telegram; Railway does not download movie files.

## Critical operating rules

1. Run exactly **one Railway replica**. There is no external distributed lock.
2. Never manually pin another message in either database channel. The bot manifest must remain the most recent pinned message.
3. Do not delete the pinned manifest or current/previous snapshot documents.
4. Keep database and storage channels private.
5. Keep storage-channel “Restrict Saving Content” disabled so old posts can be forwarded to the owner for `/index`. Delivered files are protected separately by the bot.
6. Add and start the bot before bulk uploading. Telegram only retains undelivered updates for a limited period.
7. Rapid bursts of up to 100 valid files are coalesced transactionally. Let Telegram finish forwarding each burst; the next two-minute burst may begin normally. `/health` exposes `pending_catalog_ingest`, `catalog_files`, and current/maximum compressed catalog snapshot bytes for monitoring.
8. Do not place real bot/GitHub/Railway tokens in source control or send them in chat.
9. Owner catalog-title removal is irreversible. Verify the title and file count on both confirmation screens before deleting.
10. Keep **Threaded Mode disabled** for normal operation. When upgrading from the schema-v6 topic release, leave it enabled only until the new deployment retires the legacy topics, then disable it in BotFather. Application code cannot change this setting.

## Required channel permissions

### File and User Database channels

Add the bot as administrator with:

- Post Messages
- Edit Messages
- Delete Messages (for old snapshot cleanup)

The bot also pins its manifest message.

### Category storage channels

Add the bot as an administrator before registering the channel, with **Delete Messages** permission. The application verifies privacy, administrator status, and deletion permission for newly registered or changed channels. Existing registered channels must also be updated with Delete Messages permission before using permanent title removal.

## Environment configuration

Copy `.env.example` to `.env` for local configuration. Railway should use Variables instead of an uploaded `.env` file.

```env
BOT_TOKEN=123456:replace_me
OWNER_IDS=123456789
FILE_DATABASE_CHANNEL_ID=-1001111111111
USER_DATABASE_CHANNEL_ID=-1002222222222
WEBHOOK_BASE_URL=https://your-service.up.railway.app
WEBHOOK_PATH=/telegram/webhook
WEBHOOK_SECRET_TOKEN=replace_with_a_long_random_secret
HOST=0.0.0.0
PORT=8080
LOG_LEVEL=INFO
PROTECT_DELIVERED_CONTENT=true
```

`RAILWAY_PUBLIC_DOMAIN` can replace `WEBHOOK_BASE_URL`; the app derives an HTTPS URL automatically.

## First deployment

1. Create the bot through BotFather and make sure **Threaded Mode is disabled** in its bot settings.
2. Create two private database channels.
3. Add the bot to both database channels with the required permissions.
4. Configure all Railway variables.
5. Deploy with one replica.
6. Wait for `/health` to report `status: ok`, `delivery_mode: flat_chat`, `threaded_mode_enabled: false`, and `pending_legacy_topics: 0`. A `true` threaded-mode value does not block delivery, but the owner must disable it in BotFather to restore the intended normal-chat UI.
7. Open the bot privately and send `/start`.
8. Add the bot as administrator to the Movies storage channel.
9. Open Dashboard → Admin Control Center → Categories → Add category, then submit `Movies` and its private channel ID.
10. Repeat for `Series` and its private channel ID.

The bot infers `Movies` as single-title and `Series` as episodic. Unknown category names default to mixed and can be changed through Admin Control Center → Categories → Change mode.

Only begin bulk uploads after the category registration is committed.

### Upgrading from the topic-delivery release

1. Deploy this flat-chat release while the existing BotFather Threaded Mode setting is still enabled.
2. Check startup logs for the legacy-topic retirement count. Any failed deletions retain their snapshot references and retry on the next restart; resolve permissions/transient Telegram failures before proceeding.
3. Remember that deleting a Telegram topic also deletes every delivered copy inside it. The catalog source posts are unchanged and remain requestable.
4. Inspect the topic bar once. The schema-v6 release discarded the ID of its renamed `🗃 Previous Deliveries` topic after archiving it, and the Bot API cannot enumerate that unreferenced topic; if it is still visible, delete it manually.
5. After automatic retirement and that one-time check complete, disable **Threaded Mode** in BotFather.
6. Confirm `/health` shows `delivery_mode: flat_chat`, `threaded_mode_enabled: false`, and `pending_legacy_topics: 0`, then request one real file to smoke-test the permanent-file → fresh-pinned-dashboard sequence.

## Adding future categories

Use Dashboard → Admin Control Center → Categories → Add category. Send the category name and private channel ID when prompted.

The bot verifies privacy/admin access, generates the category ID and slug, writes a catalog revision, and immediately updates browse, search, watchlists, statistics, and channel routing. No restart is needed.

Disabling a category preserves existing files and watchlists. Changing its active channel preserves the old channel as a legacy source; keep the bot in that old channel while old files are still needed.

## Importing a missed or older post

1. Forward the original storage-channel video/document to the bot's private chat.
2. Reply to that forwarded message with:

   ```text
   /index
   ```

The forward must retain `MessageOriginChannel`. If it does not, check storage-channel content protection. The source channel must already be registered.

## Caption examples

Supported series patterns include:

```text
Maamla Legal Hai S02E01 1080p Hindi WEB DL 5 1 ESub x264 Mov mkv
📁 LOST S02-E19 720p x265 Esubs mkv
@UHDPrime Game of Thrones S03 E01 BluRay 720p Hindi 2 0 English mkv
Name: Operation Safed Sagar The Highest Air Force Mission S01E01 1 mkv
Game.Of.Thrones.S01.720p.10Bit.BluRay.Hindi-English.HEVC.x265.zip.zip.001
Doraemon S01E25 [RareToonsIndia].mkv
Doraemon.S18.Ep.1to15.Combined.Multi.Audio+Hindi.mkv
Doraemon.S18.Ep31To40.Hindi+Multi.Audio.mkv
```

The last two examples are combined Season Pack files and are displayed as `Episodes 1–15` and `Episodes 31–40`, not as individual Episode 1/Episode 31 records or as one complete archive.

A filename-style movie can look like:

```text
Interstellar.2014.1080p.Hindi-English.WEB-DL.x265.mkv
```

Labeled captions are also accepted:

```text
Title: Dune Part Two
Year: 2024
Language: Hindi, English
Quality: 2160p
```

Movie-header and numbered-movie captions are also accepted:

```text
MOVIES.IN:
Stand by Me Doraemon 2 (2020) 720p HEVC [Hindi-Eng-Jap].mkv

Movie No.37: Doraemon Nobita Little Star War Movie
Quality: 1080P FHD
Language: Hindi Dubbed
```

When one Telegram caption contains promotional text or several media filenames, a descriptive attached filename (for example, one containing `S01E25`, a year, or a quality token) is authoritative. This prevents an unrelated filename or metadata block in the caption from being indexed. Ordinary single-media captions and generic attachment names retain the existing caption fallback.

Missing year/language/quality is indexed as Unknown. A title is always required. Episodic categories also require a season; the record must contain an episode or be identifiable as a season pack.

Codec, source, bit depth, subtitle, uploader, warning, link, and emoji text is ignored and is not stored as searchable metadata. Delivery captions are generated from the extracted allowlist.

## User interaction

Users normally do not need a search command:

```text
Dark
```

The bot renders ranked title buttons in one temporary private-chat workspace and deletes the typed query once the result or validation screen is ready. Selecting a series opens seasons and then available episodes. One episode variant is delivered immediately; multiple variants display language/quality buttons. Movies show a Get File button or version buttons. Incoming messages with a Telegram topic/thread identifier are ignored as title searches during legacy migration.

Telegram onboarding through `/start` creates or recovers one dashboard message and asks Telegram to pin it. Dashboard buttons create or reuse one activity card. While that workspace contains interactive Search, Browse, Watchlist, or admin controls, every callback or user-message interaction resets a five-minute inactivity timer; five quiet minutes removes it. A successful delivery closes and deletes the active workspace, leaves the delivered file permanent, posts and pins a fresh dashboard immediately below it, and then retires the prior dashboard. No persistent delivery receipt remains. If the dashboard is lost or buried, `/dashboard` remains the backup command for posting and pinning a replacement.

Search, Browse, Recently Added, and all title/file detail screens are delivery-only: there are no Watchlist checkboxes or mutation actions. Historical `px:`, `pa:`, and `pw:` discovery callbacks only show an expiry notice and cannot modify a Watchlist. Adding manual or indexed titles is exclusive to Dashboard → Watchlist.

Watchlist → Add a title → Choose from the library opens each dynamic collection as a complete alphabetical catalog. Titles are paginated, both the checkbox and adjacent title button toggle selection, and selections survive page/filter changes. Power tools provide in-category title search, unsaved-only filtering, A–Z/`#` jumping, Select Page, Clear Selection, and selected-only review. A `📚` marker identifies titles already saved before a bulk status update.

Movies, episodes, videos, documents, and every season-pack part are copied directly into the ordinary private-chat timeline, regardless of catalog category or Telegram file format. No delivery path creates, renames, reopens, recovers, or routes through a native topic. During startup migration, the bot deletes every persisted legacy delivery/category topic. Telegram topic deletion also deletes the copies inside those topics; the catalog's private source posts remain intact, so users can request the files again. A topic reference is cleared only after Telegram confirms deletion or reports that the topic is already unavailable. Other failures retain the reference for a later startup retry.

Each delivered file contains only the protected media and a premium metadata caption—no inline action keyboard. Delivered files are never registered as temporary UI and are not automatically deleted. The successful-delivery sequence removes the callback source/workspace, posts a fresh dashboard after the file, pins it, and retires the old dashboard. The steady-state timeline therefore contains permanent delivered files and exactly one current live dashboard, with no delivery receipt.

The owner's pinned card is the same user dashboard with one additional Admin Control Center entry. Owner authorization is still enforced by the handler, not merely by hiding the button. The dashboard is the final user-facing interaction system; old native user and admin commands are no longer registered or handled.

### Owner broadcast

Open Dashboard → Admin Control Center → Broadcast. Send/forward one text, photo, video, or document, or reply to an existing supported message with a short trigger message. The confirmation card displays the source type, a safe text/caption preview, and the exact current count from stored user profiles. One explicit confirmation starts delivery.

The bot copies the source message without action buttons to every stored profile, including pending, suspended, and banned profiles. Transient network/server/flood-control errors receive bounded retries; permanently unreachable accounts are reported as failures. Only successful recipients receive a fresh pinned dashboard below the announcement. All prepared dashboard references are persisted in one atomic users-snapshot commit before old dashboards are retired; a failed commit rolls back the fresh cards and preserves the tracked old dashboards. The final owner report and catalog audit include message and dashboard sent/failed totals.

The native command menu contains only the emergency recovery command:

```text
/dashboard
```

Telegram's standard `/start` entry and content deep links remain supported but are hidden from the command menu. Reply-based owner `/index` also remains hidden as an operational exception for missed storage posts.

## Watchlist panel and sharing

Open Dashboard → Watchlist. The panel supports:

1. **From catalog** — choose any enabled dynamic category; search/filter or browse alphabetically; jump by initial; select a page, clear, or review ticks; and assign one status to up to 25 selected titles.
2. **Custom batch** — choose a dynamic category, paste up to 25 titles (one per line, 160 characters each), review/tick the normalized deduplicated preview, and choose one status for one atomic commit.
3. **My titles** — every list card shows its explicit To Watch, On Hold, or Completed status and category before the title; open an entry to change status, remove it, or open its linked catalog page.
4. **Community lists** — browse other active users' Watchlists read-only with the same per-title status and category labels. Shared viewers cannot change or remove another user's entries.
5. **Community name** — choose a display name up to 40 characters without changing the Telegram profile; the real `@username` remains visible in the directory when available. Owners can edit or reset any user's Community name from Admin Control Center → Users.

Community Watchlists are always public to other active bot users and cannot be made private. The previous privacy callback remains safely handled for old cards but rejects private-mode requests. A catalog title removed by the owner remains as a text-only Watchlist entry; catalog removal does not edit personal Watchlists.

## Permanent catalog-title removal

The owner can open Admin Panel → Catalog → Remove a title. The flow shows the title and complete file count, then requires a second irreversible confirmation. The bot first commits removal from file delivery and creates source tombstones. It then attempts to delete every associated Telegram source-channel post.

Telegram's Bot API normally refuses deletion more than 48 hours after a message was sent. If permission, age, or another Telegram error prevents deletion, the title remains unavailable and its old posts cannot automatically re-index. Admin Panel → Catalog displays **Pending source deletions**, with actions to retry or—after the owner manually deletes old channel posts—confirm manual cleanup. Watchlist entries are intentionally unaffected.

## Access modes

Admin Panel → Access supports:

- `public`: new users become active automatically
- `approval`: new users remain pending until approved
- `allowlist`: only owner-approved users can use the catalog

Changing modes does not remove bans or suspensions. The environment-configured owner cannot be restricted through the bot.

## Snapshot commit protocol

Every state change is write-through:

1. Copy current state in memory.
2. Apply and validate the mutation.
3. Increment the revision.
4. Upload a deterministic gzip JSON snapshot.
5. Edit the pinned manifest to point to the new file ID/checksum.
6. Only then replace in-memory state and confirm success.

A crash before step 5 leaves the previous manifest authoritative. Startup verifies SHA-256 and falls back to the previous snapshot if the current document is corrupt.

Rapid channel-post updates wait briefly in memory so concurrent files can enter one catalog mutation and one snapshot. The webhook remains synchronous: each channel update is acknowledged only after its whole batch commits, and a failed commit rejects every member so Telegram can retry without a partial in-memory catalog. Individual audit events remain in the snapshot, while one compact human-readable card represents the burst in the File Database channel.

The public Bot API limits downloaded files, so the application refuses to let a compressed database snapshot approach that limit. For the intended 40–50 users this should remain far below the threshold.

## Development and verification

Use Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check app tests
ruff format --check app tests
python -m pytest -q -W error
pip-audit -r requirements.txt
bandit -q -r app
```

The test suite covers the supplied real caption formats, `MOVIES.IN`, numbered movie captions, mixed-caption attachment authority, ordinary episodes, combined episode ranges, split archives, catalog schema-v3 migration, one-commit 100-file burst ingestion, rollback/retry after a failed burst snapshot, 18-season navigation and 55-episode pagination, metadata allowlisting, dynamic categories, duplicate update idempotency, search ordering, range-aware Season Pack UI, series grouping, exact Watchlist statuses, snapshot rollback/fallback, Telegram callback-size limits, pinned-dashboard recovery, concurrent workspace reuse, destructive legacy-topic cleanup with failure-safe reference retention, direct flat-chat copy and Telegram file-ID fallback, button-free delivery captions, dashboard replacement and rollback, broadcast owner authorization/recipient inclusion/retry/partial failure/atomic dashboard persistence, permanent delivered-file retention across repeated deliveries, typed-query cleanup, topic-message exclusion during migration, dedicated Watchlist checkbox state across pages and alphabet filters, stale discovery callback retirement, editable community names, always-public enforcement, and atomic bulk Watchlist rollback.

Run the webhook application:

```bash
python -m app
```

A valid HTTPS `WEBHOOK_BASE_URL` is required. The production deployment uses Railway's public HTTPS domain and `/health` endpoint.

## Known Telegram constraints

- Flat-chat delivery is independent of BotFather **Threaded Mode**, but the intended normal Telegram private-chat UI requires Threaded Mode to be disabled manually. Startup logs and `/health` expose the setting so an accidental enabled state is visible.
- The standard Bot API cannot scan arbitrary old channel history.
- Updates missed for too long must be imported with `/index` or through a future MTProto tool.
- Channel-post deletion events are not generally available. If both source copying and Telegram file-ID fallback fail, the bot marks the file unavailable and alerts the owner.
- Telegram protected content prevents normal forwarding/saving in official clients, but it is not absolute DRM against modified clients, capture hardware, or a second camera.
