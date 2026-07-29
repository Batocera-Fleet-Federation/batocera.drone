---
name: drone-smtp-notifications
description: Use this when designing, reviewing, debugging, or modifying the Drone's SMTP/Email admin tile — SMTP/IMAP configuration, peer-to-peer email-credential sharing (mirrors VPN sharing), the Test Email button, the ~5-minute activity-digest poller, the relational audit_log/notifications SQLite tables, the 10 notification-type toggles, the notifications bell/dropdown reachable from the top-left drone icon, or app/device/smtp_manager.py, app/device/notifications.py, app/storage/audit_store.py, web/handlers_smtp.py, web/handlers_notifications.py.
---

# Drone SMTP / Notifications Skill

## Goal

Keep this skill accurate as the single source of truth for the Email admin
tile and the notifications inbox — the parent `drone-admin-features` skill
only carries a brief tile-level summary and points here for depth.

This feature is a close mirror of `drone-vpn-management`'s design (opt-in P2P
sharing, single-hop provenance, a revocation poller, default-on
swarm-bootstrap) — read that skill first if you haven't; this one only
explains where SMTP genuinely differs and why.

## Architecture

```text
app/device/
  smtp_manager.py    # the whole SMTP/IMAP feature: settings, sharing,
                      # provenance, revocation, bootstrap, send_mail (stdlib
                      # smtplib), Test Email, the digest poller -- mirrors
                      # vpn_manager.py's shape closely
  notifications.py    # EVENT_TYPES/EVENT_TYPE_LABELS (the 10 types) +
                      # record_event() -- a dependency-free "leaf" module
                      # (only imports storage/audit_store.py) so every layer
                      # of the app can call it with no import-cycle risk
app/storage/
  audit_store.py       # audit_log + notifications tables/indexes + all CRUD
                      # (insert, list-unsent-for-digest, mark-emailed,
                      # paginate/read/dismiss/clear the inbox, retention)
app/web/
  handlers_smtp.py     # admin route handlers (HandlersSmtpMixin); delegates
                      # all real logic to smtp_manager.py
  handlers_notifications.py  # admin route handlers (HandlersNotificationsMixin);
                      # thin pass-through to audit_store.py
  handlers_peer.py     # _handle_peer_smtp_config: the mTLS GET /peer/smtp/config
                      # peer-serving endpoint, delegates to smtp_manager.export_payload()
```

## Design: no separate on-disk credentials file, unlike VPN

