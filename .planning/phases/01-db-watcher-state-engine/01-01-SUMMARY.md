---
phase: 01-db-watcher-state-engine
plan: 01
subsystem: database
tags: [sqlite3, plistlib, kqueue, macos, notification-center, state-persistence]

# Dependency graph
requires:
  - phase: none
    provides: "First plan - no prior dependencies"
provides:
  - "nchook.py core module with DB access, plist parsing, state persistence"
  - "detect_db_path() for Sequoia+ and legacy macOS paths"
  - "validate_environment() with FDA validation and schema checks"
  - "parse_notification() with nested req key access for titl/subt/body"
  - "query_new_notifications() joining record and app tables"
  - "save_state()/load_state() with atomic write-then-replace"
  - "check_db_consistency() for DB purge detection"
  - "print_startup_summary() for startup banner"
affects: [01-02-PLAN, 02-01, 02-02]

# Tech tracking
tech-stack:
  added: [sqlite3 URI mode, plistlib FMT_BINARY, tempfile atomic writes]
  patterns: [high-water-mark state tracking, atomic write-then-replace, nested plist key access, read-only DB via URI mode]

key-files:
  created: [nchook.py, .gitignore]
  modified: []

key-decisions:
  - "All state persistence and DB purge detection implemented in same module as startup/parsing (single nchook.py)"
  - "Atomic state writes via tempfile + fsync + os.replace pattern"
  - "Plist parsing accesses nested req key for titl/subt/body, top-level for app/date"
  - "Cocoa-to-Unix timestamp conversion using constant 978307200"

patterns-established:
  - "High-water mark: single last_rec_id integer, query WHERE rec_id > ?"
  - "Atomic file writes: NamedTemporaryFile + flush + fsync + os.replace"
  - "FDA detection: attempt DB read, check if file exists on OperationalError"
  - "Defensive plist parsing: try FMT_BINARY first, fallback to auto-detect"

# Metrics
duration: 3min
completed: 2026-02-11
---

# Phase 1 Plan 1: Core Engine Summary

**Notification DB engine with Sequoia path detection, FDA validation, binary plist parsing (nested req keys), and atomic state persistence using stdlib only**

## Performance

- **Duration:** 3 min
- **Started:** 2026-02-11T18:48:55Z
- **Completed:** 2026-02-11T18:52:19Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Complete nchook.py module with 8 core functions using only Python stdlib
- DB path detection supporting both Sequoia+ Group Containers and legacy getconf paths
- FDA validation with actionable error message guiding users to System Settings
- Binary plist parsing correctly accessing nested req key for titl/subt/body fields
- State persistence using atomic write-then-replace (tempfile + fsync + os.replace)
- DB purge detection comparing persisted rec_id against MAX(rec_id)

## Task Commits

Each task was committed atomically:

1. **Task 1: Create nchook.py with startup validation, DB access, and plist parsing** - `1374ff8` (feat)
2. **Task 2: Add state persistence and DB purge detection** - Included in `1374ff8` (see note below)

_Note: Task 2's state persistence and DB purge detection functions were implemented as part of the nchook.py module in Task 1 because Task 1's verification step explicitly required all 8 functions (including save_state, load_state, check_db_consistency) to be importable. Task 2's verification (round-trip test, missing file test) confirmed correctness._

## Files Created/Modified
- `nchook.py` - Core notification engine: DB path detection, FDA validation, plist parsing, DB queries, state persistence, DB purge detection, startup summary
- `.gitignore` - Excludes state.json, __pycache__/, *.pyc, .DS_Store

## Decisions Made
- Implemented all core functions in a single nchook.py module rather than splitting across files -- matches the project's single-file daemon architecture from PROJECT.md
- Used logging.info for startup summary rather than print() for consistent log formatting
- Added ValueError and TypeError to load_state exception handling for robustness beyond plan spec (Rule 2 - defensive coding)

## Deviations from Plan

None - plan executed exactly as written. Task 2's implementation was naturally included in Task 1 because the plan's Task 1 verification required all functions to exist.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All foundational functions ready for Plan 02 (event loop, kqueue watcher, main entry point)
- Functions designed for the startup sequence: detect_db_path() -> validate_environment() -> load_state() -> check_db_consistency() -> print_startup_summary()
- query_new_notifications() ready to be called from the kqueue event loop
- save_state() ready to be called after processing each batch

## Self-Check: PASSED

- FOUND: nchook.py
- FOUND: .gitignore
- FOUND: 01-01-SUMMARY.md
- FOUND: commit 1374ff8

---
*Phase: 01-db-watcher-state-engine*
*Completed: 2026-02-11*
