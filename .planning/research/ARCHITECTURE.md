# Architecture Research

**Domain:** macOS notification database interception daemon
**Researched:** 2026-02-11
**Confidence:** MEDIUM (nchook internals and DB schema based on training data; could not verify against live sources due to tool restrictions)

## Standard Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  macOS Notification Center (usernoted)                              │
│  Writes notification records to SQLite DB + WAL                     │
└──────────────┬──────────────────────────────────────────────────────┘
               │ (filesystem write to WAL file)
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  COMPONENT 1: Patched nchook (Python daemon)                        │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ kqueue       │  │ DB Reader    │  │ Plist Decoder            │  │
│  │ WAL Watcher  │→ │ (SQLite3)    │→ │ (binary plist blobs)     │  │
│  └──────────────┘  └──────────────┘  └──────────┬───────────────┘  │
│                                                  │                  │
│  ┌──────────────┐  ┌──────────────────────────┐  │                  │
│  │ State Tracker │  │ Callback Dispatcher      │←─┘                 │
│  │ (rec_ids)     │→ │ (subprocess call)        │                    │
│  └──────────────┘  └──────────┬───────────────┘                    │
└──────────────────────────────┼──────────────────────────────────────┘
                               │ subprocess.call(script, APP, TITLE,
                               │   BODY, TIME, SUBT)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  COMPONENT 2: Wrapper Script (shell or Python)                      │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ Teams Filter  │→ │ JSON Builder │→ │ Webhook Poster           │  │
│  │ (allowlist)   │  │              │  │ (HTTP POST)              │  │
│  └──────────────┘  └──────────────┘  └──────────┬───────────────┘  │
└──────────────────────────────────────────────────┼──────────────────┘
                                                   │ HTTP POST JSON
                                                   ▼
                                        ┌──────────────────────┐
                                        │  Webhook Endpoint    │
                                        │  (downstream agent)  │
                                        └──────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Typical Implementation |
|-----------|----------------|------------------------|
| **kqueue WAL Watcher** | Detect filesystem changes to the notification DB WAL file | Python `select.kqueue()` with `KQ_FILTER_VNODE` / `NOTE_WRITE` on the WAL file descriptor |
| **DB Reader** | Open the SQLite DB in read-only mode, query for new records | Python `sqlite3` with `?mode=ro` URI, `PRAGMA journal_mode` awareness |
| **Plist Decoder** | Extract human-readable fields from binary plist blobs stored in the `data` column | Python `plistlib.loads()` on the binary blob |
| **State Tracker** | Remember which `rec_id` values have been processed to avoid duplicates | In-memory set of integers + file-based persistence (JSON or newline-delimited) |
| **Callback Dispatcher** | Invoke the user-provided script with extracted notification fields as arguments | `subprocess.call([script, app, title, body, time, subt])` |
| **Teams Filter** | Accept only Teams bundle IDs, require sender+body present, reject noise | String matching on APP arg against `com.microsoft.teams2` / `com.microsoft.teams`, allowlist logic |
| **JSON Builder** | Format extracted fields into a structured JSON payload | Construct dict with sender, chat_name, body, timestamp, is_truncated flag |
| **Webhook Poster** | POST JSON to configured URL, log-and-skip on failure | `urllib.request` or `curl` with timeout and error handling |

## macOS Notification Center Database

### DB Location

**Sequoia (macOS 15+):**
```
~/Library/Group Containers/group.com.apple.usernoted/db2/db
```

**Pre-Sequoia (Ventura/Sonoma, macOS 13-14):**
```
~/Library/Group Containers/group.com.apple.usernoted/db2/db
```
Note: The path has been consistent in the `db2/` location since roughly macOS Ventura. Earlier macOS versions (Big Sur and before) used a different path under `$(getconf DARWIN_USER_DIR)/com.apple.notificationcenter/db2/db` or similar. The `group.com.apple.usernoted` group container path is the current standard.

