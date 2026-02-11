# Phase 1: DB Watcher & State Engine - Research

**Researched:** 2026-02-11
**Domain:** macOS notification database watching via kqueue/SQLite, binary plist parsing, state persistence
**Confidence:** HIGH (nchook source verified, DB files confirmed on target machine, stdlib APIs verified)

## Summary

Phase 1 builds the core notification engine: a Python daemon that watches the macOS notification center SQLite database for new records, decodes binary plist payloads to extract notification fields (title, subtitle, body, timestamp), persists its read position across restarts, and validates the runtime environment on startup. This is the foundational component that all subsequent phases depend on.

The primary technical challenge is NOT the watching or parsing themselves (both are well-understood patterns), but rather **adapting the existing nchook codebase**. nchook's actual source code (verified from GitHub) reveals it uses two external dependencies (`apsw` and `watchdog`) and targets a pre-Sequoia DB path. The project decision to use "zero external dependencies" (stdlib only) means nchook cannot be used as-is -- it must be substantially rewritten, not merely patched. The subprocess callback architecture can be preserved, but the DB access, file watching, and state management layers all need reimplementation using stdlib modules.

The second challenge is the **nested plist structure**. The nchook source code confirms that notification fields are nested under a `"req"` key (e.g., `plist["req"]["titl"]`), not at the top level. The `"subt"` (subtitle) key lives under `"req"` as well, which nchook does not currently extract. The `"app"` and `"date"` keys ARE at the top level. Getting this nesting wrong means silent empty-string extraction.

**Primary recommendation:** Rewrite nchook's core as a new Python module using stdlib (`select.kqueue`, `sqlite3`, `plistlib`) rather than patching the existing code that depends on `apsw` and `watchdog`. Preserve the subprocess callback dispatch pattern.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `select.kqueue` (stdlib) | Python 3.12+ | WAL file change detection | Native BSD kernel event queue. Zero-latency wakeup on file writes. nchook uses watchdog's KqueueObserver which wraps this same kernel API but adds an external dependency. Direct stdlib usage eliminates the dependency. |
| `sqlite3` (stdlib) | Python 3.12+ / SQLite 3.45.3 | Read-only notification DB access | nchook uses `apsw` (Another Python SQLite Wrapper). stdlib `sqlite3` supports URI mode (`?mode=ro`) for read-only access and `timeout` for busy handling. Sufficient for our read-only queries. |
| `plistlib` (stdlib) | Python 3.12+ | Binary plist decoding | Handles `bplist00` format natively via `plistlib.loads()`. nchook already uses this. |
| `json` (stdlib) | Python 3.12+ | State file serialization | Simple JSON for persisting `last_rec_id`. |
| `logging` (stdlib) | Python 3.12+ | Structured daemon logging | Log levels, formatters, startup summary output. |
| `os` / `pathlib` (stdlib) | Python 3.12+ | Path detection, file operations, atomic writes | DB path detection, WAL path construction, state file atomic writes via `os.replace()`. |
| `signal` (stdlib) | Python 3.12+ | Graceful handling (future) | Register for SIGINT/SIGTERM (Phase 3 scope, but import needed from Phase 1 for clean design). |
| `subprocess` (stdlib) | Python 3.12+ | Callback script dispatch | Invoke handler script with notification fields as positional arguments. Same pattern as nchook. |
| `time` (stdlib) | Python 3.12+ | Poll interval, kqueue timeout | Fallback poll timer, Cocoa timestamp conversion. |
| `tempfile` (stdlib) | Python 3.12+ | Atomic state file writes | `NamedTemporaryFile` in same directory as state file, then `os.replace()`. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `struct` (stdlib) | Python 3.12+ | Low-level binary inspection | Only if plistlib fails on edge-case blobs (unlikely). |
| `fcntl` (stdlib) | Python 3.12+ | Advisory file locking | If multiple daemon instances need to be prevented. Optional. |
| `platform` (stdlib) | Python 3.12+ | macOS version detection | `platform.mac_ver()` to detect macOS version for path selection if needed. |
| `errno` (stdlib) | Python 3.12+ | Error code handling | Check specific OS errors (EACCES for FDA, ENOENT for missing files). |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `select.kqueue` (stdlib) | `watchdog` (KqueueObserver) | watchdog handles inode tracking/file recreation automatically but adds external dependency. Direct kqueue is ~15 lines of code for our single-file watch case. |
| `sqlite3` (stdlib) | `apsw` | apsw has better SQLite coverage (virtual tables, blob streaming). stdlib `sqlite3` is sufficient for read-only queries with URI mode. |
| `os.replace()` for atomic writes | `atomicwrites` library | External dependency for a 5-line pattern. `os.replace()` is atomic on POSIX (macOS). |
| Manual kqueue loop | `selectors` (stdlib high-level) | `selectors` doesn't expose kqueue vnode filters. Direct `select.kqueue` is required. |

