# Milestones

## v1.0 MVP (Shipped: 2026-02-11)

**Phases completed:** 3 phases, 5 plans, 9 tasks
**Git range:** `1374ff8..8462797`
**Lines of code:** 849 Python
**Timeline:** ~2 hours

**Key accomplishments:**
- Core notification DB engine with Sequoia path detection, FDA validation, binary plist parsing, and atomic state persistence
- kqueue-driven WAL watcher event loop with 5s fallback polling for near-real-time notification detection
- Four-stage Teams notification filter pipeline (bundle ID, allowlist, system alert, noise rejection) with message classification
- JSON webhook delivery with filter-classify-build-post pipeline wired into event loop
- Graceful SIGINT/SIGTERM shutdown with post-loop state flush and --dry-run CLI flag

**Delivered:** A macOS daemon that watches the Sequoia notification center database, filters to Teams messages, and POSTs structured JSON to a configurable webhook with graceful lifecycle management.

**Archive:** `.planning/milestones/v1.0-ROADMAP.md`, `.planning/milestones/v1.0-REQUIREMENTS.md`

---


## v1.1 Teams Status Integration (Shipped: 2026-02-11)

**Phases completed:** 3 phases (4-6), 3 plans, 6 tasks
**Git range:** `bf614f7..b84910a`
**Lines of code:** 1240 Python (+391 from v1.0)
**Timeline:** ~3 hours

**Key accomplishments:**
- Three-signal fallback chain (AX → idle → process) for user status detection with canonical result dicts
- Status-aware notification gating with fail-open policy (forward on Away/Busy/Unknown, suppress on Available/Offline)
- Pure gate function with always-advance rec_id semantics preventing stale replay on status transitions
- AX permission probe via ctypes AXIsProcessTrusted with graceful degradation to idle+process fallback
- Self-disabling safety net for broken AX trees (3 consecutive failures → auto-disable for session)
- Status metadata (_detected_status, _status_source, _status_confidence) in webhook payloads

**Delivered:** Status-aware notification gating that only forwards Teams notifications when the user is Away or Busy, with a three-signal detection chain (Accessibility tree, system idle time, process check) and graceful degradation when AX permission is unavailable.

**Archive:** `.planning/milestones/v1.1-ROADMAP.md`, `.planning/milestones/v1.1-REQUIREMENTS.md`

---

