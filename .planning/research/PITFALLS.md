# Pitfalls Research

**Domain:** macOS notification interception via SQLite database watching (Teams-specific)
**Researched:** 2026-02-11
**Confidence:** MEDIUM-HIGH (domain expertise from training data; no live verification of Sequoia-specific DB schema possible in this session)

## Critical Pitfalls

### Pitfall 1: SQLite Database Locking Causes Silent Data Loss

**What goes wrong:**
The macOS notification center daemon (`usernoted`) actively writes to the SQLite database. Opening the database with the default `sqlite3` connection mode and running queries can encounter `SQLITE_BUSY` or `SQLITE_LOCKED` errors. If these are not handled, the watcher silently misses notifications that arrived during the locked period. Worse, if the connection uses WAL mode improperly or holds read transactions open too long, it can interfere with `usernoted`'s writes, potentially causing macOS to stop writing notifications or to rotate/recreate the database file.

**Why it happens:**
Developers treat the notification database like their own application database. It is not -- it is owned by another process (`usernoted`). The watcher is a read-only parasite on someone else's database. Default SQLite behavior (journal_mode=delete) will try to take shared locks that conflict with `usernoted`'s WAL-mode writes. Even in WAL mode, a long-running read transaction prevents WAL checkpointing, eventually bloating the WAL file.