**Installation:**
```bash
# No installation needed. Entire stack is Python standard library.
python3 --version  # Should be 3.12+ (verified: 3.12.7 on target machine)
```

## Architecture Patterns

### Recommended Project Structure (Phase 1 deliverables)
```
macos-notification-intercept/
├── nchook.py                  # Main daemon: kqueue watcher + DB reader + plist decoder + state manager
├── state.json                 # Persisted state (auto-generated, gitignored)
│   # { "last_rec_id": 12345 }
├── .gitignore                 # state.json, __pycache__
└── .planning/                 # Project planning
```

Note: Phase 1 outputs a single `nchook.py` that can be run standalone. The handler/wrapper script is Phase 2 scope. During Phase 1 development, the daemon prints extracted notification fields to stdout/logs instead of dispatching to a handler.

### Pattern 1: Direct kqueue WAL File Watching (replaces watchdog)
**What:** Use `select.kqueue()` directly with `KQ_FILTER_VNODE` + `KQ_NOTE_WRITE` on the WAL file descriptor. Block until the WAL is written to, then query the DB for new records.
**When to use:** Always -- this is the event-driven core of the daemon.
**Key detail:** kqueue watches file descriptors (inodes), not paths. If the WAL file is deleted and recreated (during checkpoint), the old FD becomes stale. Must detect this and re-register.
```python
# Source: Python 3.12 select module docs + nchook architecture
import select
import os

def create_wal_watcher(wal_path):
    """Create kqueue watcher for WAL file. Returns (kq, fd, event)."""
    kq = select.kqueue()
    fd = os.open(wal_path, os.O_RDONLY)
    event = select.kevent(
        fd,
        filter=select.KQ_FILTER_VNODE,
        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
        fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
    )
    return kq, fd, event

# Main loop with fallback poll timeout
POLL_INTERVAL = 5.0  # seconds -- fallback if kqueue misses events
while running:
    events = kq.control([event], 1, POLL_INTERVAL)
    # Whether triggered by kqueue event or timeout, query for new records
    process_new_notifications(conn, last_rec_id)
```

### Pattern 2: High-Water Mark State Tracking with Atomic Persistence
**What:** Track only the highest `rec_id` processed (single integer). Persist to JSON file using write-then-replace (atomic write) pattern.
**When to use:** Always. `rec_id` is `INTEGER PRIMARY KEY` (monotonically increasing in SQLite). Query is `WHERE rec_id > ?`.
```python
# Source: SQLite INTEGER PRIMARY KEY guarantee + Python os.replace() POSIX atomicity
import json
import os
import tempfile

STATE_FILE = "state.json"

def save_state(last_rec_id, state_path=STATE_FILE):
    """Atomically persist state using write-then-replace."""
    state = {"last_rec_id": last_rec_id}
    dir_name = os.path.dirname(os.path.abspath(state_path))
    with tempfile.NamedTemporaryFile(
        mode='w', dir=dir_name, suffix='.tmp', delete=False
    ) as tmp:
        json.dump(state, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, state_path)

def load_state(state_path=STATE_FILE):
    """Load persisted state. Returns 0 if no state file."""
    try:
        with open(state_path, 'r') as f:
            return json.load(f).get("last_rec_id", 0)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return 0
```

