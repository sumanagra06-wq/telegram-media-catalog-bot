# Telegram Media Catalog Bot

A private-channel media catalog for movies and series. Telegram stores both the media and the database snapshots; Railway runs only the application and does not need PostgreSQL, Redis, or a persistent disk.

## Implemented behaviour

- Dynamic private storage categories; no category channel IDs are hardcoded
- Automatic indexing of new and edited video/document channel posts
- Owner-assisted `/index` import for an older forwarded channel post
- Messy filename-style metadata parsing with tested support for:
  - `S02E01`, `S02-E19`, and `S03 E01`
  - 360p/480p/720p/1080p/2160p, FHD, UHD, and 4K
  - common language names and abbreviations
  - split season archives such as `.zip.001`, `.002`, `.003`
  - leading uploader mentions, emojis, technical codec text, and forwarding warnings
  - complete filename-style values after labels such as `Name:` or `Title:`
- Only title, year, language, quality, season, and episode/pack part are indexed from captions
- Exact, prefix, word, substring, and typo-tolerant title ranking
- Four search results per page
- Series navigation: title → season → episode → variant → protected file
- Season packs: title → season → Complete Season Pack → Part 1/2/3
- Movie navigation: title → language/quality variant → protected file
- Dedicated Watchlist panel with manual-title and existing-catalog add flows
- Dynamic category selection and exactly three statuses: To Watch, On Hold, Completed
- Always-public, read-only community watchlists with a user-editable community display name
- Owner-only, double-confirmed catalog-title removal that deletes every file record, attempts source-post deletion, and tracks required manual cleanup
- Removed source tombstones prevent edited posts from silently re-indexing; failed Telegram deletions remain retryable
- Public access initially; owner can switch to approval or allowlist
- Native scoped command menus plus the unchanged legacy inline interaction layer
- One recoverable pinned dashboard per user and one in-place temporary workspace below it
- Sliding five-minute workspace expiry with activity resets and restart cleanup
- Checkbox-style multi-selection across Search, Browse, and Recently Added, with atomic bulk Watchlist insertion
- One unified owner dashboard with an authorization-checked Admin Control Center entry
- Protected delivery through `protect_content=True`
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
└── Categories added later with /category_add

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
7. Do not place real bot/GitHub/Railway tokens in source control or send them in chat.
8. Owner catalog-title removal is irreversible. Verify the title and file count on both confirmation screens before deleting.

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

1. Create the bot through BotFather.
2. Create two private database channels.
3. Add the bot to both database channels with the required permissions.
4. Configure all Railway variables.
5. Deploy with one replica.
6. Wait for `/health` to report `status: ok`.
7. Open the bot privately and send `/start`.
8. Add the bot as administrator to the Movies storage channel.
9. Register it:

   ```text
   /category_add Movies | -1003333333333
   ```

10. Register Series:

    ```text
    /category_add Series | -1004444444444
    ```

The bot infers `Movies` as single-title and `Series` as episodic. Unknown category names default to mixed and can be changed through Admin Panel → Categories → Change mode.

Only begin bulk uploads after the category registration is committed.

## Adding future categories

```text
/category_add Anime | -1005555555555
```

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
```

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

Missing year/language/quality is indexed as Unknown. A title is always required. Episodic categories also require a season; the record must contain an episode or be identifiable as a season pack.

Codec, source, bit depth, subtitle, uploader, warning, link, and emoji text is ignored and is not stored as searchable metadata. Delivery captions are generated from the extracted allowlist.

## User interaction

Users normally do not need a search command:

```text
Dark
```

The bot sends ranked title buttons. Selecting a series opens seasons and then available episodes. One episode variant is delivered immediately; multiple variants display language/quality buttons. Movies show a Get File button or version buttons.

`/start` creates or recovers one dashboard message and asks Telegram to pin it in the private chat. Dashboard buttons create or reuse one temporary workspace message below it. Navigation edits that same workspace instead of adding another menu card. Every callback or user-message interaction resets a five-minute inactivity timer; after five quiet minutes the workspace is deleted. A restart removes or disables stale workspace cards while preserving the pinned dashboard reference.

Search, Browse, and Recently Added results show a separate `☐`/`✅` toggle beside each title. Selections remain checked across result pages. Users can select up to 25 titles, choose **Add Selected**, then assign To Watch, On Hold, or Completed to the whole selection in one atomic database update. The adjacent title button still opens normal details and protected delivery.

Watchlist → Add a title → Choose from the library now opens each dynamic collection as a complete alphabetical catalog. Titles are paginated, both the checkbox and adjacent title button toggle selection, an alphabet picker jumps directly to A–Z or `#`, and selections survive page/alphabet changes. A `📚` marker identifies titles already saved before a bulk status update.

After any successful movie, episode, variant, or season-pack delivery, the old temporary workspace is removed and a fresh dashboard card is posted below the delivered file. This keeps the controls at the bottom of the chat without creating two active workspaces.

The owner's pinned card is the same user dashboard with one additional Admin Control Center entry. Owner authorization is still enforced by the handler, not merely by hiding the button. Existing commands and legacy callbacks remain operational during the new-panel test period.

User native commands are intentionally short:

```text
/start
/menu
/watchlist
/help
/cancel
```

The owner receives additional scoped commands for administration.

## Watchlist panel and sharing

Open `/watchlist` or Main Menu → My watchlist. The panel supports:

1. **From catalog** — choose any enabled dynamic category, browse every indexed title alphabetically, jump by initial, tick one or more titles across pages, and assign one status to the selection.
2. **Manual title** — choose a dynamic category, type any title up to 160 characters, and choose one of the same three statuses.
3. **My titles** — view entries, change status, remove an entry, or open its catalog page when one is linked and still available.
4. **Community lists** — browse other active users' Watchlists read-only. Shared viewers cannot change or remove another user's entries.
5. **Community name** — choose a display name up to 40 characters without changing the Telegram profile; the real `@username` remains visible in the directory when available.

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

The test suite covers the supplied real caption formats, split archives, metadata allowlisting, dynamic categories, duplicate update idempotency, search ordering, series grouping, exact watchlist statuses, snapshot rollback/fallback, Telegram callback-size limits, pinned-dashboard recovery, concurrent workspace reuse, post-delivery control relocation, sliding expiry, restart cleanup, checkbox state across pages and alphabet filters, editable community names, always-public enforcement, owner authorization, and atomic bulk Watchlist rollback.

Run the webhook application:

```bash
python -m app
```

A valid HTTPS `WEBHOOK_BASE_URL` is required. The production deployment uses Railway's public HTTPS domain and `/health` endpoint.

## Known Telegram constraints

- The standard Bot API cannot scan arbitrary old channel history.
- Updates missed for too long must be imported with `/index` or through a future MTProto tool.
- Channel-post deletion events are not generally available. If both source copying and Telegram file-ID fallback fail, the bot marks the file unavailable and alerts the owner.
- Telegram protected content prevents normal forwarding/saving in official clients, but it is not absolute DRM against modified clients, capture hardware, or a second camera.