**Confidence:** MEDIUM -- The Sequoia path in PROJECT.md matches known patterns. The exact path should be verified on target machine by checking if the file exists.

### WAL File

The SQLite database uses Write-Ahead Logging. The WAL file is at:
```
~/Library/Group Containers/group.com.apple.usernoted/db2/db-wal
```

This is the file monitored by kqueue. When macOS writes a new notification, the WAL file is modified first, which triggers the kqueue event.

### Database Schema (Reconstructed)

**Confidence:** MEDIUM -- Based on training data analysis of nchook source code and macOS notification center reverse engineering. Column names and types should be verified against the live database with `sqlite3 <path> ".schema"`.

The primary table is `record`:

```sql
CREATE TABLE record (
    rec_id    INTEGER PRIMARY KEY,
    app_id    INTEGER,          -- foreign key to app table
    uuid      BLOB,             -- unique notification identifier
    data      BLOB,             -- binary plist with notification content
    delivered_date REAL,        -- Core Data timestamp (seconds since 2001-01-01)
    presented INTEGER DEFAULT 0,
    style     INTEGER,
    snooze_fire_date REAL
);

CREATE TABLE app (
    app_id    INTEGER PRIMARY KEY,
    identifier TEXT              -- bundle ID, e.g. "com.microsoft.teams2"
);

-- Additional tables exist (categories, requests, etc.) but are not
-- relevant to notification interception.
```

**Key columns for this project:**

| Column | Table | Type | Purpose |
|--------|-------|------|---------|
| `rec_id` | record | INTEGER | Monotonically increasing primary key -- used as high-water mark for "new" detection |
| `app_id` | record | INTEGER | FK to `app.app_id` -- join to get bundle ID |
| `data` | record | BLOB | Binary plist containing the notification payload |
| `delivered_date` | record | REAL | Cocoa/Core Data epoch timestamp |
| `identifier` | app | TEXT | Bundle identifier string like `com.microsoft.teams2` |

### Binary Plist Payload Structure

The `data` BLOB is a binary plist (property list). When decoded, it produces a dictionary with keys including:

```python
{
    "app": "com.microsoft.teams2",     # Bundle ID (redundant with app table)
    "titl": "John Smith",              # Notification title -- sender name for Teams
    "subt": "Project Alpha Chat",      # Subtitle -- chat/channel name for Teams
    "body": "Hey, can you review...",  # Notification body -- message content
    "date": 728571234.567,             # Cocoa timestamp
    "req":  "some-request-id",         # Request identifier
    # ... additional keys vary by notification type
}
```

**Key plist fields for Teams notifications:**

| Plist Key | Meaning | Teams Usage |
|-----------|---------|-------------|
| `titl` | Title | Sender display name |
| `subt` | Subtitle | Chat name / channel name |
| `body` | Body | Message content (truncated to ~150 chars) |
| `date` | Timestamp | When notification was delivered |
| `app` | Bundle ID | `com.microsoft.teams2` or `com.microsoft.teams` |

**Confidence:** MEDIUM -- The plist keys `titl`, `subt`, `body` are well-documented in macOS notification center reverse engineering. The exact key names should be verified by decoding a real notification from the live database.

## Data Flow

### End-to-End Notification Flow