### Pattern 3: Read-Only SQLite with WAL Awareness
**What:** Open the notification DB with `?mode=ro` URI parameter. Set busy_timeout for transient lock handling. Keep transactions short.
**When to use:** Always -- this is a system database owned by usernoted.
```python
# Source: Python 3.12 sqlite3 docs (URI mode), SQLite WAL mode docs
import sqlite3

def open_db(db_path):
    """Open notification DB in read-only mode."""
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=5.0  # busy_timeout in seconds
    )
    conn.row_factory = sqlite3.Row  # dict-like access to columns
    return conn
```

### Pattern 4: Defensive Binary Plist Parsing with Nested Key Access
**What:** Decode binary plist from the `data` column. Fields are nested: `titl`, `subt`, `body` live under the `"req"` key. `app` and `date` are at the top level.
**When to use:** For every notification record extracted from the DB.
**CRITICAL -- verified from nchook source:**
```python
# Source: nchook source code (github.com/who23/nchook) -- VERIFIED
# The plist structure is:
# {
#     "app": "com.microsoft.teams2",     # TOP LEVEL
#     "date": 728571234.567,             # TOP LEVEL (Cocoa timestamp)
#     "req": {                           # NESTED container
#         "titl": "John Smith",          # under req
#         "subt": "Project Alpha Chat",  # under req (NOT extracted by nchook)
#         "body": "Hey, can you..."      # under req
#     },
#     ...other keys...
# }

import plistlib

COCOA_TO_UNIX_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01

def parse_notification(raw_data):
    """Parse binary plist blob into notification dict. Returns None on failure."""
    try:
        plist = plistlib.loads(raw_data, fmt=plistlib.FMT_BINARY)
    except Exception:
        # plistlib.loads also handles XML plists if fmt is not specified
        # but we specify FMT_BINARY since that's what the DB stores
        try:
            plist = plistlib.loads(raw_data)
        except Exception:
            return None

    req = plist.get("req", {})

    return {
        "app": plist.get("app", ""),
        "title": req.get("titl", ""),
        "subtitle": req.get("subt", ""),
        "body": req.get("body", ""),
        "timestamp": plist.get("date", 0) + COCOA_TO_UNIX_OFFSET if plist.get("date") else 0,
    }
```

### Pattern 5: DB Purge/Reset Detection
**What:** On startup and periodically, compare persisted `last_rec_id` against `MAX(rec_id)` in the DB. If MAX < persisted, the DB was purged -- reset state with a warning.
```python
def check_db_consistency(conn, persisted_rec_id):
    """Detect DB purge/recreation. Returns adjusted rec_id."""
    cursor = conn.execute("SELECT MAX(rec_id) FROM record")
    row = cursor.fetchone()
    max_rec_id = row[0] if row[0] is not None else 0

    if persisted_rec_id > max_rec_id:
        logging.warning(
            f"DB purge detected: persisted rec_id={persisted_rec_id} > "
            f"max DB rec_id={max_rec_id}. Resetting state."
        )
        return 0  # reset to beginning
    return persisted_rec_id
```

### Anti-Patterns to Avoid
- **Using `apsw` or `watchdog`:** nchook's dependencies. stdlib equivalents exist and eliminate external deps.
- **Querying with `WHERE rec_id NOT IN (set)`:** nchook does this -- grows unboundedly. Use `WHERE rec_id > ?` with high-water mark.
- **Opening DB without `mode=ro`:** Risk of accidental writes to system DB.
- **Assuming plist keys at top level:** `titl`, `subt`, `body` are nested under `"req"`. Only `app` and `date` are top-level.
- **Polling only (no kqueue):** Wastes CPU, adds latency. kqueue is near-instant.
- **kqueue only (no fallback poll):** WAL checkpoints can cause missed events. Always have a fallback timer.
- **Keeping DB connection/transaction open across event loop iterations:** Blocks usernoted checkpointing. Open-query-close per cycle, or at minimum close cursor promptly.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Binary plist parsing | Custom byte parser or regex on raw blob | `plistlib.loads(data, fmt=plistlib.FMT_BINARY)` | Binary plist is a complex format with offset tables, typed values. plistlib handles all of it. |
| File system event monitoring | Custom inotify/FSEvents wrapper | `select.kqueue` with `KQ_FILTER_VNODE` | kqueue is the correct macOS kernel API for single-file monitoring. 10 lines of setup code. |
| Atomic file writes | `open(path, 'w').write()` (non-atomic) | `tempfile.NamedTemporaryFile` + `os.fsync()` + `os.replace()` | Non-atomic writes corrupt state on crash. The temp+replace pattern is POSIX atomic. |
| Cocoa timestamp conversion | Manual epoch math | Constant offset: `cocoa_ts + 978307200` | Well-known constant. The offset between Unix epoch (1970) and Cocoa epoch (2001) is exactly 978307200 seconds. Verified. |
| SQLite busy handling | Retry loop with sleep | `sqlite3.connect(..., timeout=5.0)` | The `timeout` parameter is the built-in busy_timeout. sqlite3 retries internally for up to N seconds. |

