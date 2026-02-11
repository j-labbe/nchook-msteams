#!/usr/bin/env python3
"""
nchook - macOS Notification Center Database Watcher

Core engine for intercepting macOS notification center database records.
Provides startup validation, DB access, binary plist parsing, state
persistence, and DB purge detection.

Uses ONLY Python standard library modules (no external dependencies).
"""

import os
import sys
import sqlite3
import plistlib
import json
import logging
import tempfile
import pathlib
import time
import select
import signal
import subprocess
import argparse
import urllib.request
import urllib.error
import re

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COCOA_TO_UNIX_OFFSET = 978307200  # seconds between Unix epoch (1970) and Cocoa epoch (2001)
STATE_FILE = "state.json"
POLL_FALLBACK_SECONDS = 5.0  # fallback poll interval when kqueue misses events

# Module-level flag for event loop control (signal handler sets False for graceful shutdown)
running = True


def _shutdown_handler(signum, frame):
    """Signal handler for SIGINT/SIGTERM: set running=False for graceful loop exit."""
    global running
    logging.info("Received %s, initiating shutdown...", signal.Signals(signum).name)
    running = False

# ---------------------------------------------------------------------------
# DB Path Detection
# ---------------------------------------------------------------------------

def detect_db_path():
    """
    Detect the macOS notification center database path.

    Checks Sequoia+ path first, falls back to legacy path via getconf.
    Returns (db_path, wal_path) tuple.
    Exits with clear error if neither path exists.
    """
    # Sequoia+ path (macOS 15+)
    sequoia_db = os.path.expanduser(
        "~/Library/Group Containers/group.com.apple.usernoted/db2/db"
    )
    if os.path.exists(sequoia_db):
        wal_path = sequoia_db + "-wal"
        return (sequoia_db, wal_path)

    # Legacy path (pre-Sequoia) via getconf DARWIN_USER_DIR
    try:
        result = subprocess.run(
            ["getconf", "DARWIN_USER_DIR"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            darwin_user_dir = result.stdout.strip()
            legacy_db = os.path.join(
                darwin_user_dir, "com.apple.notificationcenter", "db2", "db"
            )
            if os.path.exists(legacy_db):
                wal_path = legacy_db + "-wal"
                return (legacy_db, wal_path)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    logging.error("Notification database not found.")
    logging.error(
        "Checked Sequoia+ path: %s",
        sequoia_db,
    )
    logging.error(
        "This daemon requires macOS Sequoia (15+) or later with an active "
        "notification center database."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# FDA Validation + DB Open
# ---------------------------------------------------------------------------

def validate_environment(db_path):
    """
    Validate Full Disk Access and open the notification database read-only.

    Attempts to connect and read from the database. On FDA failure, prints
    actionable instructions and exits. Verifies expected schema (record, app
    tables).

    Returns an open sqlite3.Connection on success.
    """
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row

        # Verify actual read access (not just file open)
        cursor = conn.execute("SELECT COUNT(*) FROM record")
        count = cursor.fetchone()[0]
        logging.info("FDA check passed. %d records in notification DB.", count)

    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e) and os.path.exists(db_path):
            logging.error("Cannot read notification database.")
            logging.error("Full Disk Access is required.")
            logging.error("")
            logging.error("To grant access:")
            logging.error(
                "  1. Open System Settings > Privacy & Security > Full Disk Access"
            )
            logging.error("  2. Click the + button")
            logging.error(
                "  3. Add your terminal app (Terminal.app, iTerm2, etc.)"
            )
            logging.error("  4. Restart the daemon")
            sys.exit(1)
        raise

    # Verify expected schema
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        if "record" not in tables or "app" not in tables:
            logging.error("Unexpected DB schema. Tables found: %s", tables)
            logging.error("Expected 'record' and 'app' tables.")
            conn.close()
            sys.exit(1)
    except sqlite3.OperationalError as e:
        logging.error("Schema validation failed: %s", e)
        conn.close()
        sys.exit(1)

    return conn


# ---------------------------------------------------------------------------
# Binary Plist Parsing
# ---------------------------------------------------------------------------

def parse_notification(raw_data):
    """
    Parse a binary plist blob from the notification DB into a dict.

    CRITICAL: Fields are nested under the "req" key.
      - titl, subt, body are at plist["req"]["key"]
      - app and date are TOP LEVEL

    Returns dict with keys: app, title, subtitle, body, timestamp
    Returns None on any parse failure.
    """
    try:
        plist = plistlib.loads(raw_data, fmt=plistlib.FMT_BINARY)
    except Exception:
        # Retry without explicit format (auto-detect)
        try:
            plist = plistlib.loads(raw_data)
        except Exception:
            return None

    req = plist.get("req", {})

    cocoa_date = plist.get("date")
    if cocoa_date is not None:
        timestamp = cocoa_date + COCOA_TO_UNIX_OFFSET
    else:
        timestamp = 0

    return {
        "app": plist.get("app", ""),
        "title": req.get("titl", ""),
        "subtitle": req.get("subt", ""),
        "body": req.get("body", ""),
        "timestamp": timestamp,
    }


# ---------------------------------------------------------------------------
# DB Query
# ---------------------------------------------------------------------------

def query_new_notifications(conn, last_rec_id):
    """
    Query for notifications newer than last_rec_id.

    Joins record with app table to get the bundle identifier.
    Parses each record's binary plist data blob.
    Overrides the app field with the JOIN result (more reliable).

    Returns list of notification dicts, each with an added 'rec_id' key.
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
            (last_rec_id,),
        )
        for row in cursor:
            notif = parse_notification(row["data"])
            if notif is None:
                logging.warning(
                    "Failed to parse plist for rec_id=%s", row["rec_id"]
                )
                continue
            # Override app with the JOIN result (more reliable than plist app field)
            notif["app"] = row["identifier"]
            notif["rec_id"] = row["rec_id"]
            results.append(notif)
    except sqlite3.OperationalError as e:
        logging.error("DB query failed: %s", e)
    return results


# ---------------------------------------------------------------------------
# State Persistence
# ---------------------------------------------------------------------------

def save_state(last_rec_id, state_path=STATE_FILE):
    """
    Atomically persist last_rec_id to a JSON state file.

    Uses write-then-replace pattern:
      1. Write to temp file in same directory
      2. fsync to ensure data is on disk
      3. os.replace() for atomic rename (POSIX guarantee)

    Logs error but does not crash if state save fails.
    """
    state = {"last_rec_id": last_rec_id}
    dir_name = os.path.dirname(os.path.abspath(state_path))
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=dir_name, suffix=".tmp", delete=False
        ) as tmp:
            json.dump(state, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, os.path.abspath(state_path))
    except OSError as e:
        logging.error("Failed to save state to %s: %s", state_path, e)
        # Clean up temp file if it exists
        try:
            os.unlink(tmp_path)
        except (OSError, UnboundLocalError):
            pass


def load_state(state_path=STATE_FILE):
    """
    Load persisted last_rec_id from JSON state file.

    Returns the integer last_rec_id, defaulting to 0 if:
      - File does not exist (fresh start)
      - File is corrupted (JSON decode error)
      - File is missing the expected key
    """
    try:
        with open(state_path, "r") as f:
            data = json.load(f)
            return int(data.get("last_rec_id", 0))
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# DB Purge Detection
# ---------------------------------------------------------------------------

def check_db_consistency(conn, persisted_rec_id):
    """
    Detect DB purge by comparing persisted rec_id against MAX(rec_id).

    If the DB has been purged (max rec_id < persisted rec_id), resets
    state to 0 with a warning.

    Returns adjusted rec_id (either unchanged or reset to 0).
    """
    cursor = conn.execute("SELECT MAX(rec_id) FROM record")
    row = cursor.fetchone()
    max_rec_id = row[0] if row[0] is not None else 0

    if persisted_rec_id > max_rec_id:
        logging.warning(
            "DB purge detected: persisted rec_id=%d > max DB rec_id=%d. "
            "Resetting state.",
            persisted_rec_id,
            max_rec_id,
        )
        return 0
    return persisted_rec_id


# ---------------------------------------------------------------------------
# Startup Summary
# ---------------------------------------------------------------------------

def print_startup_summary(db_path, last_rec_id, config=None, dry_run=False):
    """
    Print formatted startup summary banner.

    Shows DB path, FDA status (always OK at this point since validation
    passed), last rec_id, config details, and dry-run mode indicator.
    """
    logging.info("=" * 60)
    logging.info("Teams Notification Interceptor")
    logging.info("=" * 60)
    logging.info("  DB path:     %s", db_path)
    logging.info("  FDA status:  OK")
    logging.info("  Last rec_id: %d", last_rec_id)
    if config is not None:
        logging.info("  Webhook URL: %s", config.get("webhook_url", "NOT SET"))
        logging.info("  Bundle IDs:  %s", ", ".join(sorted(config.get("bundle_ids", []))))
        logging.info("  Poll interval: %.1fs", config.get("poll_interval", POLL_FALLBACK_SECONDS))
        logging.info("  Log level:   %s", config.get("log_level", "INFO"))
    if dry_run:
        logging.info("  Mode:        DRY-RUN (no HTTP requests)")
    logging.info("=" * 60)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_FILE = "config.json"

DEFAULT_CONFIG = {
    "bundle_ids": ["com.microsoft.teams2", "com.microsoft.teams"],
    "poll_interval": 5.0,
    "log_level": "INFO",
    "webhook_timeout": 10,
}


def load_config(config_path=None):
    """
    Load JSON config file with defaults. Exits if file missing or invalid.

    Resolves config_path relative to the script's directory if not provided,
    ensuring the daemon finds config.json regardless of CWD.

    Validates that webhook_url is present and non-empty.
    Converts bundle_ids list to a set for O(1) lookup.

    Returns the merged config dict.
    """
    if config_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, CONFIG_FILE)

    config = dict(DEFAULT_CONFIG)

    try:
        with open(config_path, "r") as f:
            user_config = json.load(f)
    except FileNotFoundError:
        logging.error("Config file not found: %s", config_path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error("Config file invalid JSON: %s", e)
        sys.exit(1)

    config.update(user_config)

    if "webhook_url" not in config or not config["webhook_url"]:
        logging.error("Config missing required field: webhook_url")
        sys.exit(1)

    # Convert bundle_ids to set for O(1) lookup
    config["bundle_ids"] = set(config["bundle_ids"])

    return config


# ---------------------------------------------------------------------------
# Teams Filtering
# ---------------------------------------------------------------------------

# Known noise patterns in Teams notification body text (English locale).
# These are notification types that are NOT real chat messages.
NOISE_PATTERNS = [
    # Reactions
    "Liked",
    "Loved",
    "Laughed at",
    "Was surprised by",
    "Was sad at",
    # Call notifications
    "is calling you",
    "Missed call from",
    "Incoming call",
    # Meeting notifications
    "joined the meeting",
    "left the meeting",
    "Meeting started",
    "is presenting",
    # Join/leave events
    "has been added",
    "has left",
    "has joined",
    # Typing indicators (if they surface as notifications)
    "is typing",
]

# Sentence-ending punctuation that suggests a message is complete (not truncated)
SENTENCE_ENDINGS = frozenset(".!?\"')")


def passes_bundle_id_filter(notif, bundle_ids):
    """FILT-01: Only Teams bundle IDs pass."""
    return notif["app"] in bundle_ids


def passes_allowlist_filter(notif):
    """FILT-02: Require both sender (title) and body to be present and non-empty."""
    return bool(notif.get("title", "").strip()) and bool(notif.get("body", "").strip())


def is_system_alert(notif):
    """FILT-03: Reject notifications where title is 'Microsoft Teams'."""
    return notif.get("title", "").strip() == "Microsoft Teams"


def is_noise_notification(body, title):
    """FILT-04: Reject known noise patterns in body text."""
    body_stripped = body.strip()
    for pattern in NOISE_PATTERNS:
        if body_stripped.startswith(pattern) or body_stripped == pattern:
            return True
    return False


def passes_filter(notif, config):
    """
    Complete four-stage filter chain.

    Stage 1: Bundle ID match (FILT-01)
    Stage 2: Allowlist -- require sender and body (FILT-02)
    Stage 3: System alert rejection (FILT-03)
    Stage 4: Noise pattern rejection (FILT-04)

    Returns True if notification passes all stages.
    """
    if not passes_bundle_id_filter(notif, config["bundle_ids"]):
        return False
    if not passes_allowlist_filter(notif):
        return False
    if is_system_alert(notif):
        return False
    if is_noise_notification(notif.get("body", ""), notif.get("title", "")):
        return False
    return True


def classify_notification(notif):
    """
    FILT-05: Classify notification type based on content and subtitle patterns.

    Returns one of: "direct_message", "channel_message", "mention"

    Heuristic (English locale):
    - Body contains "@" -> mention
    - Subtitle contains "|" or ">" separator -> channel_message
    - Subtitle present and differs from title -> channel_message
    - Default: direct_message
    """
    subtitle = notif.get("subtitle", "").strip()
    body = notif.get("body", "")

    # Check for @mention patterns in body
    if "@" in body:
        return "mention"

    # Subtitle with separator pattern indicates channel message
    if subtitle and ("|" in subtitle or ">" in subtitle):
        return "channel_message"

    # Subtitle present and differs from title indicates group/channel context
    title = notif.get("title", "").strip()
    if subtitle and subtitle != title:
        return "channel_message"

    # Default: direct message
    return "direct_message"


def detect_truncation(body):
    """
    WEBH-04: Detect likely truncated messages.

    Heuristic: body is >= 150 characters AND does not end with sentence-ending
    punctuation. macOS notification preview truncates long messages at approximately
    150 characters without adding an ellipsis.

    Returns True if likely truncated, False otherwise.
    """
    if len(body) < 150:
        return False
    if body and body[-1] in SENTENCE_ENDINGS:
        return False
    return True


# ---------------------------------------------------------------------------
# Webhook Delivery
# ---------------------------------------------------------------------------

def build_webhook_payload(notif, msg_type):
    """
    WEBH-02, DBWT-06: Build JSON-serializable webhook payload from notification.

    Returns dict with: senderName, chatId, content, timestamp, type,
    subtitle, _source, _truncated.
    """
    ts = notif.get("timestamp", 0)
    if ts > 0:
        ts_formatted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    else:
        ts_formatted = None

    return {
        "senderName": notif.get("title", ""),
        "chatId": notif.get("subtitle", ""),
        "content": notif.get("body", ""),
        "timestamp": ts_formatted,
        "type": msg_type,
        "subtitle": notif.get("subtitle", ""),
        "_source": "macos-notification-center",
        "_truncated": detect_truncation(notif.get("body", "")),
    }


def post_webhook(payload, webhook_url, timeout=10):
    """
    WEBH-01, WEBH-03: POST JSON payload to webhook URL.

    Returns True on success, False on any failure.
    Never raises -- all exceptions are logged and skipped.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        logging.info("Webhook delivered: HTTP %d (%d bytes sent)", resp.status, len(data))
        return True
    except urllib.error.HTTPError as e:
        logging.warning("Webhook HTTP error: %d %s (url=%s)", e.code, e.reason, webhook_url)
    except urllib.error.URLError as e:
        logging.warning("Webhook connection error: %s (url=%s)", e.reason, webhook_url)
    except TimeoutError:
        logging.warning("Webhook timed out after %ds (url=%s)", timeout, webhook_url)
    except Exception as e:
        logging.warning("Webhook unexpected error: %s (url=%s)", str(e), webhook_url)
    return False


# ---------------------------------------------------------------------------
# Status Detection
# ---------------------------------------------------------------------------

def _detect_idle_time():
    """
    STAT-01: Detect system idle time via ioreg HIDIdleTime.

    Returns idle time in seconds (float), or None if detection fails.
    """
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        logging.warning("ioreg timed out")
        return None
    except FileNotFoundError:
        logging.error("ioreg not found")
        return None

    if result.returncode != 0:
        logging.warning("ioreg returned non-zero exit code: %d", result.returncode)
        return None

    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
    if not match:
        logging.warning("HIDIdleTime not found in ioreg output")
        return None

    nanoseconds = int(match.group(1))
    return nanoseconds / 1_000_000_000  # convert nanoseconds to seconds


def _detect_teams_process():
    """
    STAT-02: Detect whether Microsoft Teams is running via pgrep.

    Checks "MSTeams" first (new Teams), then "Microsoft Teams" (legacy)
    for backward compatibility.

    Returns True if Teams is running, False otherwise.
    """
    for process_name in ["MSTeams", "Microsoft Teams"]:
        try:
            result = subprocess.run(
                ["pgrep", "-x", process_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return False


def _detect_status_ax():
    """Placeholder for AX status detection (Phase 6). Always returns None."""
    return None


_AX_STATUS_MAP = {
    "available": "Available",
    "busy": "Busy",
    "do not disturb": "DoNotDisturb",
    "in a meeting": "Busy",
    "in a call": "Busy",
    "presenting": "Busy",
    "away": "Away",
    "be right back": "BeRightBack",
    "appear offline": "Offline",
    "offline": "Offline",
    "out of office": "Offline",
}


def _normalize_ax_status(raw):
    """STAT-04 prep: Normalize raw AX status text to canonical status value."""
    return _AX_STATUS_MAP.get(raw.lower().strip(), "Unknown")


# ---------------------------------------------------------------------------
# kqueue WAL Watcher
# ---------------------------------------------------------------------------

def create_wal_watcher(wal_path):
    """
    Create a kqueue watcher on the WAL file.

    Monitors for WRITE (new data), DELETE, and RENAME events.
    DELETE/RENAME happen when SQLite checkpoints the WAL.

    Returns (kq, fd, kevent) tuple.
    """
    kq = select.kqueue()
    fd = os.open(wal_path, os.O_RDONLY)
    kev = select.kevent(
        fd,
        filter=select.KQ_FILTER_VNODE,
        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
        fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME,
    )
    return (kq, fd, kev)


# ---------------------------------------------------------------------------
# Event Loop
# ---------------------------------------------------------------------------

def run_watcher(db_path, wal_path, state_path, config=None, dry_run=False):
    """
    Main kqueue event loop that watches the WAL file for changes.

    Queries for new notifications on each kqueue event or poll timeout,
    filters through the pipeline, builds payloads, and POSTs to webhook.
    Persists state after each batch.
    Handles WAL file recreation gracefully (re-registers kqueue).

    Falls back to periodic polling when kqueue is unavailable.
    """
    global running

    # Use config poll interval or fallback
    poll_interval = POLL_FALLBACK_SECONDS
    if config is not None:
        poll_interval = config.get("poll_interval", POLL_FALLBACK_SECONDS)

    # Load persisted state
    last_rec_id = load_state(state_path)

    # Open DB read-only
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row

    # Check DB consistency (detect purges)
    last_rec_id = check_db_consistency(conn, last_rec_id)

    # Print startup banner
    print_startup_summary(db_path, last_rec_id, config, dry_run=dry_run)

    # Set up kqueue if WAL exists
    kq = None
    fd = None
    kev = None
    if os.path.exists(wal_path):
        try:
            kq, fd, kev = create_wal_watcher(wal_path)
            logging.info("Watching WAL file via kqueue: %s", wal_path)
        except OSError as e:
            logging.warning("Failed to set up kqueue on WAL: %s. Using poll-only mode.", e)
    else:
        logging.warning("WAL file not found: %s. Using poll-only mode.", wal_path)

    try:
        while running:
            wal_was_deleted = False

            if kq is not None and kev is not None:
                try:
                    events = kq.control([kev], 1, poll_interval)
                    for ev in events:
                        if ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                            wal_was_deleted = True
                except OSError as e:
                    logging.warning("kqueue error (stale FD?): %s", e)
                    wal_was_deleted = True  # Treat as WAL gone, attempt re-register
            else:
                time.sleep(poll_interval)

            # Query for new notifications
            notifications = query_new_notifications(conn, last_rec_id)

            for notif in notifications:
                if config is not None and not passes_filter(notif, config):
                    logging.debug(
                        "Filtered: app=%s title=%s body=%.50s",
                        notif["app"], notif["title"], notif.get("body", ""),
                    )
                    continue

                # Log the notification (keep Phase 1 behavior for non-config mode)
                if notif["timestamp"] > 0:
                    ts_str = time.strftime(
                        "%Y-%m-%dT%H:%M:%S",
                        time.gmtime(notif["timestamp"]),
                    )
                else:
                    ts_str = "unknown"

                logging.info(
                    "Notification | app=%s | title=%s | subtitle=%s | body=%s | time=%s",
                    notif["app"],
                    notif["title"],
                    notif["subtitle"],
                    notif["body"],
                    ts_str,
                )

                # Webhook delivery (only if config with webhook_url)
                if config is not None and config.get("webhook_url"):
                    msg_type = classify_notification(notif)
                    payload = build_webhook_payload(notif, msg_type)
                    if dry_run:
                        logging.info(
                            "DRY-RUN | Would POST to %s:\n%s",
                            config["webhook_url"],
                            json.dumps(payload, indent=2),
                        )
                    else:
                        post_webhook(
                            payload,
                            config["webhook_url"],
                            config.get("webhook_timeout", 10),
                        )

            # Update high-water mark
            if notifications:
                last_rec_id = notifications[-1]["rec_id"]
                save_state(last_rec_id, state_path)

            # Handle WAL delete/rename: re-register kqueue if WAL reappears
            if wal_was_deleted and os.path.exists(wal_path):
                # Close old FD
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                # Close old kqueue
                if kq is not None:
                    try:
                        kq.close()
                    except OSError:
                        pass
                # Re-register
                try:
                    kq, fd, kev = create_wal_watcher(wal_path)
                    logging.info("Re-registered kqueue on new WAL file.")
                except OSError as e:
                    logging.warning(
                        "Failed to re-register kqueue: %s. Falling back to polling.", e
                    )
                    kq = None
                    fd = None
                    kev = None
            elif wal_was_deleted and not os.path.exists(wal_path):
                logging.warning("WAL file deleted and not yet recreated. Falling back to polling.")
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if kq is not None:
                    try:
                        kq.close()
                    except OSError:
                        pass
                kq = None
                fd = None
                kev = None

        # Graceful shutdown: flush final state
        logging.info("Saving state before exit: last_rec_id=%d", last_rec_id)
        save_state(last_rec_id, state_path)
        logging.info("Shutdown complete.")
    finally:
        # Clean up resources
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if kq is not None:
            try:
                kq.close()
            except OSError:
                pass
        conn.close()


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    """
    CLI entry point: parse args, load config, validate environment, start event loop.

    Parses --dry-run flag first (so --help works without config.json), loads
    config, sets log level, detects DB path, validates FDA, then enters the
    kqueue event loop. Handles KeyboardInterrupt for clean shutdown.
    """
    parser = argparse.ArgumentParser(
        description="macOS Teams Notification Interceptor"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log webhook payloads without sending HTTP requests",
    )
    args = parser.parse_args()

    config = load_config()

    # Set log level from config
    logging.getLogger().setLevel(
        getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)
    )

    db_path, wal_path = detect_db_path()

    # Validate environment (FDA, schema) -- returns a connection we don't need
    validation_conn = validate_environment(db_path)
    validation_conn.close()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    try:
        run_watcher(db_path, wal_path, STATE_FILE, config, dry_run=args.dry_run)
    except KeyboardInterrupt:
        logging.info("Shutting down...")


if __name__ == "__main__":
    main()