```
1. User receives Teams message
       │
       ▼
2. Teams app posts notification via NSUserNotificationCenter / UNUserNotificationCenter
       │
       ▼
3. macOS usernoted daemon writes record to SQLite DB
   (INSERT into record table, data = binary plist blob)
       │
       ▼
4. SQLite WAL file modified on disk
       │
       ▼
5. kqueue fires NOTE_WRITE event on WAL file descriptor
       │
       ▼
6. nchook wakes from kqueue wait
       │
       ▼
7. nchook queries: SELECT rec_id, data FROM record
   JOIN app ON record.app_id = app.app_id
   WHERE rec_id > last_seen_rec_id
       │
       ▼
8. For each new row:
   a. Decode binary plist from data column
   b. Extract app, titl, subt, body, date
   c. Update last_seen_rec_id (state tracker)
   d. Call wrapper script with args: APP TITLE BODY TIME SUBT
       │
       ▼
9. Wrapper script receives arguments
       │
       ▼
10. Filter check:
    - Is APP a Teams bundle ID? (com.microsoft.teams2 or com.microsoft.teams)
    - Is TITLE present and not "Microsoft Teams"? (reject system alerts)
    - Is BODY present and non-empty? (reject empty notifications)
    - Reject known noise patterns (reactions, calls, etc.)
       │
       ▼
11. Build JSON payload:
    {
      "sender": TITLE,
      "chat_name": SUBT,
      "body": BODY,
      "timestamp": TIME (ISO 8601),
      "is_truncated": len(BODY) >= 148,
      "source": "macos-notification"
    }
       │
       ▼
12. HTTP POST to webhook URL from config
       │
       ▼
13. Log result (success or failure), continue
```

### State Management Flow

```
Startup:
  1. Read state file (JSON with rec_ids set or last_rec_id integer)
  2. Load into memory as set/high-water-mark
  3. Begin kqueue monitoring

Per notification:
  1. Check rec_id against state
  2. Process if new
  3. Add rec_id to state
  4. Periodically flush state to disk (or flush per-notification)

Shutdown:
  1. Write final state to disk
  2. Close DB connection
  3. Close kqueue fd
```

## Recommended Project Structure

```
macos-notification-intercept/
├── nchook/                    # Patched nchook (forked, modified)
│   ├── nchook.py              # Main daemon: kqueue watcher + DB reader + plist decoder
│   └── README.md              # Fork notes: what was changed and why
├── handler.sh                 # Wrapper script called by nchook (or handler.py)
├── config.json                # User configuration
│   # {
│   #   "webhook_url": "https://...",
│   #   "bundle_ids": ["com.microsoft.teams2", "com.microsoft.teams"],
│   #   "log_level": "INFO"
│   # }
├── state.json                 # Persisted state (auto-generated, gitignored)
│   # { "last_rec_id": 12345 }
├── run.sh                     # Entry point: launches nchook with handler path
├── .gitignore                 # state.json, __pycache__, etc.
└── .planning/                 # Project planning (GSD)
    ├── PROJECT.md
    └── research/
```

### Structure Rationale