**Key insight:** Every component of Phase 1 maps to a stdlib module or a simple constant. The complexity is in correct composition (nesting, ordering, error handling), not in any single component.

## Common Pitfalls

### Pitfall 1: nchook Uses External Dependencies (apsw + watchdog)
**What goes wrong:** Attempting to "patch" nchook by modifying a few lines. nchook's source (verified from GitHub) imports `apsw` and `watchdog.observers.kqueue.KqueueObserver`. Neither is in stdlib. `apsw` is not installed on this machine.
**Why it happens:** The project description says "patched nchook" implying small modifications. But nchook's core DB access uses `apsw.Connection()` and its file watching uses `watchdog.KqueueObserver`. Both must be replaced wholesale.
**How to avoid:** Rewrite the core functionality using stdlib. Preserve nchook's architecture (event handler + DB query + plist parse + subprocess dispatch) but replace the plumbing:
- `apsw.Connection(db_file)` -> `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`
- `apsw.BusyError` -> `sqlite3.OperationalError` + `timeout` param
- `KqueueObserver.schedule()` -> Direct `select.kqueue()` + `select.kevent()`
- `watchdog.events.FileSystemEventHandler.on_modified()` -> kqueue event loop with `kq.control()`
**Warning signs:** ImportError for `apsw` on first run.

### Pitfall 2: Wrong Plist Key Nesting Depth
**What goes wrong:** Extracting `plist["titl"]` instead of `plist["req"]["titl"]`. Returns `None`/KeyError instead of the actual title.
**Why it happens:** Prior research docs and some forensic tools show keys at top level. But the VERIFIED nchook source shows `notif_plist["req"]["titl"]` and `notif_plist["req"]["body"]`. The `"req"` nesting is real and confirmed.
**How to avoid:** Always access `titl`, `subt`, `body` via `plist.get("req", {}).get("key", "")`. Access `app` and `date` at top level: `plist.get("app", "")`.
**Warning signs:** All parsed notifications have empty title/body but non-empty app.

### Pitfall 3: nchook's Unbounded rec_id List
**What goes wrong:** nchook stores ALL seen rec_ids in a Python list and queries `WHERE rec_id NOT IN (?,?,?,...,?)`. After days of operation, this list has thousands of entries. SQL generation slows down. Memory grows.
**Why it happens:** nchook was designed for short sessions, not persistent daemon operation.
**How to avoid:** Replace with high-water mark pattern: store single `last_rec_id`, query `WHERE rec_id > ?`. O(1) state instead of O(n).
**Warning signs:** Increasing memory usage over time, gradually slower DB queries.

### Pitfall 4: kqueue Stale File Descriptor After WAL Checkpoint
**What goes wrong:** usernoted periodically checkpoints the WAL (moves WAL contents back to main DB). This can delete and recreate the WAL file. The kqueue FD points to the old inode. No more events fire. Daemon appears alive but stops seeing notifications.
**Why it happens:** kqueue monitors inodes (file descriptors), not paths. When the file is replaced, the FD is orphaned.
**How to avoid:** Two complementary strategies:
1. **Fallback poll timer:** Set `kq.control()` timeout to 5-10 seconds. On timeout (no kqueue event), query DB anyway. Catches anything missed during checkpoint.
2. **Also watch for NOTE_DELETE and NOTE_RENAME:** Include these in fflags. If received, close old FD, re-open WAL file, re-register kqueue event.
**Warning signs:** Daemon stops detecting notifications after running for hours/days but still runs. Restart fixes it.

