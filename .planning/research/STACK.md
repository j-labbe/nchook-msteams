# Stack Research

**Domain:** macOS notification interception daemon (Python)
**Researched:** 2026-02-11
**Confidence:** HIGH

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.12+ (system) | Runtime | macOS Sequoia ships Python 3.12. nchook is Python. Zero-dependency stdlib approach means no venv needed. 3.12 has stable `sqlite3`, `plistlib`, `select.kqueue`. |
| `sqlite3` (stdlib) | ships with Python | Read macOS notification center DB | Standard library module. No external driver needed. Supports WAL mode reads, `PRAGMA journal_mode`, connection-level timeouts. Sufficient for read-only access to the notification DB. |
| `select.kqueue` (stdlib) | ships with Python | File system event monitoring | BSD/macOS kernel event notification. nchook already uses this for WAL file change detection. `KQ_FILTER_VNODE` with `NOTE_WRITE` flags detect WAL writes. No external watcher library needed. |
| `plistlib` (stdlib) | ships with Python | Parse binary plist blobs | Standard library since Python 3.4. Handles Apple binary plist format (bplist00) via `plistlib.loads()`. Notification payloads in the DB are stored as binary plist blobs in the `data` column. |
| `urllib.request` (stdlib) | ships with Python | HTTP POST to webhook | Standard library. `urllib.request.Request` with `method='POST'` and JSON body is sufficient for fire-and-forget webhook delivery. No retry logic needed (log-and-skip design). |
| `json` (stdlib) | ships with Python | Config file parsing + webhook payload serialization | Read JSON config, build JSON POST bodies. Standard library, no alternatives needed. |
| `logging` (stdlib) | ships with Python | Structured logging | Standard library logging with formatters. Supports log levels, file/console handlers. Adequate for daemon logging needs. |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `os` / `os.path` (stdlib) | ships with Python | Path resolution, file existence checks | DB path detection (Sequoia vs pre-Sequoia), config/state file paths. |
| `pathlib` (stdlib) | ships with Python | Modern path handling | Preferred over `os.path` for new code. `Path.expanduser()` for `~/Library/...` resolution. |
| `struct` (stdlib) | ships with Python | Binary data unpacking | Only if raw plist parsing falls through; unlikely needed since `plistlib` handles the format. |
| `signal` (stdlib) | ships with Python | Graceful shutdown | Handle SIGTERM/SIGINT for clean daemon shutdown (close DB connections, flush logs). |
| `time` (stdlib) | ships with Python | Poll intervals, timestamps | `time.sleep()` for poll interval (if used alongside kqueue), epoch timestamp handling for notification times. |
| `fcntl` (stdlib) | ships with Python | File locking for state file | Prevent concurrent state file writes if multiple instances accidentally run. `fcntl.flock()` for advisory locks. |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| Python 3.12 (macOS system) | Runtime | No Homebrew Python needed. macOS Sequoia ships 3.12+ via Xcode CLT. Verify with `python3 --version`. |
| No virtualenv needed | Zero external deps | Entire stack is stdlib. No `pip install`, no `requirements.txt`, no dependency management. |
| `sqlite3` CLI | DB inspection during development | Ships with macOS. Use `sqlite3 ~/Library/Group\ Containers/group.com.apple.usernoted/db2/db` to inspect schema. |
| `plutil` (macOS CLI) | Plist debugging | Ships with macOS. `plutil -p` converts binary plist to human-readable for debugging extracted blobs. |
| `log stream` (macOS CLI) | System log monitoring | `log stream --predicate 'subsystem == "com.apple.usernoted"'` to observe notification system behavior during development. |

## Installation

