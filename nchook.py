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

def print_startup_summary(db_path, last_rec_id):
    """
    Print formatted startup summary banner.

    Shows DB path, FDA status (always OK at this point since validation
    passed), and last rec_id.
    """
    logging.info("=" * 60)
    logging.info("Teams Notification Interceptor")
    logging.info("=" * 60)
    logging.info("  DB path:     %s", db_path)
    logging.info("  FDA status:  OK")
    logging.info("  Last rec_id: %d", last_rec_id)
    logging.info("=" * 60)