### Pitfall 5: Full Disk Access (FDA) Silent Failure
**What goes wrong:** Without FDA, `sqlite3.connect()` on the notification DB raises `OperationalError: unable to open database file`. But the file exists and is visible via `stat()`. This is macOS TCC (Transparency, Consent, and Control) blocking read access.
**Why it happens:** macOS Sequoia+ moved the notification DB to a TCC-protected Group Container. The terminal/app running Python needs FDA granted in System Settings.
**How to avoid:** On startup, attempt to open and query the DB. If it fails with "unable to open database file" and the file exists, print specific FDA instructions:
```
ERROR: Cannot read notification database.
Full Disk Access is required.

To grant access:
  1. Open System Settings > Privacy & Security > Full Disk Access
  2. Click the + button
  3. Add your terminal app (Terminal.app, iTerm2, etc.)
  4. Restart the daemon
```
**Warning signs:** `OperationalError: unable to open database file` when DB path is confirmed to exist.

### Pitfall 6: Pre-Sequoia DB Path (nchook's Default)
**What goes wrong:** nchook uses `getconf DARWIN_USER_DIR` to find `com.apple.notificationcenter/db2/db`. This path does NOT exist on Sequoia/Tahoe (verified: the directory is empty on this machine). The Sequoia+ path is `~/Library/Group Containers/group.com.apple.usernoted/db2/db` (verified: file exists, 536KB).
**Why it happens:** nchook was written pre-Sequoia. macOS 15 moved the notification DB to a Group Container for TCC protection.
**How to avoid:** Detect Sequoia+ path first. Fall back to legacy path. Fail with clear error if neither exists.
```python
SEQUOIA_DB = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.usernoted/db2/db"
)
LEGACY_DB_DIR = os.path.join(
    subprocess.run(['getconf', 'DARWIN_USER_DIR'],
                   capture_output=True, text=True).stdout.strip(),
    "com.apple.notificationcenter", "db2"
)
LEGACY_DB = os.path.join(LEGACY_DB_DIR, "db")
```
**Warning signs:** `FileNotFoundError` or "no such file" on the legacy path.

### Pitfall 7: DB Purge Causes Silent Stall
**What goes wrong:** macOS purges old notifications. If persisted `last_rec_id` is 50000 but max DB `rec_id` is 100 after purge, `WHERE rec_id > 50000` returns zero rows forever.
**Why it happens:** Developers test with growing databases, never with purged ones.
**How to avoid:** On startup: `SELECT MAX(rec_id) FROM record`. If max < persisted, reset with warning. Already covered in Pattern 5 above.
**Warning signs:** Zero notifications captured despite visible notifications in macOS UI.

### Pitfall 8: macOS Version is Tahoe (26.x), Not Sequoia (15.x)
**What goes wrong:** The project spec says "macOS Sequoia (15+)" but this machine runs macOS Tahoe 26.2. The notification DB path is the same (Group Containers path), but there could be schema changes.
**Why it happens:** macOS Tahoe is the successor to Sequoia. The project was specced when Sequoia was current.
**How to avoid:** The DB path is confirmed to work on Tahoe (file exists at expected path). Schema should be verified during implementation by running `.schema` against the live DB once FDA is granted. Design code to detect macOS version but not hard-gate on version number -- just validate that the expected path and schema exist.
**Warning signs:** Unexpected table or column names when querying the DB.

## Code Examples

### Complete DB Query for New Notifications
```python
# Source: nchook source code (adapted from apsw to sqlite3)
# Verified plist structure and SQL pattern

def query_new_notifications(conn, last_rec_id):
    """
    Query for notifications newer than last_rec_id.
    Returns list of (rec_id, parsed_notification) tuples.
    """
    results = []
    try:
        cursor = conn.execute(
            """
            SELECT r.rec_id, r.data, a.identifier, r.delivered_date
            FROM record r
            JOIN app a ON r.app_id = a.app_id
            WHERE r.rec_id > ?
            ORDER BY r.rec_id ASC
            """,
            (last_rec_id,)
        )
        for row in cursor:
            notif = parse_notification(row['data'])
            if notif is None:
                logging.warning(f"Failed to parse plist for rec_id={row['rec_id']}")
                continue
            # Override app with the JOIN result (more reliable than plist)
            notif['app'] = row['identifier']
            notif['rec_id'] = row['rec_id']
            results.append(notif)
    except sqlite3.OperationalError as e:
        logging.error(f"DB query failed: {e}")
    return results
```