```bash
# No installation needed. Entire stack is Python standard library.
# Verify Python is available:
python3 --version  # Should be 3.12+

# Clone and run:
git clone <repo>
cd macos-notification-intercept
python3 nchook.py  # or whatever the entry point becomes
```

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| `urllib.request` (stdlib) | `requests` library | If you needed session management, automatic retries, complex auth, or connection pooling. This project does fire-and-forget POSTs with log-on-failure -- `urllib.request` is sufficient and avoids the only potential external dependency. |
| `select.kqueue` (stdlib) | `watchdog` library | If you needed cross-platform file watching (Linux, Windows). This project is macOS-only and nchook already uses kqueue directly. `watchdog` would add a dependency and an abstraction layer over what kqueue already provides natively. |
| `select.kqueue` (stdlib) | `FSEvents` via `pyobjc-framework-FSEvents` | If you needed recursive directory watching or higher-level macOS filesystem events. kqueue on a single WAL file is simpler, proven in nchook, and needs no external dependency. FSEvents is designed for directory-tree monitoring which is overkill here. |
| `plistlib` (stdlib) | `biplist` | Never. `biplist` is unmaintained (last release 2018). `plistlib` in Python 3.4+ handles binary plists natively. `biplist` was only needed for Python 2. |
| `json` (stdlib) for config | `tomllib` (stdlib 3.11+) or YAML | If config complexity grew significantly. JSON config is specified in the project requirements and is the simplest option. TOML would need `tomllib` (read-only in stdlib) or `tomli` for write. YAML needs `pyyaml` (external dep). JSON is fine for the ~5 config keys this project needs. |
| `sqlite3` (stdlib) | `apsw` (Another Python SQLite Wrapper) | If you needed advanced SQLite features like virtual tables, custom VFS, or blob streaming. `apsw` provides a more complete SQLite binding. For simple read-only queries on the notification DB, `sqlite3` stdlib is more than sufficient. |
| `logging` (stdlib) | `structlog` | If you needed structured JSON logging for log aggregation pipelines. This is a single-machine daemon; `logging` with a simple formatter is adequate. |
| File-based state (JSON) | `sqlite3` for state | If tracked state grew beyond simple rec_id sets. For this project, state is a set of processed notification IDs -- a JSON file is simpler and perfectly adequate. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `requests` library | Adds the only external dependency for a simple POST. `urllib.request` does the same thing in 5 lines. The project's log-and-skip error handling means you don't need `requests`' retry/session features. | `urllib.request.Request` with `method='POST'` and `json.dumps()` body |
| `watchdog` library | External dependency, cross-platform abstraction you don't need on macOS-only project. Adds complexity over direct kqueue. nchook's proven kqueue approach is simpler. | `select.kqueue` with `KQ_FILTER_VNODE` and `NOTE_WRITE` |
| `biplist` | Unmaintained since 2018. `plistlib` in Python 3.4+ handles binary plists. No reason to use an abandoned library. | `plistlib.loads()` (stdlib) |
| `pyobjc` / Objective-C bridge | Massive dependency tree. Only needed if you were using NSDistributedNotificationCenter or UserNotifications API. DB-watching approach doesn't need Objective-C bindings. | Direct `sqlite3` reads of the notification DB |
| `asyncio` | Adds complexity for no benefit. This daemon does: wait for kqueue event, read DB, POST webhook, repeat. Synchronous flow is clearer, simpler to debug, and nchook is synchronous. No concurrent I/O needed. | Synchronous Python with `select.kqueue` blocking |
| `multiprocessing` / `threading` | Single-threaded event loop (kqueue wait -> process -> webhook) is the correct model. Threading adds complexity, race conditions, and debugging difficulty for zero throughput benefit on a low-volume notification stream. | Single-threaded main loop |
| `schedule` / `APScheduler` | External dependencies for what `time.sleep()` or kqueue timeout handles natively. The daemon's main loop is event-driven (kqueue), not schedule-driven. | kqueue event loop with optional timeout fallback |
| Python 2 | EOL since 2020. nchook may have Python 2 artifacts but all code should target Python 3.12+. | Python 3.12+ |
| Homebrew Python | macOS Sequoia includes Python 3.12+ via Xcode Command Line Tools. Using Homebrew Python adds PATH complexity and potential version conflicts. | System Python 3 (`/usr/bin/python3` or Xcode CLT Python) |

## Stack Patterns

**Zero-dependency daemon pattern (this project):**
- Use stdlib exclusively: `sqlite3`, `plistlib`, `select`, `urllib.request`, `json`, `logging`
- No `requirements.txt`, no virtualenv, no pip
- Ship as a single directory of `.py` files
- Advantage: runs on any macOS Sequoia box with zero setup
- This is the correct pattern because the problem domain maps perfectly to stdlib capabilities

**If webhook delivery needed retries (not this project):**
- Would add `requests` + `urllib3` for exponential backoff
- Would need `requirements.txt` and virtualenv
- But project spec explicitly says log-and-skip, making this unnecessary