VPN's config/credentials live in a dedicated `<install root>/vpn/` directory
(`auth.txt`, chmod 600) because OpenVPN's own `auth-user-pass <file>`
directive **requires** a file on disk in a specific format — that's an
external-tool constraint, not a security design choice. SMTP has no such
constraint (`smtplib.login(username, password)` takes plain Python strings),
so there is deliberately **no** `smtp_dir()`/no separate credentials file:
the entire settings dict, **including the password**, lives in one JSON blob
via the same `storage/state_store.py` `load_payload`/`save_payload`
mechanism VPN's whole state dict already uses (`SMTP_STATE_NAMESPACE =
"smtp_manager.json"`, one row in the shared `app_state` table). This repo has
no crypto library available either way (stdlib-only), so this is the same
plaintext-on-disk tradeoff VPN already makes — do not add a separate
credentials file "for consistency with VPN" without a concrete new
requirement forcing one.

## Settings shape: shared fields vs. local-only fields

`_load_state()`/`_save_state()` return one dict split into three groups —
getting this split wrong is the most likely way to introduce a real bug here:

- **Shared** (`_SHARED_FIELDS`, travel in `export_payload()`/
  `import_from_peer()`): `host, port, use_starttls, use_ssl, username,
  password, from_address, recipient_email, imap_host, imap_port,
  imap_use_ssl, imap_username, imap_password`. `recipient_email` — where
  test/digest mail is actually delivered, distinct from `username` (an
  SMTP-AUTH login, not necessarily a mailbox) — is inferred as a required
  field, not something the original feature ask spelled out by name.
- **Sharing/provenance** (mirrors `vpn_manager._load_state()` exactly):
  `sharing_enabled` (default `False`), `source_peer_id`, `source_peer_name`,
  `revoked_reason`, `revoked_at`.
- **Local-only, never shared**: `smtp_enabled` (master send switch, default
  `True`), `notify` (dict of `event_type -> bool`, all 10 keys from
  `notifications.EVENT_TYPES`, default `True` each), `last_test_result`,
  `last_test_at`, `last_digest_sent_at`, `last_digest_error`. These stay
  local because each drone runs its **own** digest poller against its
  **own** `audit_log` using its own (possibly-adopted) credentials — "which
  of *my* events get emailed" is inherently per-drone, unlike the connection
  settings themselves. `import_from_peer()` only ever writes `_SHARED_FIELDS`
  (via `update_settings()`, which ignores unknown keys) — a malicious or
  buggy peer payload that tries to smuggle `notify`/`smtp_enabled` values in
  is silently ignored, not applied.

IMAP fields are stored and shared alongside SMTP **but never consumed** —
nothing in this app reads a mailbox. They exist because the feature spec
asked for both to be configurable. Do not wire up mailbox-reading behavior
without the user asking for it specifically.

## Peer-to-peer sharing, provenance, revocation, bootstrap (mirrors VPN)

Same shape as `drone-vpn-management`'s "Peer-to-peer sharing" section — read
that for the full rationale (single-hop-only, why the second explicit gate
exists on top of pairing, etc.). The concrete differences:

- **Completeness is one flag, not two.** VPN splits `has_config` (an
  uploaded `.ovpn`) from `has_credentials` (typed separately) because
  they're genuinely two different user actions in two different forms. SMTP
  has one settings form covering everything (`update_settings()` validates
  `host`/`from_address`/`recipient_email` together), so there is only
  `has_config` — don't reintroduce a `has_credentials` split without a real
  reason; `check_sharing_revocation()`'s "is there anything to revoke" gate
  checks `source_peer_id and has_config`, not a credentials-specific flag.
- **`bootstrap_smtp_from_swarm()` has no "connected" gate.** VPN's
  swarm-bootstrap only adopts from a peer whose tunnel is *actually up right
  now* (`export_payload()`'s `"connected"` field) — the strongest signal the
  shared credentials genuinely work. SMTP has no persistent-connection
  concept to check, so any paired peer that is `sharing_enabled` and returns
  a payload with a non-empty `host` qualifies immediately; the first one
  found (in `local_network.paired_peers()`'s own order) wins. Per-peer
  failures (offline, not sharing, empty/malformed payload) are silently
  skipped, exactly like VPN.
- **`_revoke_local_credentials()` wipes `password`/`imap_password` only** —
  same as VPN leaving the `.ovpn`/`source_peer_id` in place, the rest of the
  config (host/port/from/recipient/…) survives a revocation, and
  `source_peer_id` is *never* cleared here (only a genuine fresh
  `update_settings()` call clears provenance) — clearing it during
  revocation would let a now-credential-less imported config pass the
  "is this self-owned" check and become shareable again.
- **`maybe_bootstrap_smtp()`** is the startup entry point (mirrors
  `vpn_manager.maybe_auto_connect()`'s bootstrap step), called once from
  `create_server()`, gated on `not _load_state(settings)["has_config"]` —
  never overrides an existing local configuration.
- **Revocation poller**: `run_sharing_revocation_poller()` — identical
  shape to VPN's, its own `DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS` env
  var (default 300s, floored at 30s), own `_SMTP_SHARING_POLLER_STARTED`
  guard flag in `create_server()`.

## The audit_log / notifications tables

Two tables (`storage/audit_store.py`), deliberately separate despite being
written together by `insert_event()`:

- **`audit_log`** — the permanent record. `emailed_at` (nullable) tracks
  digest inclusion. Feeds `smtp_manager.send_digest_if_needed()`. Never
  touched by a user clearing notifications from the bell dropdown.
- **`notifications`** — the UI-facing inbox. One row per audit event, linked
  via `audit_log_id` (`REFERENCES audit_log(id)`, declared for schema
  clarity — `PRAGMA foreign_keys=ON` is deliberately **not** enabled on the
  shared connection app-wide, since that's a broader change than this
  feature warrants). Its own `read_at` (nullable). Rows are **hard-deleted**
  when a user clears them — `delete_notification`/`clear_notifications`
  never touch `audit_log`, since that's the trail the email pipeline depends
  on. Don't collapse these into one table "for simplicity" — a user clearing
  their inbox must never silently erase not-yet-emailed history.

Indexes: `idx_audit_log_created_at`, `idx_audit_log_pending_email`
(`event_type, emailed_at` composite, backs the digest poller's `WHERE
emailed_at IS NULL AND event_type IN (...)` query), `idx_notifications_
created_at` (`DESC`, backs the newest-first inbox listing),
`idx_notifications_read_at`, `idx_notifications_audit_log_id`.

**Retention** (`prune_old_events()`, called once per digest-poller tick,
after marking rows emailed): deletes already-**emailed** `audit_log` rows
older than 180 days, capped at the most recent 5,000 emailed rows; deletes
already-**read** `notifications` rows older than 90 days. Never prunes an
unsent audit row or an unread notification regardless of age, on purpose —
losing not-yet-delivered/not-yet-seen data automatically would be a real
bug, not a hygiene feature.

Storage convention: `audit_store._open()` follows the exact same shape every
other `storage/*_store.py` module uses (`saves_store.py` is the cleanest
example) — `_open_state_database(path)` (the shared `state_store.py`
connection, same physical sqlite file as everything else) then this
module's own idempotent `_ensure_schema()`. No separate migration-file
system exists in this repo; schema changes are always additive
`CREATE TABLE/INDEX IF NOT EXISTS`.

## `notifications.record_event()`: the one call site every hook uses

`device/notifications.py` is a dependency-free "leaf" module (only imports
`storage/audit_store.py`) specifically so any layer — `device/`, `transfer/`,
`web/`, `roms/` — can call `record_event(settings, event_type, title,
message="", details=None)` with zero import-cycle risk. **It never raises**
(internally try/except + log) — a notifications bug must never break the
feature that triggered it (a VPN connect, a completed download, a torrent
finishing, …). Logging is **unconditional** for all 10 event types — the
per-type toggles the SMTP page exposes only filter what the digest poller
*emails*, they do not gate whether an event is recorded or shown in the bell
dropdown. Don't add a toggle check inside `record_event()` or at a call
site; that check belongs solely in `smtp_manager.send_digest_if_needed()`.

## The 10 event types and their hook sites

`notifications.EVENT_TYPES` (also `EVENT_TYPE_LABELS` for display — keep
both in sync, and keep `drone.js`'s `SMTP_EVENT_TYPES` array in sync by hand
too, since there's no shared-schema codegen between the stdlib backend and
the frontend):

| `event_type` | Fired from | Fires on |
|---|---|---|
| `vpn_connected` / `vpn_disconnected` | `device/vpn_manager.py`'s `status()` | A genuine transition, tracked via a new `last_status` state field compared against the freshly-computed `connection_state` on every call — **not** on repeated polls of an unchanged status (the admin UI polls `status()` every 3s). `vpn_disconnected` fires on any transition *away from* `"connected"` (to disconnected, error, or connecting), not only to `"disconnected"` specifically. |
| `swarm_peer_connected` | `transfer/local_network.py`'s `save_paired_peer()` | `is_new = peer_id not in peers`, computed before the peer map is overwritten. Single choke point for both genuine-pairing call sites (`handlers_peer.py`'s `_handle_peer_pair`, `peer_connectivity.py`'s `_local_pair_peer`) — does **not** fire on the 3 other callers that only refresh an already-paired peer's address/last_seen. |
| `asset_added` / `asset_removed` | `roms/rom_scanner.py`'s `_poll_rom_metadata_once()` | Once per **completed scan pass** (already single-flight-guarded via `_begin_rom_metadata_activity`), not per file. `_poll_rom_metadata_cache()`'s `stats` dict gained a `roms_added` key (`len(rom_updates.keys() - previous_keys)` — genuinely new keys, as opposed to an existing ROM that merely changed) alongside the pre-existing `stats["deleted"]`. Only fires when the respective count is truthy. |
| `manual_control_submitted` | `web/handlers_diagnostics.py`'s `_handle_admin_system_volume` / `_handle_admin_screen_mode_post` | Right after `_apply_audio_volume`/`_apply_screen_mode` succeed. |
| `automation_updated` | `web/handlers_system.py`'s 3 automation POST handlers | Right after each `_save_automation_config()` call. |
| `asset_uploaded` | `web/handlers_peer.py`'s `_stream_file()` | Right after `tracker.finish(upload_id, "completed")` — the one choke point shared by all 5 peer-serving asset types (rom/bios/movie/save/artwork all funnel through `_stream_file`). Note this call site has `self.settings`; `transfer/upload_tracker.py`'s `UploadTracker` class deliberately does **not** — it's a standalone singleton with no settings/repository dependency by design (see its own module docstring), so the hook lives in the caller, not inside `UploadTracker.finish()`. |
| `asset_downloaded` | `transfer/download_manager.py`'s `_run_job()` `finally:` block | On `terminal_activity.get("status") == "completed"`, **without** the pre-existing `asset_type == "rom"` restriction that a different, older action (`_kick_asset_metadata_sync_after_download`) is gated on right next to it — bios/saves/movies/artwork downloads must fire this too. |
| `torrent_completed` | `transfer/torrent_manager.py`'s `_apply_aria2_status_locked()` | Inside the two `if not entry.get("completed_at"):` blocks (there are two because aria2 can report completion via either its `"active"`+fully-downloaded branch or its own `"complete"` status) — **not** on the bare `entry["status"] = "complete"` assignment, which re-fires on every poll tick while a torrent stays complete/seeding. |

When adding a new hook site for a *new* event type in the future: add the
slug to `EVENT_TYPES`/`EVENT_TYPE_LABELS`, add it to `drone.js`'s
`SMTP_EVENT_TYPES`, and call `notifications.record_event()` at exactly the
point the real transition/action completes — never at a point that could
re-fire on an unrelated poll or retry.

## The digest poller: this codebase's "cron style job every ~5 minutes"

**There is no OS cron, systemd timer, or scheduler abstraction anywhere in
this repo** — confirmed by reading `service_bootstrap.sh` in full (its only
loops are process-supervision `while true; do ...; sleep N; done`, not
feature scheduling) and every existing "periodic" feature (VPN's
sharing-revocation/self-heal pollers, the ROM metadata scan, peer health
checks, automation checks). Every one of them is an in-process
`threading.Thread(daemon=True)` running `while True: time.sleep(interval);
...`, started exactly once from `create_server()` behind a module-level
`_STARTED` guard flag. `run_audit_email_digest_poller()` follows this exact
shape (`DRONE_AUDIT_EMAIL_INTERVAL_SECONDS`, default 300s, floored at 60s).
**Do not build a new scheduling abstraction for this or any future periodic
feature** — copy this pattern.

`send_digest_if_needed()` (called by the poller, and safe to call directly
for a manual/test trigger) is a no-op unless `smtp_enabled` **and**
`has_config` **and** at least one notification type is enabled **and**
`audit_store.list_unsent_events()` (filtered to the enabled types) returns
something. On success: composes one plain-text digest email (`_compose_digest`,
one line per item with its timestamp/label/title/message), sends it,
`audit_store.mark_events_emailed()` the included rows, updates
`last_digest_sent_at`. On failure: records `last_digest_error`, leaves the
rows unsent so the **next** poll tick retries them — never mark rows emailed
before `send_mail()` has actually succeeded. Like `vpn_manager.check_and_
self_heal`/`check_sharing_revocation`, this function **never raises** —
wrapped in its own outer try/except, intended to run unattended forever.

## Sending mail: stdlib `smtplib` only

`send_mail(settings, subject, body)` picks `smtplib.SMTP_SSL` vs `smtplib.SMTP`
based on `use_ssl`, calls `.starttls()` only when `use_starttls and not
use_ssl`, calls `.login()` only when both `username` and `password` are
non-empty (an open-relay/no-auth SMTP server is a legitimate configuration),
builds the message with `email.mime.text.MIMEText`, and raises
`SmtpSendError` (wrapping the underlying `OSError`/`smtplib.SMTPException`)
on any failure — callers decide how to surface it (a 502 to the admin UI for
Test Email, a log line + `last_digest_error` for the poller). Confirmed via
full-tree grep: zero pre-existing `smtplib`/`imaplib`/`email.*` usage
anywhere in this repo before this feature — there is no other precedent to
reconcile with.

## Frontend: SMTP page + the notifications bell

`drone.js` (`renderSmtpPage`/`renderSmtpLive`/`patchSmtpLive`/
`startSmtpAutoRefresh`) is a line-for-line structural mirror of the VPN
page's own live-refresh pattern (see `drone-admin-features`'s "Live-refreshing
tile pattern" section) — a `render*Live(payload)` skeleton built once with
stable per-region container ids, a `patch*Live(payload)` that only ever
`.innerHTML`s those same leaf nodes on a 5s poll (never re-renders the
settings form above it, so an in-progress edit survives a poll tick).

**This codebase's actual convention is plain inline `onclick="..."`
attributes, not a `data-*` delegated-action allowlist.** (A prior research
pass for this feature incorrectly asserted a `UI_ACTION_NAMES`/
`data-ui-click` system exists in `drone.js` — it does not; grep confirms zero
occurrences. `renderAdminMenu()`, the VPN page, and the Torrents upload
button all use plain `onclick=`, and the new SMTP/notifications code matches
that.) The one real, confirmed frontend gotcha to still watch for is the
`window.bootstrap` collision: never declare a top-level `function
bootstrap()`/`async function bootstrap()` in `drone.js` (the app's own init
entry point is deliberately named `bootstrapApp()` — see
`drone-admin-features`).

**The notifications bell reuses the previously-inert top-left mascot icon**
(`index.html`'s `.brand-mark` `<span>`, now `id="notificationsBellBtn"`) —
it had no id/click-handler at all before this feature; the adjacent
`#brandHomeBtn` text link already owned "go home" and is untouched. A
Bootstrap 5 dropdown (`data-bs-toggle="dropdown"` on the bell, a sibling
`.dropdown-menu`) handles show/hide — Bootstrap's bundle is already loaded
and used elsewhere (`window.bootstrap?.Modal`), so no new dependency. Two
independent refresh triggers, deliberately different cadences:

- **The unread-count badge** polls `GET /admin/notifications/unread-count`
  every 20s via `startNotificationsPoll()`/`refreshNotificationsUnreadCount()`,
  started once from `applyAdminVisibility()` (and stopped when
  `adminEnabled` is false, since `/admin/*` routes 403 without it) — runs
  globally on every page, not just while a specific tile is open, so it uses
  a lighter cadence than the 3s admin-tile polls.
- **The dropdown's actual item list** only refreshes when opened
  (`show.bs.dropdown` → `refreshNotificationsDropdown()`) — an inbox that's
  closed doesn't need 3s freshness, only the badge count does.

No separate paginated `#notifications` page exists — the dropdown shows the
most recent 20 (`GET /admin/notifications?limit=20`); `audit_store`'s
keyset pagination (`before_id`) is implemented and exposed via the API for a
future page if one is ever wanted, but nothing in the frontend calls it yet.

## Common failure patterns

- Putting `notify`/`smtp_enabled` in `_SHARED_FIELDS` (or reading them out of
  a peer-sourced payload during import) — these are local-only by design; see
  "Settings shape" above.
- Gating `record_event()` (or a call site) on the per-type `notify` toggle —
  logging must be unconditional; only `send_digest_if_needed()` filters by it.
- Marking `audit_log` rows emailed before `send_mail()` has actually
  succeeded — a send failure must leave them unsent so the next poll retries.
- Deleting an `audit_log` row (or cascading into one) when a user clears a
  notification — `notifications`/`audit_log` are intentionally decoupled;
  only `delete_notification`/`clear_notifications` touch the inbox table.
- Reviving a `has_credentials`-style split for SMTP "for consistency with
  VPN" — SMTP has one settings form and one `has_config` completeness flag;
  see "Peer-to-peer sharing" above for why that's deliberate, not an oversight.
- Adding a `"connected"`-style gate to `bootstrap_smtp_from_swarm()` "for
  consistency with VPN" — SMTP has no persistent-connection concept to check
  against; any sharing peer with a non-empty `host` qualifies.
- Clearing `source_peer_id` anywhere in `_revoke_local_credentials()` — that
  would let a now-credential-less imported config pass the "is this
  self-owned" check and become shareable again. Only a genuine fresh
  `update_settings()` call may clear provenance.
- Calling `_apply_screen_mode`/`_apply_audio_volume`/`_save_automation_config`/
  etc. and forgetting the paired `record_event()` call when adding a *new*
  manual-control or automation action — check the existing call sites in
  "The 10 event types" table above for the pattern to copy.
- Assuming `UploadTracker` has access to `self.settings` — it doesn't (see
  its own module docstring); the `asset_uploaded` hook lives in
  `handlers_peer.py`'s `_stream_file()`, the caller, not inside
  `UploadTracker.finish()`.
- Building a new scheduling abstraction (or reaching for a third-party cron
  library) for a future periodic feature — this repo has no OS cron
  anywhere; copy the `Thread(daemon=True)` + `time.sleep(interval)` +
  `_STARTED` guard-flag shape every existing poller (including this one)
  already uses.
- Trusting the `UI_ACTION_NAMES`/`data-ui-click` system described by an
  earlier (incorrect) research pass for this feature — it does not exist in
  `drone.js`; new UI code uses plain `onclick="..."` attributes, matching
  the VPN page and `renderAdminMenu()`.

## Expected output format

When completing SMTP/notifications work, respond using this format:

```text
Objective:
...
smtp_manager.py changes:
...
notifications.py / audit_store.py changes (schema, if applicable):
...
Hook-site changes (which event_type, which file/function):
...
Backend route + handler changes (api_routes.py + handlers_smtp.py/handlers_notifications.py):
...
Frontend changes (drone.js, index.html/drone.css for the bell):
...
Digest poller changes (if applicable):
...
Tests:
...
Risks:
...
Files changed:
...
```

## Safety rules

Do not:

- return the SMTP/IMAP password in any **`/admin/*`** (browser-facing) API
  response — `smtp_manager.get_settings()`'s `_sanitized()` strips it to
  `has_password`/`has_imap_password` booleans; the peer-to-peer
  `/peer/smtp/config` payload is the one intentional, narrowly-scoped
  exception, gated by pairing + `sharing_enabled`,
- let an imported SMTP config be re-shared — enforce single-hop-only in both
  `set_sharing_enabled` (reject) and `export_payload` (independently refuse),
  exactly like VPN,
- clear `source_peer_id` anywhere except a genuine fresh `update_settings()`
  call, especially not during revocation cleanup,
- treat a network error, timeout, or non-404 status as revocation — only an
  explicit 404 or "peer no longer paired" may disconnect and wipe credentials,
- mark an `audit_log` row `emailed_at` before the send actually succeeded,
- delete or mutate `audit_log` from any notifications-inbox action (read,
  dismiss, clear-all),
- gate `notifications.record_event()` itself on a notification-type toggle —
  only the digest poller's query may filter by it,
- add a background thread for this or any future periodic feature via
  anything other than the existing `Thread(daemon=True)` +
  `time.sleep(interval)` + module-level `_STARTED` guard-flag shape,
- add a per-request `PRAGMA foreign_keys=ON` or flip it on globally in
  `state_store.open_database()` as part of this feature — that's a broader,
  unrelated change; the `notifications.audit_log_id` FK is declared for
  schema clarity only.

## Default bias

When unsure, keep the VPN-mirrored shape (sharing/provenance/revocation/
bootstrap) rather than inventing a new pattern, keep the shared-vs-local-only
settings split exactly as documented above, keep `record_event()` calls
unconditional (never toggle-gated), keep the digest poller's "mark emailed
only after a real send success" invariant, and keep new periodic work on the
existing in-process-thread shape rather than introducing any new scheduling
mechanism.