### Complete Startup Validation Sequence
```python
# Source: Composite from verified paths, FDA behavior, and DB schema knowledge

import os
import sys
import sqlite3
import logging

def validate_environment():
    """
    Validate FDA, DB path, and DB schema on startup.
    Returns (db_path, wal_path) or exits with actionable error.
    """
    # 1. Detect DB path
    db_path = os.path.expanduser(
        "~/Library/Group Containers/group.com.apple.usernoted/db2/db"
    )
    if not os.path.exists(db_path):
        logging.error(f"Notification database not found at: {db_path}")
        logging.error("This daemon requires macOS Sequoia (15+) or later.")
        sys.exit(1)

    wal_path = db_path + "-wal"
    if not os.path.exists(wal_path):
        logging.warning(f"WAL file not found at: {wal_path}")
        logging.warning("Database may not be in WAL mode. Falling back to poll-only.")

    # 2. Test FDA by attempting to read the DB
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # Verify we can actually read data (not just open the file)
        cursor = conn.execute("SELECT COUNT(*) FROM record")
        count = cursor.fetchone()[0]
        logging.info(f"FDA check passed. {count} records in notification DB.")
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            logging.error("ERROR: Cannot read notification database.")
            logging.error("Full Disk Access is required.")
            logging.error("")
            logging.error("To grant access:")
            logging.error("  1. Open System Settings > Privacy & Security > Full Disk Access")
            logging.error("  2. Click the + button")
            logging.error("  3. Add your terminal app (Terminal.app, iTerm2, etc.)")
            logging.error("  4. Restart the daemon")
            sys.exit(1)
        raise

    # 3. Verify expected schema
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        if 'record' not in tables or 'app' not in tables:
            logging.error(f"Unexpected DB schema. Tables found: {tables}")
            logging.error("Expected 'record' and 'app' tables.")
            sys.exit(1)
    except sqlite3.OperationalError as e:
        logging.error(f"Schema validation failed: {e}")
        sys.exit(1)
    finally:
        conn.close()

    return db_path, wal_path
```

### Complete kqueue Event Loop with Fallback
```python
# Source: Python select module docs + kqueue vnode pattern

import select
import os
import time
import logging

POLL_FALLBACK_SECONDS = 5.0

def run_watcher(db_path, wal_path, state_path):
    """Main event loop: kqueue + fallback poll."""
    conn = open_db(db_path)
    last_rec_id = load_state(state_path)
    last_rec_id = check_db_consistency(conn, last_rec_id)

    # Print startup summary (OPER-02)
    print_startup_summary(db_path, last_rec_id)

    kq = None
    wal_fd = None

    try:
        # Set up kqueue if WAL exists
        if os.path.exists(wal_path):
            kq = select.kqueue()
            wal_fd = os.open(wal_path, os.O_RDONLY)
            kev = select.kevent(
                wal_fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                fflags=(select.KQ_NOTE_WRITE |
                        select.KQ_NOTE_DELETE |
                        select.KQ_NOTE_RENAME)
            )
            logging.info("kqueue watching WAL file")
        else:
            logging.warning("No WAL file -- falling back to poll-only mode")

        running = True
        while running:
            wal_deleted = False

            if kq is not None:
                # Block until WAL write OR timeout (fallback poll)
                try:
                    events = kq.control([kev], 1, POLL_FALLBACK_SECONDS)
                except OSError:
                    events = []
                    logging.warning("kqueue error -- will re-register")

                for ev in events:
                    if ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                        wal_deleted = True
            else:
                time.sleep(POLL_FALLBACK_SECONDS)

            # Query for new notifications regardless of trigger source
            new_notifs = query_new_notifications(conn, last_rec_id)
            for notif in new_notifs:
                # Phase 1: print to stdout. Phase 2: dispatch to handler.
                logging.info(
                    f"[{notif['app']}] {notif['title']}: {notif['body']}"
                    f" (subt={notif['subtitle']})"
                )
                last_rec_id = notif['rec_id']

            if new_notifs:
                save_state(last_rec_id, state_path)

            # Re-register kqueue if WAL was deleted/renamed
            if wal_deleted and os.path.exists(wal_path):
                if wal_fd is not None:
                    os.close(wal_fd)
                wal_fd = os.open(wal_path, os.O_RDONLY)
                kev = select.kevent(
                    wal_fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                    fflags=(select.KQ_NOTE_WRITE |
                            select.KQ_NOTE_DELETE |
                            select.KQ_NOTE_RENAME)
                )
                logging.info("Re-registered kqueue on new WAL file")

    finally:
        if wal_fd is not None:
            os.close(wal_fd)
        if kq is not None:
            kq.close()
        conn.close()
```