**If cross-platform support were needed (not this project):**
- Would replace `select.kqueue` with `watchdog` library
- Would need to abstract the notification DB schema per-platform
- But project spec explicitly targets macOS Sequoia only

## Version Compatibility

| Component | Compatible With | Notes |
|-----------|-----------------|-------|
| Python 3.12+ | macOS Sequoia 15+ | Sequoia ships Python 3.12 via Xcode CLT. Verify `python3 --version`. |
| Python 3.12 `sqlite3` | SQLite 3.43+ | macOS Sequoia bundles SQLite 3.43+. WAL mode reading works. `sqlite3.connect()` with `timeout` parameter handles DB locks from usernoted. |
| Python 3.12 `plistlib` | Binary plist format (bplist00) | Handles NSKeyedArchiver-encoded blobs. The notification DB `data` column stores binary plists that `plistlib.loads()` decodes. |
| Python 3.12 `select.kqueue` | macOS kernel kqueue | BSD kqueue is native to macOS. `KQ_FILTER_VNODE` with `NOTE_WRITE` on the WAL file descriptor detects DB changes. |
| `urllib.request` | HTTPS webhook endpoints | Uses macOS system SSL certificates. Handles HTTPS POSTs without additional SSL config. |

## Key Implementation Notes

### SQLite Read-Only Access
```python
# Always open notification DB read-only to avoid interfering with usernoted
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
```
The `mode=ro` URI parameter prevents accidental writes. The `timeout` handles brief locks from the usernoted process writing to the DB.

### Binary Plist Parsing
```python
import plistlib

# Notification data column contains binary plist
raw_data = row['data']  # bytes from sqlite3
parsed = plistlib.loads(raw_data)
# parsed is a dict with keys like 'app', 'titl', 'subt', 'body', etc.
```

### kqueue WAL Monitoring
```python
import select

kq = select.kqueue()
fd = os.open(wal_path, os.O_RDONLY)
ev = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                   flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                   fflags=select.KQ_NOTE_WRITE)
# Block until WAL file is written to
events = kq.control([ev], 1, timeout)
```

### Webhook POST
```python
import urllib.request
import json

data = json.dumps(payload).encode('utf-8')
req = urllib.request.Request(webhook_url, data=data, method='POST')
req.add_header('Content-Type', 'application/json')
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        logging.info(f"Webhook delivered: {resp.status}")
except Exception as e:
    logging.error(f"Webhook failed (skipping): {e}")
```

## Sources

- Python 3.12 installed on target machine (verified: `python3 --version` returned 3.12.7)
- Project definition at `.planning/PROJECT.md` (read directly)
- nchook repository description and architecture from project context (describes kqueue + sqlite3 + plistlib + subprocess approach)
- Python standard library documentation for `sqlite3`, `plistlib`, `select`, `urllib.request` (training data, MEDIUM confidence on exact API details -- verified against known Python 3.12 stdlib surface)
- macOS Sequoia notification DB path from project requirements: `~/Library/Group Containers/group.com.apple.usernoted/db2/db`

### Confidence Notes

| Claim | Confidence | Basis |
|-------|------------|-------|
| Python 3.12 on this machine | HIGH | Verified via `python3 --version` |
| `plistlib.loads()` handles binary plists | HIGH | Stdlib since Python 3.4, well-documented behavior |
| `select.kqueue` available on macOS Python | HIGH | BSD subsystem, core to nchook's proven approach |
| `sqlite3` URI mode (`mode=ro`) available | HIGH | Added in Python 3.4, well-documented |
| `urllib.request` sufficient for webhook POST | HIGH | Basic stdlib HTTP, proven pattern |
| macOS Sequoia ships Python 3.12+ via Xcode CLT | MEDIUM | Training data; confirmed 3.12.7 is installed but didn't verify it's from Xcode CLT specifically |
| SQLite version on Sequoia is 3.43+ | MEDIUM | Training data about macOS bundled SQLite versions; not verified directly |
| No external dependencies needed | HIGH | All functionality maps to stdlib modules; verified each module exists in Python 3.12 |

---
*Stack research for: macOS Teams notification interception daemon*
*Researched: 2026-02-11*
