---
phase: 01-db-watcher-state-engine
plan: 02
subsystem: database
tags: [kqueue, select, sqlite3-wal, macos, notification-center, event-loop, daemon]

# Dependency graph
requires:
  - phase: 01-01
    provides: "nchook.py core module with DB access, plist parsing, state persistence, startup validation"
provides:
  - "Complete runnable daemon: python3 nchook.py"
  - "create_wal_watcher() for kqueue on WAL file with VNODE filter"
  - "run_watcher() event loop with kqueue + fallback polling"
  - "main() CLI entry point with startup sequence and clean shutdown"
  - "WAL delete/rename detection with automatic kqueue re-registration"
  - "Notification logging with ISO 8601 timestamps"
affects: [02-01, 02-02, 03-01]

# Tech tracking
tech-stack:
  added: [select.kqueue, select.kevent, KQ_FILTER_VNODE]
  patterns: [kqueue event loop with poll fallback, WAL file monitoring via VNODE events, graceful kqueue re-registration on WAL recreation]

key-files:
  created: []
  modified: [nchook.py]

key-decisions:
  - "Module-level running flag for future signal handler integration (Phase 3)"
  - "5-second fallback poll interval balances responsiveness with CPU usage"
  - "kqueue re-registration on WAL delete/rename handles SQLite checkpoint edge case"
  - "Logging setup kept at module level (already configured in Plan 01) rather than in main()"

patterns-established:
  - "kqueue WAL watcher: KQ_FILTER_VNODE with NOTE_WRITE | NOTE_DELETE | NOTE_RENAME"
  - "Fallback polling: time.sleep when kqueue unavailable or after WAL deletion"
  - "WAL recreation handling: close old FD + kqueue, re-open, re-register"
  - "Startup sequence: detect_db_path -> validate_environment -> close validation conn -> run_watcher"

# Metrics
duration: 12min
completed: 2026-02-11
---

# Phase 1 Plan 2: Event Loop & CLI Entry Point Summary

**kqueue-driven WAL watcher event loop with 5s fallback polling, notification field logging, and clean CLI entry point composing all Plan 01 engine functions into a runnable daemon**

## Performance

- **Duration:** 12 min
- **Started:** 2026-02-11T18:55:34Z
- **Completed:** 2026-02-11T19:07:51Z
- **Tasks:** 2
- **Files modified:** 1

## Accomplishments
- Complete runnable daemon: `python3 nchook.py` watches notification DB in near-real-time
- kqueue event loop on WAL file with automatic re-registration when WAL is recreated during SQLite checkpoint
- Fallback polling mode (5s interval) when kqueue is unavailable or WAL file missing
- Notification field logging with ISO 8601 timestamps (app, title, subtitle, body, time)
- Clean startup sequence: path detection, FDA validation, state load, consistency check, event loop
- Human-verified against live macOS notification database with all success criteria met

## Task Commits

Each task was committed atomically:

1. **Task 1: Implement kqueue event loop and main entry point** - `af276be` (feat)
2. **Task 2: Verify daemon against live notification database** - Human-verified checkpoint (no code commit)

## Files Created/Modified
- `nchook.py` - Added create_wal_watcher(), run_watcher(), main(), POLL_FALLBACK_SECONDS constant, module-level running flag, and if __name__ guard

## Decisions Made
- Kept logging.basicConfig at module level (already configured by Plan 01) rather than moving into main() -- avoids duplicate setup
- Module-level `running = True` flag designed for Phase 3 signal handler to set False for graceful shutdown
- 5-second fallback poll interval chosen to balance responsiveness with CPU efficiency
- Validation connection closed before run_watcher opens its own connection -- each function manages its own DB lifecycle

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
Full Disk Access must be granted to the terminal app before running the daemon. This was verified during the human checkpoint.

## Next Phase Readiness
- Phase 1 complete: nchook.py is a fully working daemon with all engine functions
- Ready for Phase 2: wrapper script for filtering (Teams-only), webhook delivery, config file loading
- Ready for Phase 3: signal handling (SIGTERM/SIGINT via `running` flag), --dry-run flag, launchd plist

## Self-Check: PASSED

- FOUND: nchook.py
- FOUND: 01-02-SUMMARY.md
- FOUND: commit af276be
- FOUND: all event loop functions importable (run_watcher, main, create_wal_watcher)

---
*Phase: 01-db-watcher-state-engine*
*Completed: 2026-02-11*