### Startup Summary Output (OPER-02)
```python
def print_startup_summary(db_path, last_rec_id, bundle_ids=None):
    """Print startup summary per OPER-02 requirement."""
    print("=" * 60)
    print("Teams Notification Interceptor")
    print("=" * 60)
    print(f"  DB path:     {db_path}")
    print(f"  FDA status:  OK")
    print(f"  Last rec_id: {last_rec_id}")
    if bundle_ids:
        print(f"  Bundle IDs:  {', '.join(bundle_ids)}")
    print("=" * 60)
```

## State of the Art

| Old Approach (nchook) | Current Approach (this project) | Why Changed | Impact |
|---|---|---|---|
| `apsw.Connection(db_file)` | `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` | Eliminate external dependency; force read-only | No `apsw` install needed; safer DB access |
| `watchdog.KqueueObserver` | Direct `select.kqueue()` | Eliminate external dependency; simpler for single-file watch | No `watchdog` install needed; full control over kqueue behavior |
| `rec_ids` list + `NOT IN` SQL | High-water mark `last_rec_id` + `WHERE rec_id > ?` | O(n) -> O(1) state; unbounded growth fixed | Constant memory; faster queries; simpler state file |
| In-memory only state | JSON state file with atomic writes | Survive restarts without replaying history | Required for daemon reliability |
| `getconf DARWIN_USER_DIR` path | `~/Library/Group Containers/group.com.apple.usernoted/db2/db` | macOS Sequoia moved DB to TCC-protected Group Container | Works on Sequoia+; original path no longer exists |
| 4 callback args (APP, TITLE, BODY, TIME) | 5 callback args (APP, TITLE, BODY, TIME, SUBT) | nchook never extracted `subt` from plist | Chat/channel name now available to wrapper |
| No startup validation | FDA check + path validation + schema check + purge detection | Fail loudly instead of silently producing nothing | Actionable error messages for common setup failures |

**Deprecated/outdated in nchook:**
- `apsw` dependency: replaced by stdlib `sqlite3` with URI mode
- `watchdog` dependency: replaced by stdlib `select.kqueue`
- Pre-Sequoia DB path via `getconf DARWIN_USER_DIR`: path no longer exists on Sequoia+
- Unbounded `rec_ids` list: replaced by high-water mark integer
- `python3.9` shebang: update to `python3` (system is 3.12.7)

## Open Questions

1. **Exact DB schema on Tahoe (macOS 26.x)**
   - What we know: The DB file exists at the Sequoia path on this machine (macOS Tahoe 26.2). File is 536KB, WAL is 840KB and actively written.
   - What's unclear: Whether table/column names match the expected `record`/`app` schema. Cannot verify until FDA is granted.
   - Recommendation: First task in implementation should be granting FDA to the development terminal and running `sqlite3 <path> ".schema"` to confirm schema. Design code to fail loudly if schema differs.

2. **Exact plist key names on current macOS version**
   - What we know: nchook source (verified) accesses `plist["req"]["titl"]` and `plist["req"]["body"]`. Forensic tools (objective-see, macNotifications.py) confirm `req.titl`, `req.subt`, `req.body`. The `subt` key exists in the plist but nchook never extracts it.
   - What's unclear: Whether macOS Tahoe 26.x has changed any key names from the Sequoia-era format.
   - Recommendation: During implementation, decode a real notification blob and log its full structure. Adjust key paths if different.