**How to avoid:**
- Open the database in read-only mode: `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`
- Set `PRAGMA journal_mode=wal` on the read connection (matches the DB's actual mode, avoids implicit mode conversion attempts)
- Never hold a transaction open longer than needed. Execute the query, fetch results, close immediately. Do not keep a cursor open between poll cycles.
- Set a busy timeout: `connection.execute("PRAGMA busy_timeout = 1000")` so transient locks are retried rather than errored
- Handle `sqlite3.OperationalError` (database is locked) gracefully -- log and retry on next poll cycle rather than crashing

**Warning signs:**
- Intermittent `sqlite3.OperationalError: database is locked` in logs
- Notifications appearing in macOS UI but not being captured by the watcher
- WAL file (`db-wal`) growing unboundedly on disk
- `usernoted` process using elevated CPU

**Phase to address:**
Phase 1 (Core DB Watcher) -- this is foundational. The database connection setup must be correct from the first working prototype.

---

### Pitfall 2: kqueue on WAL File Fires Unreliably or Misses Events

**What goes wrong:**
nchook uses `kqueue` with `KQ_FILTER_VNODE` and `KQ_NOTE_WRITE` on the WAL file (`db-wal`) to detect new notifications. This has several failure modes:
1. **WAL checkpoint collapses the WAL**: When `usernoted` checkpoints, data moves from WAL back to the main DB file. If the watcher only monitors the WAL file, it can miss notifications that were checkpointed before the next kqueue event.
2. **kqueue event coalescing**: Multiple rapid writes to the WAL produce a single kqueue event. If the handler processes only one notification per event, it misses the rest.
3. **WAL file recreation**: macOS can delete and recreate the WAL file (e.g., after a DB vacuum or usernoted restart). The kqueue file descriptor becomes stale -- it references the old (now deleted) inode. The watcher stops receiving events permanently with no error.
4. **File descriptor limits**: kqueue watches consume file descriptors. Not a practical limit for one DB, but matters if the approach is extended.

**Why it happens:**
kqueue watches file descriptors (inodes), not file paths. When the file is replaced, the FD points to nothing. Also, kqueue is a notification that "something changed," not "here's what changed" -- you must re-query the DB after every event, and you must query for ALL new records, not just one.

**How to avoid:**
- After every kqueue event, query for ALL `rec_id` values greater than the last processed `rec_id`, not just one row. Process the entire batch.
- Implement a fallback poll timer (e.g., every 5-10 seconds) that queries the DB regardless of kqueue events. This catches anything missed during WAL checkpoints or coalesced events.
- Detect WAL file replacement by periodically `stat()`-ing the WAL file and comparing the inode number. If it changes, re-register the kqueue watch on the new file descriptor.
- Alternatively, watch the directory containing the DB files for `KQ_NOTE_WRITE` as a secondary trigger.
- nchook watches only the WAL file -- verify this behavior in the fork. If nchook doesn't handle WAL recreation, patch it.

**Warning signs:**
- Watcher stops receiving events after running for hours/days (stale FD)
- Notifications arrive in macOS UI but watcher only picks them up on restart
- Burst of notifications arrives but only one is processed
- Watcher works fine for minutes but "stalls" periodically (checkpoint window)

**Phase to address:**
Phase 1 (Core DB Watcher) -- the watching mechanism is the heart of the system. The fallback poll timer should be implemented alongside kqueue from the start, not as an afterthought.

---

### Pitfall 3: Sequoia DB Path and Schema Divergence

**What goes wrong:**
macOS Sequoia (15.x) changed the notification center database path from the pre-Sequoia location. The PROJECT.md notes the Sequoia path as `~/Library/Group Containers/group.com.apple.usernoted/db2/db`. The original nchook hardcodes the pre-Sequoia path (`~/Library/Group Containers/group.com.apple.usernoted/db2/db` on some versions, or the older `~/Library/Application Support/NotificationCenter/` path on pre-Ventura). If the path detection is wrong, the daemon opens the wrong file or no file and silently produces zero notifications.

Beyond the path, the DB schema itself can change between macOS versions. Column names, table names, and the structure of the binary plist stored in the `data` column may differ. Sequoia may have added, renamed, or reorganized columns. Code that assumes specific column names will break on schema changes.

**Why it happens:**
Apple does not document or guarantee the notification center database schema. It is a private implementation detail. Each major macOS version can change it without notice. Developers test on one version and assume stability.

**How to avoid:**
- On startup, detect the macOS version (`platform.mac_ver()`) and select the DB path accordingly
- Validate the DB path exists before starting the watcher. Fail loudly with a clear error message if the file is not found.
- On startup, introspect the actual schema: `PRAGMA table_info(record)` (or whatever the table is named). Log the schema. Compare against expected columns and warn if unexpected columns are found or expected columns are missing.
- Do not hardcode column indices. Use column names from the query or use `row_factory = sqlite3.Row` for dict-like access.
- Add a startup self-test: query the DB, parse one notification, verify the expected fields (app, title, subtitle, body) are extractable. If not, fail with a diagnostic message.

**Warning signs:**
- Zero notifications captured despite Teams notifications visibly arriving
- `sqlite3.OperationalError: no such table` or `no such column` on startup
- Binary plist parsing returns empty dicts or unexpected structure
- The daemon starts without errors but webhook is never called

**Phase to address:**
Phase 1 (Core DB Watcher) -- path detection and schema validation must be the first thing implemented. A startup self-test should be part of the MVP.

---

### Pitfall 4: Binary Plist Parsing Fragility

**What goes wrong:**
The notification data is stored as a binary plist (bplist) in a BLOB column. Parsing this correctly is the most fragile part of the system. Failure modes include:
1. **Nested structure assumptions**: The plist may contain nested dicts/arrays. Assuming `data["title"]` when it's actually `data["req"]["titl"]` or some other nested path silently returns `None`.
2. **Key name changes**: Apple uses abbreviated keys (`titl`, `subt`, `body`, `app` or similar). These abbreviations can change between macOS versions.
3. **Multiple plist formats**: Some records may use XML plist instead of binary plist. `plistlib.loads()` handles both, but if code assumes binary-only and uses a different parser, XML records break.
4. **Encoding edge cases**: Notification bodies can contain emoji, Unicode characters, RTL text, and even null bytes in malformed plists. String handling must be UTF-8 safe.
5. **Missing keys**: Not all notifications have all fields. A "call" notification has no body. A system notification has no subtitle. Accessing missing keys without `.get()` crashes the parser.

**Why it happens:**
Developers dump one notification's plist, see the structure, hardcode paths to fields, and assume all notifications match. They don't -- different apps, different notification types, and different macOS versions produce different structures.

**How to avoid:**
- Use `plistlib.loads()` from the standard library -- it handles both binary and XML plists
- Always use `.get()` with defaults: `data.get("titl", "")` not `data["titl"]`
- Log the raw plist structure (at DEBUG level) for notifications that fail to parse, so you can diagnose format changes
- Build the parser to be defensive: if title/body/subtitle extraction fails, skip the notification and log it rather than crashing
- Create test fixtures with real notification plists from Sequoia for regression testing
- After extracting the plist dict, normalize keys to a standard internal format before passing downstream

**Warning signs:**
- `KeyError` or `TypeError` exceptions in the plist parsing code
- Some notifications parse correctly but others silently produce empty fields
- After macOS update, parser starts returning empty dicts
- Emoji-heavy messages produce truncated or garbled output

**Phase to address:**
Phase 1 (Core DB Watcher) for initial parsing, but Phase 2 (Teams Filtering) must validate that the parsed structure contains the expected Teams-specific fields. Create a dedicated parsing module with extensive error handling from the start.

---

### Pitfall 5: Teams Bundle ID Fragmentation

**What goes wrong:**
Microsoft Teams on macOS exists in multiple versions with different bundle identifiers:
- `com.microsoft.teams2` -- the "new" Teams (Electron-based, then later native)
- `com.microsoft.teams` -- classic/legacy Teams
- Potentially others: `com.microsoft.teams.nightly`, `com.microsoft.teams.beta`, or future bundle IDs if Microsoft rebundles again

Filtering on a single bundle ID misses notifications from the other version. Users may have both installed, or may be migrated from classic to new Teams without realizing it. The wrapper script must match ALL known Teams bundle IDs.

Additionally, the bundle ID is stored in the notification database record, but the exact column/field name and format may vary. It might be stored as part of the binary plist, or in a separate column like `app_id` or `bundleid`.

**Why it happens:**
Microsoft's Teams product has been through multiple rebrandings and architectural changes. The classic-to-new migration left fragmented bundle IDs. Developers test with whatever version they have installed and miss the other.

**How to avoid:**
- Filter with a set/list of bundle IDs, not a single string: `{"com.microsoft.teams", "com.microsoft.teams2"}`
- Make the bundle ID list configurable in the JSON config file so users can add future bundle IDs without code changes
- On startup, log which bundle IDs are being watched
- Periodically (or on first run), scan the DB for distinct app identifiers that contain "teams" (case-insensitive) to discover unexpected bundle IDs

**Warning signs:**
- Notifications from one Teams version are captured but not the other
- User reports "it stopped working" after a Teams update (bundle ID changed)
- DB scan shows Teams notifications with a bundle ID not in the filter list

**Phase to address:**
Phase 2 (Teams Filtering) -- bundle ID handling is core to the filtering logic. The configurable list should be established in Phase 1's config file structure.

---

### Pitfall 6: Foreground Suppression Creates Invisible Gaps

**What goes wrong:**
When a Teams chat or channel is actively focused (in the foreground), macOS and/or Teams suppress notifications for that conversation. Messages arrive in Teams but no notification is generated, so no record appears in the notification center database. The watcher has zero visibility into these messages. This creates gaps in the captured message stream that are invisible -- there's no "notification was suppressed" record, just silence.

Users expect the system to capture "all messages" and are confused when messages from their active conversations are missing.

**Why it happens:**
This is by design -- macOS and Teams both suppress notifications for the active/focused conversation to avoid redundant alerts. The notification center database only contains notifications that were actually generated, not all messages.

**How to avoid:**
- Document this limitation prominently. This is not a bug to fix but a fundamental constraint of the DB-watching approach.
- In the webhook JSON payload, include a `"notice": "Messages from active/focused Teams chats are not captured"` or similar field so downstream consumers are aware.
- Consider adding a `coverage_gap` boolean or similar metadata to indicate when a long silence from a previously active sender *might* indicate foreground suppression (heuristic only, not reliable).
- The PROJECT.md already lists this as a known limitation -- ensure it's surfaced to end users of the webhook, not just developers.

**Warning signs:**
- Users report missing messages that they can see in Teams but that never appeared in the webhook
- Long gaps in notifications from a conversation that then suddenly resume (user switched away from that chat)
- "Completeness" testing shows missed messages that correlate with the user's active Teams window

**Phase to address:**
Phase 3 (Webhook Delivery / JSON formatting) -- the limitation should be documented in the webhook payload design. Phase 1 should document it in logs/startup output.

---

### Pitfall 7: rec_id State Persistence Race Conditions

**What goes wrong:**
nchook tracks processed notifications by `rec_id` to avoid duplicates. The project adds file-based persistence so this survives restarts. Several things go wrong:
1. **Crash between process and persist**: If the daemon processes a notification (sends webhook) but crashes before writing the rec_id to the state file, it will re-send that notification on restart (duplicate).
2. **Write-then-crash**: If the daemon writes the rec_id to the state file but the webhook POST hasn't completed yet, the notification is marked as processed but never actually delivered (lost).
3. **State file corruption**: Writing JSON/text state files is not atomic. A crash mid-write produces a truncated/corrupt state file. On restart, the daemon either crashes or resets to zero (reprocessing everything).
4. **rec_id gaps**: Notification `rec_id` values in the SQLite DB may not be strictly sequential. If the daemon tracks "highest rec_id seen" instead of a set of processed rec_ids, gaps can cause it to skip unprocessed notifications with lower rec_ids that arrived out of order.

**Why it happens:**
File I/O is not atomic. Developers assume "write to file" is instant and reliable, but process crashes, disk full conditions, and OS buffering all create windows where state is inconsistent.

**How to avoid:**
- Use atomic file writes: write to a temp file, then `os.rename()` (atomic on the same filesystem) to the state file path. This prevents corruption.
- Accept the "at-least-once" delivery semantic: it's better to re-send a duplicate notification on restart than to lose one. Persist the rec_id AFTER successful webhook delivery, accepting that a crash in between means a duplicate.
- Track the highest `rec_id` rather than a set (simpler, and sufficient if rec_ids are monotonically increasing in the notification DB -- verify this assumption).
- If rec_ids are not monotonic, track a set of processed rec_ids, but cap the set size (e.g., last 10,000) and use a high-water mark to avoid unbounded memory growth.
- On startup, log the restored state (last rec_id or count of tracked IDs) so operators can verify correctness.

**Warning signs:**
- Duplicate webhook deliveries after daemon restart
- State file contains truncated JSON / is empty after a crash
- "Gap" notifications never processed because they had lower rec_ids than the high-water mark
- State file grows unboundedly over time

**Phase to address:**
Phase 1 (Core DB Watcher) for basic persistence, refined in Phase 2 (Teams Filtering) when webhook delivery ordering matters. Atomic writes should be implemented from the start.

---

### Pitfall 8: Full Disk Access (FDA) Permission Not Granted

**What goes wrong:**
macOS (Catalina and later) requires Full Disk Access (FDA) for processes that read files in `~/Library/Group Containers/` belonging to other apps. Without FDA, the Python process silently fails to open the notification database -- `sqlite3.connect()` may return an error, or worse, may succeed but return zero rows because the process doesn't have read access to the actual file content (sandboxing returns empty/permission-denied).

The failure mode is confusing: the daemon starts, appears to be running, but captures zero notifications. There is no obvious "permission denied" error because macOS sandbox violations are often silent.

**Why it happens:**
macOS privacy protections are aggressive and not well-documented for programmatic access. Developers test in environments where they've already granted FDA (from prior experiments) and forget it's required. New users hit this on first run with no clear error.

**How to avoid:**
- On startup, attempt to open and read the database. If zero records are returned or an error occurs, check for FDA explicitly. Print a clear message: "Full Disk Access required. Go to System Settings > Privacy & Security > Full Disk Access and add Terminal (or your terminal emulator)."
- Test the actual read: `SELECT COUNT(*) FROM record LIMIT 1` (or equivalent). If this returns an error or zero when the DB file exists and has nonzero size, it's likely a permission issue.
- Document the FDA requirement prominently in the README, including step-by-step instructions with screenshots for System Settings.
- Consider detecting the specific terminal app being used (`$TERM_PROGRAM`) and including it in the instruction message.

**Warning signs:**
- Daemon starts without errors but captures zero notifications
- `sqlite3.OperationalError: unable to open database file` on first run
- Database file exists (visible in Finder) but Python reports it as empty or unreadable
- Works in one terminal but not another (FDA is per-app, so Terminal.app may have it but iTerm2 may not)

**Phase to address:**
Phase 1 (Core DB Watcher) -- the very first thing the startup sequence should verify. This should block the daemon from running if FDA is not detected, rather than silently producing no results.

---

### Pitfall 9: Notification Cleanup / Purge Causes Negative rec_id Delta

**What goes wrong:**
macOS periodically purges old notifications from the database. `usernoted` may also vacuum or recreate the database. If the daemon's persisted "last rec_id" is higher than the maximum rec_id in the (now-purged) database, the daemon's query `WHERE rec_id > last_processed_rec_id` returns zero rows forever. The daemon appears to be running but captures nothing.

In extreme cases, after a DB recreation, rec_ids restart from 1. The daemon's persisted state says "I've processed up to rec_id 50000" and every new notification (rec_id 1, 2, 3...) is below the threshold and ignored.

**Why it happens:**
Developers test with a fresh database where rec_ids only increase. They don't test the scenario where the database is purged, vacuumed, or recreated by macOS, which resets the rec_id sequence.

**How to avoid:**
- On startup and periodically during operation, check `SELECT MAX(rec_id) FROM record`. If `MAX(rec_id) < last_processed_rec_id`, the database has been purged/recreated. Reset the high-water mark to 0 and log a warning.
- Alternatively, query `SELECT COUNT(*) FROM record WHERE rec_id > last_processed_rec_id`. If this is 0 but `SELECT COUNT(*) FROM record` is nonzero, something is wrong -- investigate.
- Store the database file's inode number or creation timestamp alongside the rec_id in the state file. If the inode changes, reset the state.
- Log the current `MAX(rec_id)` on startup so operators can compare against the persisted state.

**Warning signs:**
- Daemon running with no webhook calls despite new notifications arriving in macOS UI
- `MAX(rec_id)` in DB is lower than persisted `last_processed_rec_id`
- After system restart or macOS update, daemon stops capturing

**Phase to address:**
Phase 1 (Core DB Watcher) -- DB validity check must be part of the startup sequence and the periodic health check.

---

### Pitfall 10: Teams Notification Format Variations Break Allowlist

**What goes wrong:**
The allowlist strategy ("only forward notifications with both a sender and body present") seems clean but has edge cases:
1. **Group chat format**: Sender might be "Alice in GroupChatName" or the title might be the group name with the sender in the subtitle. The "sender" field contains different content depending on 1:1 vs group vs channel.
2. **Channel notifications**: For Teams channels, the title may be "ChannelName" and subtitle "SenderName," or vice versa, depending on Teams version and notification settings.
3. **Reactions that look like messages**: "Alice liked your message" has both a sender-like title and a body. The allowlist passes it through unless specifically filtered.
4. **Meeting notifications**: "Meeting starting in 5 minutes" or "Alice joined the meeting" have sender-like content but are not chat messages.
5. **Empty-looking bodies**: Some notifications have a body that's just whitespace or a zero-width character. `.strip()` catches whitespace but not all Unicode whitespace variants.

**Why it happens:**
Teams notifications are not a stable, documented API. They are user-facing strings that vary by Teams version, locale, notification settings, and conversation type. An allowlist based on field presence is a heuristic, not a contract.

**How to avoid:**
- In addition to field presence, add content-based filters: reject notifications where the title is exactly "Microsoft Teams" (system notifications), where the body matches known non-message patterns (reactions, calls, meetings)
- Build a blocklist of known non-message patterns alongside the allowlist: `["liked your message", "joined the meeting", "is calling", "left the meeting", "scheduled a meeting"]`
- Make both allowlist and blocklist configurable so users can tune without code changes
- Log all rejected notifications at DEBUG level so users can audit what's being filtered out
- Accept that some noise will get through and some messages will be missed. Perfect filtering is not achievable. Design downstream consumers to handle occasional noise.

**Warning signs:**
- Webhook receives reaction/call/meeting notifications that should have been filtered
- Legitimate messages from group chats are filtered out because the title format doesn't match expectations
- After a Teams update, filter starts passing through noise or blocking real messages

**Phase to address:**
Phase 2 (Teams Filtering) -- this is the core of that phase. Build the filter as a configurable pipeline, not hardcoded conditionals. Include logging for all filter decisions.

---

## Technical Debt Patterns

Shortcuts that seem reasonable but create long-term problems.

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Hardcoding the DB path | Faster initial development | Breaks on any macOS version change | Never -- use detection logic from day one |
| In-memory rec_id tracking only | No file I/O complexity | All state lost on restart, reprocesses entire DB | Only during initial development/testing, never in "production" |
| Single bundle ID filter | Simpler filter logic | Misses notifications from other Teams versions | Never -- use a configurable set |
| Polling-only (no kqueue) | Simpler, no FD management | Higher latency (seconds vs milliseconds), wastes CPU | Acceptable as fallback, not as primary mechanism |
| Shell-exec for webhook (curl) | Quick prototype | Process spawn overhead per notification, error handling is harder, shell injection risk | Only in prototype, replace with `requests`/`urllib` in Phase 1 |
| Printing to stdout instead of logging | No logging setup needed | No log levels, no file rotation, no structured output | Only during initial debugging |
| No startup validation | Faster to "just start" | Silent failures (wrong path, no FDA, wrong schema) | Never -- startup checks prevent hours of debugging |

## Integration Gotchas

Common mistakes when connecting to external services.

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Webhook HTTP POST | No timeout on the POST request, daemon hangs if endpoint is slow | Set a 5-10 second timeout on `requests.post()`. On timeout, log and skip (per project design). |
| Webhook HTTP POST | Not checking HTTP status code, assuming 2xx | Check `response.status_code`. Log non-2xx responses with the status code and response body. |
| Webhook HTTP POST | Sending notification data as form-encoded instead of JSON | Use `requests.post(url, json=payload)` not `requests.post(url, data=payload)` |
| SQLite connection | Keeping connection open permanently | Open, query, close per cycle. Or at minimum, use short-lived transactions. Long-lived connections to another process's DB are fragile. |
| SQLite connection | Not handling `OperationalError` for locked/busy states | Wrap all queries in try/except, handle gracefully |
| macOS file system | Assuming `~` expands in all contexts | Use `os.path.expanduser("~")` or `pathlib.Path.home()` explicitly |
| Config file | Not validating config on load (missing keys, wrong types) | Validate all required keys on startup, fail with clear error messages |

## Performance Traps

Patterns that work at small scale but fail as usage grows.

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Querying all records on every poll instead of using rec_id cursor | Slow queries, high CPU as DB grows | Always use `WHERE rec_id > last_id` with an index on rec_id | DB exceeds ~10K records |
| Storing all processed rec_ids in a Python set (unbounded) | Memory grows continuously | Use high-water mark or capped deque, not unbounded set | After weeks/months of operation (100K+ IDs) |
| Spawning subprocess (curl/script) per notification | High latency per notification, process creation overhead | Use in-process HTTP client (requests/urllib) | During notification bursts (10+ in seconds) |
| Synchronous webhook delivery blocking the poll loop | New notifications queue up while waiting for slow webhook | Fire-and-forget with timeout, or async delivery | When webhook endpoint is slow (>1s response time) |
| Reading entire DB file to detect changes instead of kqueue/polling | Massive I/O, wastes disk bandwidth | Use kqueue on WAL + fallback timer | Always -- DB can be 10-100MB |

## Security Mistakes

Domain-specific security issues beyond general web security.

| Mistake | Risk | Prevention |
|---------|------|------------|
| Logging full notification content at INFO level | PII (message content, sender names) written to log files accessible to other processes | Log full content at DEBUG only. Default logging to WARNING+. |
| Webhook URL in plaintext config file with world-readable permissions | Anyone on the machine can read the webhook URL and send spoofed data | Set file permissions to 0600 on config file. Document this in setup. |
| No TLS verification on webhook POST | MITM can intercept notification content | Use `verify=True` (default in requests). Do not add `verify=False` to "fix" cert errors. |
| Shell injection via notification content | If using subprocess/shell for webhook delivery, notification body could contain shell metacharacters | Never pass notification content through shell. Use in-process HTTP or proper argument escaping. |
| State file in world-readable location | Reveals what notifications have been processed (metadata leak) | State file permissions 0600, same as config. |
| Not sanitizing notification content before JSON serialization | Control characters in notification body can break JSON consumers | Use `json.dumps()` which handles escaping, but also strip null bytes and other control chars |

## UX Pitfalls

Common user experience mistakes in this domain.

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| No indication of what's being filtered out | User thinks messages are missing but they were just filtered | Log filter decisions at DEBUG; provide a `--verbose` flag that shows all notifications including filtered ones |
| Daemon starts silently with no confirmation | User doesn't know if it's working until the first notification arrives (could be minutes/hours) | Print startup summary: DB path, FDA status, bundle IDs watched, webhook URL, last processed rec_id |
| No health check mechanism | User has no way to verify the daemon is still alive and working | Add a periodic heartbeat log message ("Still watching, N notifications processed in last hour") |
| Cryptic error on FDA failure | User sees "unable to open database" and doesn't know what to do | Detect the specific failure and print actionable instructions for granting FDA |
| Webhook failures logged but not escalated | Operator doesn't notice the webhook has been failing for hours | After N consecutive webhook failures, print a WARNING-level message indicating persistent delivery issues |

## "Looks Done But Isn't" Checklist

Things that appear complete but are missing critical pieces.

- [ ] **DB watching:** Works in testing but no fallback poll timer -- will miss notifications during WAL checkpoints. Verify kqueue AND timer-based polling both trigger notification processing.
- [ ] **Plist parsing:** Parses 1:1 chat notifications but crashes on group/channel/call notifications with different plist structures. Test with at least 5 different notification types.
- [ ] **Startup:** Connects to DB successfully but doesn't verify FDA -- works on dev machine but fails silently on fresh installs. Add explicit FDA check.
- [ ] **State persistence:** Saves rec_id to file but doesn't use atomic writes -- state file will corrupt on crash. Verify with kill -9 during operation.
- [ ] **Filtering:** Allowlist passes all "real" messages but also passes reactions and meeting joins that have sender+body. Test with non-message notification types.
- [ ] **Webhook delivery:** POSTs successfully but has no timeout -- will hang forever if endpoint goes down. Verify behavior with a non-responsive endpoint.
- [ ] **Bundle ID filtering:** Matches `com.microsoft.teams2` but not `com.microsoft.teams` -- misses classic Teams users. Verify with both bundle IDs.
- [ ] **Config loading:** Reads config file but doesn't validate values -- empty webhook URL or missing keys cause cryptic errors later. Add validation on load.
- [ ] **rec_id tracking:** Tracks highest rec_id but doesn't handle DB purge/recreation -- daemon stops capturing after macOS cleans the DB. Test with a reset DB.
- [ ] **Error handling:** Happy path works but unhandled exception in notification processing kills the main loop. Wrap the per-notification processing in try/except.

## Recovery Strategies

When pitfalls occur despite prevention, how to recover.

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Database locking / corruption | LOW | Restart daemon. If DB is corrupted, restart usernoted (`killall usernoted`; macOS will restart it and recreate the DB). |
| Stale kqueue FD | LOW | Restart daemon. The kqueue watch will be re-established on the current WAL file. Consider adding auto-detection and re-registration. |
| Wrong DB path | LOW | Update path detection logic. Check actual path with `ls ~/Library/Group\ Containers/group.com.apple.usernoted/db2/`. |
| FDA not granted | LOW | Grant FDA in System Settings. Restart daemon. No data loss (missed notifications are gone, but future ones will be captured). |
| State file corruption | LOW | Delete state file. Daemon will reprocess existing notifications in DB (duplicates to webhook, but no data loss). |
| rec_id high-water mark above DB max | LOW | Delete or edit state file to reset rec_id to 0. Daemon will reprocess existing notifications. |
| Plist parsing failure on new macOS version | MEDIUM | Dump raw plist data from DB, examine new structure, update parsing logic. May require reverse-engineering the new format. |
| Bundle ID change after Teams update | LOW | Check DB for new bundle IDs, add to config. `SELECT DISTINCT app_id FROM record WHERE app_id LIKE '%teams%'`. |
| Webhook endpoint down | LOW | Fix endpoint. Missed notifications are lost (log-and-skip design). Check logs for what was missed. No way to replay. |
| Teams notification format change | MEDIUM | Dump recent notifications, compare against filter rules, update allowlist/blocklist patterns. |

## Pitfall-to-Phase Mapping

How roadmap phases should address these pitfalls.

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| SQLite DB locking | Phase 1: Core DB Watcher | Test with concurrent writes; verify no `SQLITE_BUSY` crashes |
| kqueue WAL unreliability | Phase 1: Core DB Watcher | Verify notifications captured during WAL checkpoint; test with burst of 10+ simultaneous notifications |
| Sequoia DB path divergence | Phase 1: Core DB Watcher | Startup prints detected path; test on Sequoia specifically |
| Binary plist parsing fragility | Phase 1: Core DB Watcher | Parse 5+ different notification types without error |
| Teams bundle ID fragmentation | Phase 2: Teams Filtering | Test with both `com.microsoft.teams` and `com.microsoft.teams2` |
| Foreground suppression gaps | Phase 3: Webhook/JSON | Document in webhook payload; mention in README |
| rec_id state persistence races | Phase 1: Core DB Watcher | Kill -9 during operation, verify restart behavior |
| FDA permission failure | Phase 1: Core DB Watcher | Test on fresh macOS user account without FDA pre-granted |
| DB purge / rec_id reset | Phase 1: Core DB Watcher | Simulate by deleting and recreating DB file |
| Teams format variations | Phase 2: Teams Filtering | Test with group chats, channels, reactions, calls, meetings |

## Sources

- macOS notification center database internals (training data, MEDIUM confidence -- Apple does not document this)
- SQLite WAL mode documentation (HIGH confidence -- well-documented by SQLite project)
- kqueue behavior with file replacement (HIGH confidence -- POSIX/BSD well-documented)
- nchook project architecture (MEDIUM confidence -- based on project description in PROJECT.md; source code not reviewed in this session)
- Teams notification format patterns (MEDIUM confidence -- based on general knowledge of Teams notification behavior; no live Sequoia DB inspection performed)
- macOS Full Disk Access requirements (HIGH confidence -- well-documented Apple privacy feature)
- Binary plist format (HIGH confidence -- `plistlib` is standard library, well-documented)

**Confidence note:** The most uncertain areas are (1) exact Sequoia DB schema column names and plist key names, and (2) current Teams notification format variations. These should be verified by inspecting a live Sequoia notification database during Phase 1 implementation.

---
*Pitfalls research for: macOS notification interception via SQLite database watching (Teams-specific)*
*Researched: 2026-02-11*