- **nchook/**: Isolated as a forked dependency. Changes to nchook are tracked separately. The patched file is self-contained Python with no external dependencies beyond stdlib.
- **handler.sh (or handler.py)**: The wrapper script is the primary custom code. Keeping it at root makes it easy to reference from run.sh. Shell script is simplest for the subprocess call pattern nchook uses; Python script is an option if JSON construction or filtering logic grows complex.
- **config.json**: User-editable configuration at project root. Read by handler script (not by nchook -- nchook only cares about DB path and handler path).
- **state.json**: Auto-generated, gitignored. Simple JSON with last_rec_id or a set of processed rec_ids.
- **run.sh**: Single entry point that configures and launches nchook pointing at handler.sh.

## Architectural Patterns

### Pattern 1: kqueue WAL File Watching

**What:** Instead of polling the SQLite database on a timer, use the kernel's kqueue mechanism to get notified when the WAL file changes. This gives near-instant detection of new notifications.

**When to use:** Always -- this is the standard approach for macOS notification DB watching. Polling would work but wastes CPU and introduces latency.

**Trade-offs:**
- Pro: Near-zero latency, near-zero CPU when idle
- Pro: No polling interval to tune
- Con: kqueue is macOS/BSD specific (not portable, but portability is not a concern here)
- Con: WAL file changes don't guarantee new records -- checkpoints and other writes also trigger events, so the reader must handle "no new records" gracefully

**Implementation sketch:**
```python
import select
import os

kq = select.kqueue()
wal_fd = os.open(wal_path, os.O_RDONLY)
event = select.kevent(
    wal_fd,
    filter=select.KQ_FILTER_VNODE,
    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
    fflags=select.KQ_NOTE_WRITE
)

while True:
    events = kq.control([event], 1, None)  # blocks until WAL written
    # Query DB for new records...
```

### Pattern 2: High-Water Mark State Tracking

**What:** Track the highest `rec_id` processed rather than a set of all processed IDs. Since `rec_id` is a monotonically increasing integer primary key, any `rec_id > last_seen` is new.

**When to use:** When the primary key is guaranteed monotonically increasing (which SQLite INTEGER PRIMARY KEY is, absent explicit reuse).

**Trade-offs:**
- Pro: O(1) storage, O(1) comparison
- Pro: State file is tiny (single integer)
- Con: If macOS ever reuses or resets rec_ids (e.g., DB wipe on upgrade), could miss or re-process
- Con: Cannot skip/exclude specific rec_ids without additional logic

**Why not a set of all rec_ids:** The notification DB can accumulate thousands of records. Storing all processed IDs wastes memory and disk. High-water mark is sufficient because the query is `WHERE rec_id > ?` which naturally excludes all previously seen records.

**Implementation sketch:**
```python
# Query for new notifications
cursor.execute("""
    SELECT r.rec_id, r.data, a.identifier, r.delivered_date
    FROM record r
    JOIN app a ON r.app_id = a.app_id
    WHERE r.rec_id > ?
    ORDER BY r.rec_id ASC
""", (last_rec_id,))

for row in cursor.fetchall():
    rec_id, data, bundle_id, delivered_date = row
    # process...
    last_rec_id = rec_id  # advance high-water mark

# Persist
with open("state.json", "w") as f:
    json.dump({"last_rec_id": last_rec_id}, f)
```

### Pattern 3: Subprocess Callback Dispatch

**What:** nchook invokes the handler as a subprocess with notification fields as positional arguments. This decouples nchook (generic notification watcher) from the handler (Teams-specific logic).

**When to use:** This is nchook's existing architecture. Maintaining it preserves the clean separation between "watch DB" and "do something with notification."

**Trade-offs:**
- Pro: Handler can be any language (shell, Python, etc.)
- Pro: Handler crash doesn't crash the watcher
- Pro: Easy to test handler independently
- Con: Subprocess overhead per notification (fork + exec)
- Con: Arguments limited by command-line length (not a real concern for notification text)
- Con: Special characters in notification text need proper escaping

**Argument convention (patched nchook):**
```
handler.sh APP TITLE BODY TIME SUBT
   $1  = "com.microsoft.teams2"
   $2  = "John Smith"              (sender name / titl)
   $3  = "Hey, can you review..."  (message body)
   $4  = "2026-02-11T14:30:00"     (timestamp)
   $5  = "Project Alpha Chat"      (chat/channel name / subt)
```

### Pattern 4: Read-Only SQLite Access with WAL Awareness

**What:** Open the notification database in read-only mode with `immutable=0` to avoid write locks while still reading WAL contents.

**When to use:** Always -- this is a system database owned by usernoted. Writing to it would be dangerous and unnecessary.

**Trade-offs:**
- Pro: Cannot corrupt the system database
- Pro: No lock contention with usernoted
- Con: Must handle SQLITE_BUSY if usernoted is checkpointing
- Con: Read-only connections may not see very recent WAL entries depending on timing

**Implementation considerations:**
```python
import sqlite3

# Open read-only
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

# Important: set WAL mode awareness
conn.execute("PRAGMA journal_mode")  # will return "wal"

# Query... (may need retry on SQLITE_BUSY)
```

## Anti-Patterns

### Anti-Pattern 1: Polling the Database on a Timer

**What people do:** Use `time.sleep(N)` in a loop and re-query the database every N seconds.

**Why it's wrong:** Wastes CPU cycles during idle periods. Introduces latency (up to N seconds). Forces you to choose between responsiveness (low N = more CPU) and efficiency (high N = missed messages for longer).

**Do this instead:** Use kqueue on the WAL file. Zero CPU when idle, near-instant wake on new notifications.

### Anti-Pattern 2: Storing All Processed rec_ids as a Set

**What people do:** Maintain a set of every rec_id ever seen and persist it as a JSON array.

**Why it's wrong:** The set grows without bound as macOS accumulates notifications. After weeks/months, state file becomes unnecessarily large. Loading a large set on startup becomes slow.

**Do this instead:** Store only the high-water mark (single integer). Periodically, if paranoid about DB resets, also store a hash or count as a sanity check.

### Anti-Pattern 3: Opening the DB in Read-Write Mode

**What people do:** Use default `sqlite3.connect(path)` which opens read-write.

**Why it's wrong:** Risk of accidental writes to a system database. Potential lock contention with usernoted. macOS integrity protection may block write access entirely on newer versions.

**Do this instead:** Always use `?mode=ro` in the URI: `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`.

### Anti-Pattern 4: Parsing the Binary Plist Manually

**What people do:** Try to parse the binary plist format manually or use regex on the raw bytes.

**Why it's wrong:** Binary plist is a structured format. Manual parsing is fragile and incomplete.

**Do this instead:** Use Python's built-in `plistlib.loads(data)` which handles binary plist natively.

### Anti-Pattern 5: Monolithic Single Script

**What people do:** Put DB watching, plist decoding, Teams filtering, JSON formatting, and webhook posting all in one script.

**Why it's wrong:** Cannot test components independently. Cannot reuse the watcher for other notification types. Harder to debug which layer failed.

**Do this instead:** Maintain the nchook/handler separation. nchook watches and dispatches; handler filters and delivers. Two focused components rather than one sprawling script.

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| macOS Notification Center (usernoted) | Read-only SQLite access + kqueue on WAL file | System-owned DB; read-only access essential. DB path varies by macOS version. |
| Webhook Endpoint | HTTP POST with JSON body | Timeout should be short (5-10s). Log-and-skip on failure per project requirements. |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| nchook -> handler | subprocess.call with positional args | Clean process boundary. Handler receives APP, TITLE, BODY, TIME, SUBT as $1-$5. Handler exit code is informational only (nchook continues regardless). |
| handler -> config | File read (config.json) | Handler reads config on each invocation (stateless per-call). Alternatively, handler could cache config and re-read on SIGHUP. |
| handler -> state | File read/write (state.json) | Note: if state is managed by nchook (rec_id tracking), handler doesn't need state. If handler manages state, it must handle concurrent invocations safely (though nchook calls sequentially). |

### Critical Boundary Decision: Where Does State Live?

Two viable approaches:

**Option A: State in nchook (recommended)**
- nchook tracks last_rec_id in memory + persists to state file
- nchook only calls handler for truly new notifications
- Handler is stateless -- receives args, filters, posts, exits
- Pro: Single writer for state file, no race conditions
- Pro: Handler is simple and testable
- Con: Requires patching nchook's state management (adding file persistence)

**Option B: State in handler**
- nchook calls handler for every notification on WAL change
- Handler maintains its own rec_id tracking
- Pro: nchook needs fewer patches
- Con: Handler must be careful about concurrent calls (though nchook calls sequentially)
- Con: Handler does duplicate work (called for already-seen notifications)

**Recommendation:** Option A. nchook already tracks rec_ids in memory; patching it to persist to a file is minimal work and keeps the handler stateless.

## Build Order (Suggested Phase Dependencies)

Understanding component dependencies informs what to build first:

### Phase 1: Patched nchook (Foundation)

Everything depends on nchook being able to watch the Sequoia DB and extract the subtitle field. This is the critical path.

**Tasks:**
1. Fork nchook, update DB path detection for Sequoia
2. Add `subt` extraction from binary plist
3. Add `subt` as 5th argument to callback dispatch
4. Add state file persistence for `last_rec_id`
5. Test: run patched nchook with a dummy handler that prints args

**Depends on:** Nothing (foundational component)
**Blocks:** Everything else

### Phase 2: Wrapper Script (Core Logic)

With nchook dispatching notifications correctly, build the filtering and delivery layer.

**Tasks:**
1. Receive args from nchook ($1-$5)
2. Filter by Teams bundle ID
3. Allowlist logic (require sender + body)
4. Noise rejection (system alerts, reactions, etc.)
5. JSON construction with truncation detection
6. Config file reading (webhook URL)
7. HTTP POST to webhook
8. Error handling (log-and-skip)

**Depends on:** Phase 1 (needs nchook to call it with correct args)
**Blocks:** Phase 3

### Phase 3: Integration and Hardening

End-to-end testing and operational polish.

**Tasks:**
1. Entry point script (run.sh) wiring nchook to handler
2. End-to-end test with real Teams notifications
3. Logging (what was filtered, what was forwarded, failures)
4. Config validation on startup
5. Graceful shutdown handling (SIGTERM/SIGINT)

**Depends on:** Phase 1 + Phase 2

## Scaling Considerations

This is a single-user local daemon. "Scaling" means reliability, not throughput.

| Concern | Normal Operation | Edge Cases |
|---------|-----------------|------------|
| Notification volume | Teams generates ~1-50 notifications/hour for active user | Meeting chat can generate bursts of 10-20 in seconds |
| DB size | Notification DB grows over time; old records pruned by macOS | Thousands of records is normal; query should use rec_id index |
| WAL file churn | WAL written on each notification | Checkpoint events also trigger kqueue -- must handle "no new records" case |
| Webhook latency | Synchronous POST per notification; ~100-500ms typical | Slow webhook blocks processing of next notification; consider async or timeout |
| State file I/O | Write state after each notification batch | Crash between processing and state write = re-processing on restart (idempotent if downstream handles dupes) |

### Burst Handling

When a meeting chat generates rapid notifications, nchook receives one kqueue event per WAL write (or batched events). The query `WHERE rec_id > last_seen` naturally batch-reads all new records. The subprocess calls are sequential, so a burst of 20 notifications means 20 sequential handler invocations. This is fine -- each handler call is fast (filter + POST).

If webhook latency becomes a concern during bursts, the handler could batch multiple notifications into a single POST. But this adds complexity and should only be considered if real-world testing shows it's needed.

## Sources

- PROJECT.md in this repository (primary architecture decisions and constraints)
- nchook by who23 on GitHub (https://github.com/who23/nchook) -- training data knowledge of the tool's architecture, kqueue-based WAL watching, subprocess dispatch pattern. **Confidence: MEDIUM** -- could not fetch live source to verify current state.
- macOS notification center SQLite database internals -- training data knowledge of the DB schema, binary plist storage, usernoted daemon. **Confidence: MEDIUM** -- schema details reconstructed from training data; column names and plist keys should be verified against live database.
- Python sqlite3, plistlib, select.kqueue documentation -- **Confidence: HIGH** -- stable stdlib APIs unlikely to have changed.
- macOS kqueue documentation -- **Confidence: HIGH** -- stable kernel API.

### Verification Recommendations

Before implementation, verify these against the live system:

1. **DB path exists:** `ls -la ~/Library/Group\ Containers/group.com.apple.usernoted/db2/db`
2. **DB schema:** `sqlite3 ~/Library/Group\ Containers/group.com.apple.usernoted/db2/db ".schema"`
3. **Plist keys:** Decode a real Teams notification blob and inspect key names
4. **nchook current source:** Fetch https://github.com/who23/nchook to confirm current callback args and DB path logic
5. **Teams bundle ID:** Check which bundle ID your Teams installation uses (`com.microsoft.teams2` for new Teams, `com.microsoft.teams` for classic)

---
*Architecture research for: macOS notification database interception daemon*
*Researched: 2026-02-11*