3. **Whether `plistlib.FMT_BINARY` is always correct**
   - What we know: nchook uses `plistlib.loads(raw_plist, fmt=plistlib.FMT_BINARY)`. The forensic tools also show binary plists.
   - What's unclear: Whether any notification records use XML plist format instead.
   - Recommendation: Try `FMT_BINARY` first. If it fails, retry without the `fmt` parameter (plistlib auto-detects). Log any fallback cases.

4. **WAL checkpoint behavior under heavy notification load**
   - What we know: SQLite WAL checkpointing can delete/recreate the WAL file, breaking kqueue watches.
   - What's unclear: How frequently usernoted checkpoints. Whether it's time-based, size-based, or both.
   - Recommendation: The fallback poll timer (5s) handles this regardless. Also watch for `KQ_NOTE_DELETE` to detect WAL recreation and re-register.

## Sources

### Primary (HIGH confidence)
- **nchook source code** - [github.com/who23/nchook](https://github.com/who23/nchook) - Fetched via `gh api`. Complete source reviewed. Confirmed: uses `apsw`, `watchdog`, plist nested under `"req"` key, 4-arg callback (no subt), pre-Sequoia DB path.
- **Python 3.12 stdlib docs** - `select` module (kqueue/kevent API), `sqlite3` module (URI mode, timeout), `plistlib` module (FMT_BINARY). Verified all modules available on target machine.
- **Target machine verification** - macOS Tahoe 26.2, Python 3.12.7, SQLite 3.45.3. DB exists at `~/Library/Group Containers/group.com.apple.usernoted/db2/db` (536KB). WAL file exists and is actively written. `apsw` is NOT installed. `watchdog` IS installed but not needed.
- **Cocoa timestamp offset** - Verified: 978307200 seconds between Unix epoch (1970) and Cocoa epoch (2001). Matches nchook's constant.

### Secondary (MEDIUM confidence)
- **[9to5Mac: Sequoia notification DB privacy](https://9to5mac.com/2024/09/01/security-bite-apple-addresses-privacy-concerns-around-notification-center-database-in-macos-sequoia/)** - Confirmed DB moved to Group Containers for TCC protection.
- **[objective-see blog](https://objective-see.org/blog/blog_0x2E.html)** - Confirmed plist keys: `body`, `titl`, `subt`, `atta`, `app`, `date`. Nested under structures.
- **[ydkhatri/MacForensics](https://github.com/ydkhatri/MacForensics/blob/master/macNotifications.py)** - Confirmed High Sierra+ schema: `record` table, `app` table with `identifier`, `data` BLOB column. Confirmed plist access pattern `req['titl']`, `req['subt']`, `req['body']`.
- **[75033us/blog: Monterey schema](https://github.com/75033us/blog/blob/main/2022-02-02-macos-monterey-notification-database-schema.md)** - Tables: `app`, `dbinfo`, `displayed`, `requests`, `categories`, `delivered`, `record`, `snoozed`.
- **Atomic writes**: `os.replace()` is atomic on POSIX per [Python docs](https://docs.python.org/3/library/os.html#os.replace) and [multiple sources](https://blog.gocept.com/2013/07/15/reliable-file-updates-with-python/).

### Tertiary (LOW confidence - needs validation during implementation)
- **DB schema on macOS Tahoe 26.x**: Assumed same as Sequoia based on path existence. Not yet verified (requires FDA).
- **Plist key names on Tahoe**: Assumed same as Sequoia/Monterey. Multiple sources agree on `req.titl/subt/body` pattern, but Apple could change without notice.
- **WAL checkpoint frequency**: Unknown. Mitigated by fallback poll timer design.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - All stdlib modules verified present on target machine. nchook source verified from GitHub.
- Architecture: HIGH - Direct adaptation of nchook's proven pattern, replacing external deps with stdlib equivalents.
- Pitfalls: HIGH - nchook source code revealed specific pitfalls (apsw dependency, unbounded rec_id list, no subt extraction, wrong DB path) that prior research did not fully capture.
- Code examples: MEDIUM-HIGH - Patterns are verified against Python docs and nchook source, but have not been run against the actual notification DB (requires FDA).

**Research date:** 2026-02-11
**Valid until:** 60 days (stable domain -- macOS DB format unlikely to change mid-cycle; stdlib APIs frozen in Python 3.12)
