# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-11)

**Core value:** Reliably capture every Teams message notification and deliver it as structured JSON to a webhook -- no missed messages, no noise.
**Current focus:** Phase 6 -- AX Discovery and Permission Handling (COMPLETE)

## Current Position

Phase: 6 of 6 (AX Discovery and Permission Handling)
Plan: 1 of 1 in current phase -- COMPLETE
Status: Phase 6 complete -- v1.1 milestone complete
Last activity: 2026-02-11 -- Phase 6 complete, AX permission probe + AppleScript status query + graceful degradation

Progress: [████████░░] 80% (8/10 plans across all milestones; v1.1: 100%)

## Performance Metrics

**Velocity:**
- Total plans completed: 8
- Average duration: 4.3min
- Total execution time: 0.57 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-db-watcher-state-engine | 2/2 | 15min | 7.5min |
| 02-teams-filtering-webhook-delivery | 2/2 | 5min | 2.5min |
| 03-operational-hardening | 1/1 | 3min | 3.0min |
| 04-status-detection-core | 1/1 | 3min | 3.0min |
| 05-config-gating-event-loop-integration | 1/1 | 3min | 3.0min |
| 06-ax-discovery-permission-handling | 1/1 | 4min | 4.0min |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [v1.1 roadmap]: Three phases (4-6) derived from 16 requirements. Phase 7 (hardening) dropped -- all hardening items are v2 deferred.
- [v1.1 roadmap]: AX discovery isolated in Phase 6 because research flags it as high-risk (new Teams AX tree may be broken). Phases 4-5 deliver value without AX.
- [Phase 4]: MSTeams checked before "Microsoft Teams" in process detection for new Teams binary compatibility
- [Phase 4]: Signal functions return None on failure; orchestrator handles fallthrough (no exception propagation)
- [Phase 4]: idle_threshold_seconds config param (default 300) added now to avoid code changes in Phase 5
- [Phase 5]: Hardcoded _FORWARD_STATUSES frozenset (Away, Busy, Unknown) -- configurable policy is SREF-04 v2 deferred
- [Phase 5]: Status check placed before query_new_notifications for efficiency; rec_id always advances (GATE-03)
- [Phase 5]: config.json not modified on disk -- status_enabled defaults from DEFAULT_CONFIG, users opt out manually
- [Phase 6]: ctypes AXIsProcessTrusted over osascript probe: instant boolean vs 30s+ hang when permission denied
- [Phase 6]: AX permission cached at startup (_ax_available): TCC changes require process restart on macOS
- [Phase 6]: 3s osascript timeout (< 5s poll interval) prevents AX query from blocking event loop
- [Phase 6]: Self-disable after 3 consecutive AX failures: handles known broken AX tree in new Teams

### Pending Todos

None.

### Blockers/Concerns

- [Resolved] New Teams AX tree is known broken -- Phase 6 implemented graceful degradation with self-disabling safety net. AX permission probe works; AppleScript query will attempt two candidate paths and self-disable if they fail.

## Session Continuity

Last session: 2026-02-11
Stopped at: Completed 06-01-PLAN.md -- Phase 6 complete, v1.1 milestone complete
Resume file: None
