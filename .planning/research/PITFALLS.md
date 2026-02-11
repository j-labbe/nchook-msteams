# Domain Pitfalls

**Domain:** Status detection integration for macOS notification daemon (v1.1 milestone)
**Researched:** 2026-02-11
**Confidence:** MEDIUM (AppleScript AX behavior verified via multiple community sources; ioreg parsing verified via Apple Developer Forums; subprocess integration based on Python docs + training data)

**Context:** Adding three-signal status detection (AppleScript AX tree, ioreg HIDIdleTime, pgrep process check) to an existing 849 LOC Python daemon that uses a kqueue + fallback-poll event loop. All external checks run via `subprocess.run()` / `subprocess.check_output()`. This file covers pitfalls specific to ADDING these features, not the existing v1.0 daemon pitfalls (those are addressed and shipped).

---

## Critical Pitfalls

Mistakes that cause the daemon to hang, miss notifications, or silently stop working.

### Pitfall 1: subprocess.run() Blocks the kqueue Event Loop

**What goes wrong:**
The daemon's event loop uses `kq.control([kev], 1, poll_interval)` with a 5-second timeout. When a notification arrives, the loop processes it -- and if status detection calls `subprocess.run(["osascript", ...])` synchronously, the entire event loop blocks until osascript returns. AppleScript Accessibility tree walks can take 500ms-3s for a complex app like Teams. ioreg is faster (~50-100ms) but still blocks. During this blocking window, kqueue events are buffered but not consumed, and if multiple notifications arrive in a burst (common in meeting chats), each notification triggers a sequential subprocess call. Ten notifications times 2 seconds of AppleScript = 20 seconds of blocked event loop. New kqueue events queue but the daemon appears frozen.

**Why it happens:**
Developers think "subprocess is fast, it's just a quick shell command." They're correct for ioreg (~50ms) but wrong for osascript with AX tree walking (500ms-3000ms). The problem compounds during notification bursts because status is checked per-notification rather than cached.

**Consequences:**
- Notifications pile up; processing latency jumps from <100ms to 20+ seconds during bursts
- If webhook POST also blocks (up to 10s timeout per current code), total blocking is subprocess + webhook per notification
- kqueue events may be lost if the OS event buffer fills (unlikely but possible under sustained load)
- User perceives the daemon as "laggy" or "stuck"

**Prevention:**
- Cache status results with a TTL. Status changes slowly (minutes, not seconds). A 30-60 second cache means at most one subprocess call per TTL period, not one per notification.
- Check status ONCE per poll cycle (before processing the notification batch), not per notification. The status is the same for all notifications in a single batch.
- Always use `timeout=` on every `subprocess.run()` call. Never call subprocess without a timeout.
- Consider the call ordering: check status first (cheap cached lookup), skip the batch if Available, only then process individual notifications. This avoids doing per-notification work that will be dropped anyway.

**Detection:**
- Log timestamps around subprocess calls; any call >500ms warrants investigation
- Monitor notification processing latency; sudden spikes correlate with status checks

**Phase to address:** First implementation plan -- the caching and "check once per cycle" pattern must be the architectural decision before any subprocess code is written.

---

### Pitfall 2: osascript Hangs Indefinitely When Teams Window Is Not Available

**What goes wrong:**
The AppleScript to read Teams status walks the Accessibility tree via System Events. If Teams is not running, is minimized with no windows, is in a broken UI state, or is displaying a modal dialog (e.g., update prompt), the AppleScript may hang waiting for a response that never comes. AppleScript's `with timeout` statement only applies to commands sent to application objects, not to all operations. The `osascript` process blocks forever, and because `subprocess.run()` without `timeout=` blocks the caller, the daemon hangs permanently.

**Why it happens:**
AppleScript's timeout semantics are counter-intuitive. The `with timeout of N seconds` construct does NOT apply universally -- it only works for Apple Events sent to applications, not for System Events UI element access. Developers test when Teams is running and healthy, never when it is absent, hung, or showing unexpected UI. The osascript process waits for a response from the AX framework that may never arrive.

**Consequences:**
- Daemon hangs permanently (requires kill -9 to recover)
- No notifications processed until daemon is restarted
- No error message -- the process is just stuck in subprocess.run()

**Prevention:**
- ALWAYS pass `timeout=5` (or similar) to `subprocess.run()`. This is non-negotiable.
- Catch `subprocess.TimeoutExpired` explicitly and treat it as "AX unavailable, fall back to next signal."
- Before running osascript, check if Teams is running via pgrep first (fast, <10ms). If Teams is not running, skip osascript entirely.
- Structure the code so that subprocess timeout is the OUTER defense and a pre-check is the INNER optimization:
  ```python
  def get_ax_status():
      # Fast pre-check
      if not is_teams_running():
          return None  # skip osascript entirely
      try:
          result = subprocess.run(
              ["osascript", "-e", SCRIPT],
              capture_output=True, text=True, timeout=5
          )
          ...
      except subprocess.TimeoutExpired:
          logging.warning("osascript timed out (Teams UI unresponsive?)")
          return None  # fall back
  ```

**Detection:**
- Log every subprocess timeout at WARNING level
- Track consecutive timeouts; 3+ in a row suggests Teams is in a broken state

**Phase to address:** First implementation plan -- the timeout parameter is line 1 of the subprocess call, not an afterthought.

---

### Pitfall 3: Accessibility Permission Grants to the Wrong App (Terminal vs. osascript vs. Python)

**What goes wrong:**
macOS Accessibility permission is granted to the PARENT APPLICATION that spawns the process, not to osascript itself. When the daemon runs from Terminal.app, it is Terminal.app that needs Accessibility permission. When run from iTerm2, it is iTerm2. When run via launchd (future), the permission target changes again. Developers grant permission to one terminal, test successfully, then the daemon fails silently when launched from a different context. The error is `"osascript is not allowed assistive access"` on stderr, returncode 1 -- but if stderr is not checked, this looks like "no status found" rather than "permission denied."

**Why it happens:**
macOS Accessibility permissions are per-application, determined by the code-signed bundle of the process that makes the AX calls. When osascript is launched as a child of Terminal.app, it inherits Terminal.app's Accessibility grant. This is documented but not obvious. Developers test in their usual terminal and forget that the permission is terminal-specific.

**Consequences:**
- AX signal always returns None/error, daemon permanently falls back to idle+process signals
- User doesn't realize Accessibility permission is needed (they already granted FDA for v1.0)
- Different from FDA: FDA is for file access, Accessibility is for UI element inspection. Users must grant BOTH permissions, to potentially different apps.

**Prevention:**
- On startup, attempt a minimal AX probe (e.g., `osascript -e 'tell application "System Events" to get name of first process'`). If this returns error 1 with "not allowed assistive access" in stderr, print actionable instructions: "Accessibility permission required. Go to System Settings > Privacy & Security > Accessibility and add [detected terminal app]."
- Detect the parent terminal: `os.environ.get("TERM_PROGRAM", "your terminal")` gives "Apple_Terminal", "iTerm.app", "vscode", etc. Include this in the error message.
- Distinguish between "Accessibility not granted" (fixable, print instructions) and "Teams not running" (normal, fall through silently). Check stderr content, not just return code.
- Log at startup whether AX signal is available or degraded: "AX status: AVAILABLE" vs "AX status: UNAVAILABLE (Accessibility permission not granted for iTerm2)"

**Detection:**
- osascript returncode == 1 AND stderr contains "not allowed assistive access"
- Status detection always uses fallback signals, never AX, despite Teams being visible on screen

**Phase to address:** First implementation plan -- the AX probe should run at startup alongside FDA validation. Print clear instructions for both permissions.

---

### Pitfall 4: New Teams (com.microsoft.teams2) Has a Broken Accessibility Tree

**What goes wrong:**
The "new" Microsoft Teams on macOS (bundle ID `com.microsoft.teams2`) has known issues with its Accessibility tree. Community reports indicate the tree is malformed: parent-child relationships are inconsistent (element A lists B as child, but B's parent is C, not A). The tree traversal that worked on the old Electron-based Teams may return incorrect elements, wrong text, or fail entirely on the new native Teams. Worse, this can change between Teams updates without notice.

**Why it happens:**
The new Teams was rewritten from Electron to a native macOS app. The Accessibility tree implementation was not prioritized. Microsoft Community Hub discussions (thread 4033014) confirm that the old method of exposing the AX tree via a call to Teams no longer works in the new version. The tree structure is app-version-dependent and not under our control.

**Consequences:**
- AppleScript returns wrong status text or empty string
- AX signal appears to work (no error) but returns incorrect data
- Status detection makes wrong gating decisions (drops notifications when user is actually Away)
- Silent data loss -- the worst kind of bug

**Prevention:**
- Validate AX results against known status strings: {"Available", "Away", "Busy", "Do not disturb", "Be right back", "Offline", "Out of office"}. If the extracted text is not in this set, treat the result as UNKNOWN and fall back.
- Never trust AX output blindly. Log every extracted status string at DEBUG level so you can audit what's being read from the tree.
- Build the AX script to be specific about the UI path (e.g., targeting a specific window role/subrole) rather than using `entire contents` which will break on tree changes.
- Prepare for the AX signal to be permanently unavailable for new Teams. The fallback chain exists for exactly this reason. Design the system so it works well WITHOUT AX, and AX is a nice-to-have enhancement.
- Pin the AppleScript to specific UI element paths discovered via Accessibility Inspector, and document how to re-discover the path when Teams updates.

**Detection:**
- AX returns text not in the known status set
- AX returns empty string despite Teams being visible and running
- Status confidence is always "low" (never AX-sourced) despite permissions being correct

**Phase to address:** Research phase (before first implementation) -- verify the AX tree structure with Accessibility Inspector on the actual Teams version installed. If the tree is broken, consider whether AX signal is viable at all or if idle+process is sufficient.

---

### Pitfall 5: ioreg HIDIdleTime Output Format Assumptions Break Silently

**What goes wrong:**
The ioreg command output for HIDIdleTime looks like:
```
    |   "HIDIdleTime" = 4523456789
```
Parsing this with regex (e.g., `re.search(r'HIDIdleTime.*?(\d+)', output)`) seems simple, but several things can go wrong:
1. **The number is in nanoseconds**, not seconds or milliseconds. Forgetting to divide by 1,000,000,000 gives astronomically wrong idle times.
2. **The output format includes leading whitespace, pipe characters, and quotes** that can vary between macOS versions. A rigid regex breaks if Apple changes the formatting.
3. **ioreg may not include HIDIdleTime at all** if IOHIDSystem is not loaded (rare, but possible on headless/remote setups or after sleep/wake).
4. **Integer overflow in Python is not a concern** (Python handles arbitrary precision), but downstream JSON serialization may truncate the raw nanosecond value if it exceeds 64-bit float precision (~9.007 * 10^15 nanoseconds = ~104 days).
5. **The HIDIdleTime key appears multiple times** in ioreg output. Naive parsing may grab the wrong one.

**Why it happens:**
Developers test by running `ioreg -c IOHIDSystem | grep HIDIdleTime` in Terminal, see the output, write a regex, and assume stability. They don't handle the "key not found" case, the "wrong instance" case, or the unit conversion correctly.

**Consequences:**
- Idle time always reads as 0 (wrong regex captures nothing, default is 0) -- daemon thinks user is always active, never forwards notifications
- Idle time calculated in wrong units -- 4.5 billion "seconds" instead of 4.5 seconds, daemon thinks user has been idle for 142 years
- ioreg output changes on macOS update, regex stops matching, idle signal silently fails

**Prevention:**
- Use a robust regex: `r'"HIDIdleTime"\s*=\s*(\d+)'` -- this handles whitespace variations around the `=` sign.
- Always divide by 1_000_000_000 to convert nanoseconds to seconds. Add a comment explaining the unit.
- Handle `None` from `re.search()` -- if the regex doesn't match, return None (signal unavailable), do not default to 0.
- Use `ioreg -c IOHIDSystem -d 4` with `-d` flag to limit depth and reduce output size / parsing ambiguity.
- Add a sanity check: if idle_seconds > 86400 (more than one day), something is probably wrong. Log a warning.
- Unit test the parser with real ioreg output captured from the target machine.

**Detection:**
- Idle signal always returns 0 or always returns a huge number
- Status decisions never use idle signal (always falls through to process check)
- After macOS update, idle detection stops working

**Phase to address:** First implementation plan -- the parser function should be written with explicit unit conversion, regex robustness, and None handling from the start.

---

## Moderate Pitfalls

Mistakes that cause incorrect behavior or degraded reliability but don't hang the daemon.

### Pitfall 6: Fallback Chain Produces Inconsistent Status During Transitions

**What goes wrong:**
The three-signal chain (AX -> idle+process -> process-only) produces status at different confidence levels. During status transitions (user returns to computer, Teams auto-changes from Away to Available), the signals disagree:
- AX says "Available" (updated instantly by Teams)
- HIDIdleTime still shows >300 seconds (hasn't been reset yet because user moved mouse but timer updates lag)
- pgrep says "running" (no change)

If AX fails (timeout) during this transition, the daemon falls to idle+process, which still reads the stale idle value, and concludes the user is Away. It forwards a notification that should have been dropped (user is actively looking at Teams).

**Why it happens:**
The three signals have different update latencies. AX reflects Teams' internal state (near-instant). HIDIdleTime reflects OS-level input (updates within milliseconds of input, but the subprocess call to read it introduces latency). pgrep is binary (running/not running) and says nothing about status. During transitions, signals are temporarily contradictory.

**Prevention:**
- When falling back from AX to idle+process, add a brief grace period. If AX was recently available (within last 60 seconds) but just timed out, treat the status as UNKNOWN rather than inferring from stale signals.
- Never combine AX status with idle time in the same decision. Use one signal chain, not a hybrid: AX result is authoritative when available, idle+process is the backup, not a cross-check.
- Add hysteresis to status transitions: require the same status from 2 consecutive checks before changing the gating decision. This prevents flip-flopping during transitions.
- Log every status change with the source signal so you can audit transition behavior.

**Detection:**
- Notifications forwarded/dropped incorrectly in the minutes after the user returns or leaves
- Status flips rapidly between Available and Away in logs (oscillation)
- Webhook payloads show `status_source: "idle"` immediately after a `status_source: "ax"` reading

**Phase to address:** Second implementation plan (after basic signals work) -- hysteresis and grace periods are refinements on top of working signal collection.

---

### Pitfall 7: pgrep Matches Wrong Process or Multiple Processes

**What goes wrong:**
`pgrep -x "Microsoft Teams"` or `pgrep -f "com.microsoft.teams"` can match:
1. Multiple processes (Teams spawns helper processes with similar names)
2. The wrong process entirely (pgrep without `-x` does substring matching: "Microsoft Teams Helper" matches "Microsoft Teams")
3. Zero processes even when Teams is running (if the process name doesn't match expectations -- Teams renamed the process in an update)

On macOS, the new Teams (`com.microsoft.teams2`) may have a different process name than expected. The process might be "Microsoft Teams" or "Microsoft Teams (work or school)" or just "Teams" depending on version.

**Why it happens:**
Developers run `pgrep -x "Microsoft Teams"` on their machine, see a PID, and hardcode the name. They don't test with Teams Helper processes, Teams PWA instances, or after a Teams update that changes the process name.

**Consequences:**
- Process check says "running" when Teams is actually closed (matched a helper process)
- Process check says "not running" when Teams is open (process name changed)
- Fallback chain produces wrong status

**Prevention:**
- Use `pgrep -x` for exact matching. Test the exact process name on the target machine.
- Make the process name configurable in config.json alongside bundle IDs.
- Better: use `pgrep -f "Contents/MacOS/.*[Tt]eams"` to match the executable path, which is more stable than the display name.
- Even better: instead of pgrep, check for a running process by bundle ID. On macOS, `osascript -e 'tell application "System Events" to (name of processes) contains "Microsoft Teams"'` works but requires Accessibility permission (which you may already have for AX status). If Accessibility is not available, fall back to pgrep.
- When pgrep returns multiple PIDs, treat as "running" (any match is sufficient for "Teams is running").

**Detection:**
- Process check reports "running" when Teams dock icon is absent
- Process check reports "not running" when Teams is visibly open
- pgrep output contains multiple PIDs (log and investigate which processes matched)

**Phase to address:** First implementation plan -- decide the process detection strategy (pgrep vs. osascript check) and the exact match pattern before writing the code.

---

### Pitfall 8: Status Cache Stale After Sleep/Wake Cycle

**What goes wrong:**
The daemon caches status with a 30-60 second TTL. When the Mac goes to sleep (lid close, idle sleep), the daemon is suspended by the OS. On wake, the cache TTL appears still valid (wall clock jumped, but the cache timestamp was set before sleep). The cached status is stale -- user may have been Away for hours -- but the daemon uses the cached "Available" status and drops the first batch of notifications after wake.

Similarly, HIDIdleTime resets on wake (user opened lid = physical input), so the idle signal says "active" even though Teams may still show Away until it syncs.

**Why it happens:**
Developers test with the Mac always awake. Sleep/wake introduces time discontinuities that invalidate assumptions about cache freshness. `time.monotonic()` continues to advance during sleep on macOS (unlike some other platforms), so a cache check using monotonic time may or may not catch the gap depending on sleep duration vs. TTL.

**Prevention:**
- Use `time.monotonic()` for cache timing, not `time.time()`. On macOS, `time.monotonic()` is based on `mach_absolute_time()` which does NOT advance during sleep. This means the cache TTL "pauses" during sleep and naturally expires relative to actual processing time.
- BUT: verify this behavior on your macOS version. The Python docs say monotonic clock "may or may not include time during sleep" and this varies by platform. If it does include sleep time on your macOS version, the cache will appear fresh after sleep when it should be stale.
- Add an explicit invalidation: after any kqueue timeout where no events were received AND more than N minutes of wall-clock time passed, invalidate the status cache. This catches the "woke from sleep, lots of time passed" case.
- On the first poll cycle after a long gap (>2x poll interval), always refresh status before processing.

**Detection:**
- First notification after wake is incorrectly gated
- Logs show cache hit immediately after a long gap between poll cycles

**Phase to address:** Second implementation plan -- sleep/wake handling is a hardening concern, not a first-pass requirement. But the cache timing choice (monotonic vs wall clock) should be correct from the start.

---

### Pitfall 9: AppleScript `entire contents` Is Extremely Slow on Complex Windows

**What goes wrong:**
The natural approach to finding status text in Teams' AX tree is to dump `entire contents` of the window and search for a status string. This is catastrophically slow. `entire contents` traverses the ENTIRE UI element hierarchy, which for a complex Electron/web-based app like Teams can be thousands of elements. This takes 5-30 seconds, during which the event loop is blocked (see Pitfall 1).

**Why it happens:**
Developers use `entire contents` as a convenient debugging tool in Script Editor to explore the UI hierarchy. It works (slowly) for exploration, but is unsuitable for repeated automated queries. The performance problem is architectural: each UI element access requires a round-trip Apple Event between the scripting host and the target application, and `entire contents` generates hundreds or thousands of these round-trips.

**Prevention:**
- NEVER use `entire contents` in the production AppleScript. Use a targeted path: `tell application "System Events" to tell process "Microsoft Teams" to get value of static text 1 of group 1 of ...` (path discovered via Accessibility Inspector).
- Use Accessibility Inspector (in Xcode developer tools) to find the exact element path to the status text. Document this path and the Teams version it was discovered on.
- If the exact path is fragile (breaks on Teams update), use a shallow search: iterate children of a specific known container, not the entire window.
- The narrower the element targeting, the faster the call: targeting a specific group at a known depth is 10-100x faster than `entire contents`.
- Profile the AppleScript: run it in Script Editor and check the time. If it takes >200ms, the path is too broad.

**Detection:**
- osascript calls consistently take >1 second (should be <200ms for a targeted query)
- Event loop processing latency spikes when AX signal is used

**Phase to address:** Research phase (before implementation) -- use Accessibility Inspector to discover the element path BEFORE writing the AppleScript. The script design depends on the discovered path.

---

### Pitfall 10: Gating Logic Drops Notifications That Should Be Forwarded

**What goes wrong:**
The core v1.1 behavior is: forward on Away/Busy, drop on Available/Offline/OOO. Edge cases:
1. **Status is UNKNOWN** (all signals failed): Drop or forward? Dropping means silent data loss. Forwarding means noise when the user is Available.
2. **Status is "Do not disturb"**: Is DND treated as Busy (forward) or as "user explicitly doesn't want interruptions" (drop)?
3. **Status just changed**: User went Away 5 seconds ago, but the notification was generated 10 seconds ago (before the transition). The notification was for an Available user but is being processed against an Away status.
4. **Teams is not running**: Should this be treated as "user is away from Teams" (forward) or "user is not using Teams" (drop)?
5. **Multiple status values from different signals disagree** (covered in Pitfall 6, but the gating decision amplifies the impact).

**Why it happens:**
Developers implement the happy path (Available = drop, Away = forward) and don't think through the edge cases. Each edge case is a policy decision, not a technical one, and different users want different behavior.

**Prevention:**
- Define explicit policy for EVERY status value, including UNKNOWN:
  ```
  Available    -> DROP
  Away         -> FORWARD
  Busy         -> FORWARD
  DND          -> FORWARD (user may still want mobile alert)
  BRB          -> FORWARD
  Offline      -> DROP (Teams not connected)
  OOO          -> DROP
  UNKNOWN      -> FORWARD (fail-open: better to get noise than miss messages)
  ```
- Make the gating policy configurable: `"forward_statuses": ["Away", "Busy", "DND", "BRB", "Unknown"]` in config.json. This lets users tune without code changes.
- Log every gating decision: `"Notification from Alice | status=Available (source=ax) | DROPPED"`. This is essential for debugging and user confidence.
- Default to fail-open (forward on UNKNOWN). The v1.0 behavior was "forward everything." The v1.1 addition should only SUBTRACT (drop when clearly Available), never accidentally drop when uncertain.

**Detection:**
- Users report missing notifications (dropped due to wrong status)
- Logs show UNKNOWN status for extended periods with no explanation
- DND notifications are dropped when user expected them forwarded

**Phase to address:** First implementation plan -- the gating policy must be defined and documented before the code is written. This is a requirements decision, not a coding decision.

---

## Minor Pitfalls

Mistakes that cause minor issues, confusing logs, or operational annoyance.

### Pitfall 11: Status Check Spawns Too Many Processes

**What goes wrong:**
Without caching, every notification triggers up to 3 subprocess calls (osascript, ioreg, pgrep). During a burst of 20 notifications, that is 60 process spawns in seconds. Each `subprocess.run()` call forks the Python process, exec's the command, waits for completion, and cleans up. This is expensive: each fork copies the process's memory page table, and on macOS, process creation is slower than on Linux.

**Prevention:**
- Cache status with a 30-60 second TTL. Status doesn't change faster than this.
- Check status once per poll cycle, not per notification. The entire batch shares one status.
- The fallback chain should short-circuit: if AX returns a result, don't also run ioreg and pgrep.

---

### Pitfall 12: ioreg Output Changes Between macOS Versions

**What goes wrong:**
The ioreg output format (key names, nesting, quoting) has been stable for years, but Apple does not guarantee it. Future macOS versions could rename `HIDIdleTime`, change the IOHIDSystem hierarchy, or alter the output formatting. The regex-based parser would silently fail.

**Prevention:**
- If the regex returns None, log a WARNING with the raw ioreg output (first 500 chars) so you can diagnose the format change.
- Include the macOS version in startup logs so format failures can be correlated with OS updates.
- Consider `ioreg -a` (plist output format) which produces machine-parseable XML plist. This is more robust than parsing the human-readable text format: `ioreg -c IOHIDSystem -a` then parse with `plistlib.loads()`.

---

### Pitfall 13: Status Metadata Bloats Webhook Payload When Status Is Unavailable

**What goes wrong:**
The webhook payload adds `detected_status`, `status_source`, and `status_confidence` fields. When all signals fail, these fields are `"Unknown"`, `"none"`, `"zero"` respectively -- present but not useful. Downstream consumers that parse these fields may misinterpret "Unknown" as a meaningful status rather than "we couldn't determine status."

**Prevention:**
- Use `null` (JSON null) for `detected_status` when status cannot be determined, not a string "Unknown". This lets downstream code distinguish "checked and found Unknown" from "could not check."
- Or: include a `status_available: true/false` boolean alongside the status fields.
- Document the webhook payload schema including all possible values for the status fields.

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| AppleScript AX tree walk | osascript hangs permanently (Pitfall 2) | Always use `timeout=5` on subprocess.run() |
| AppleScript AX tree walk | `entire contents` takes 5-30 seconds (Pitfall 9) | Use targeted element path, not tree dump |
| AppleScript AX tree walk | Permission granted to wrong app (Pitfall 3) | Probe at startup, print instructions with detected terminal name |
| AppleScript AX tree walk | New Teams has broken AX tree (Pitfall 4) | Validate result against known status strings, design for AX being unavailable |
| ioreg HIDIdleTime | Nanosecond/second confusion (Pitfall 5) | Always divide by 1_000_000_000, comment the conversion |
| ioreg HIDIdleTime | Output format changes (Pitfall 12) | Use robust regex, handle None, consider plist output mode |
| pgrep process check | Matches wrong process (Pitfall 7) | Use `-x` for exact match, make process name configurable |
| Subprocess from event loop | Blocks kqueue loop (Pitfall 1) | Cache status with TTL, check once per cycle not per notification |
| Fallback chain | Signals disagree during transitions (Pitfall 6) | Use one signal chain (not hybrid), add hysteresis |
| Gating logic | Drops notifications on UNKNOWN status (Pitfall 10) | Fail-open policy, configurable forward_statuses |
| Sleep/wake | Stale cache after wake (Pitfall 8) | Use monotonic time for cache, invalidate on long gaps |
| Status caching | Too many subprocesses without cache (Pitfall 11) | 30-60s TTL, check once per cycle |

## Subprocess Integration Gotchas (macOS-specific)

Common mistakes when calling external commands from a Python daemon on macOS.

| Gotcha | What Happens | Correct Approach |
|--------|-------------|------------------|
| No `timeout=` on subprocess.run() | Process hangs forever if child doesn't exit | Always specify `timeout=5` (or appropriate value) |
| Using `shell=True` with osascript | Shell injection risk if any input is interpolated; also slower (extra shell process) | Use `shell=False` with list args: `["osascript", "-e", script]` |
| Not capturing stderr | Accessibility errors appear on stderr; returncode 1 alone doesn't explain why | Use `capture_output=True` and check `result.stderr` for "not allowed assistive access" |
| Ignoring returncode | osascript returns 1 on error but stdout may still contain partial output | Check `result.returncode == 0` before using stdout |
| Not handling TimeoutExpired | The exception propagates up and crashes the event loop | Catch `subprocess.TimeoutExpired` and return None / fallback |
| Calling subprocess in signal handler | Signal handlers in Python are restricted; subprocess calls may deadlock | Never call subprocess from a signal handler. The daemon already uses the flag-setting pattern (correct). |
| Not stripping subprocess stdout | Output includes trailing newline; comparison with "Available" fails because it is "Available\n" | Always `.strip()` the stdout before comparing |
| ioreg output parsed with wrong regex | Multiple HIDIdleTime entries; regex grabs wrong one | Use `re.search()` (first match) or filter with `-d` depth flag |

## AX Permission Handling Patterns

The correct pattern for detecting and communicating Accessibility permission status.

```
Startup:
  1. Run minimal AX probe: osascript -e 'tell application "System Events" to get name of first process'
  2. If returncode == 0: AX is available. Log "AX status signal: AVAILABLE"
  3. If returncode == 1 AND stderr contains "assistive access":
     a. Detect terminal: os.environ.get("TERM_PROGRAM")
     b. Log WARNING with instructions: "Accessibility permission required for status detection.
        Grant access to [terminal name] in System Settings > Privacy & Security > Accessibility."
     c. Log "AX status signal: UNAVAILABLE (missing permission)"
     d. Continue running with degraded status (idle+process only)
  4. If returncode == 1 AND stderr contains other error:
     a. Log WARNING: "AX probe failed: [stderr content]"
     b. Log "AX status signal: UNAVAILABLE (probe error)"
     c. Continue running with degraded status

Runtime:
  5. AX calls that fail should NOT retry immediately -- use a backoff
  6. After N consecutive AX failures, disable AX signal for the session and log WARNING
  7. Periodically re-enable AX probe (every 5 minutes) in case permission was granted while running
```

## "Looks Done But Isn't" Checklist (v1.1)

Things that appear complete but are missing critical pieces for the status detection feature.

- [ ] **osascript call works:** But no `timeout=` parameter -- will hang when Teams is unresponsive. Verify with `kill -STOP` on Teams process.
- [ ] **AX returns status text:** But doesn't validate against known status set -- garbage text from broken AX tree treated as valid status.
- [ ] **ioreg parsing works:** But uses nanoseconds directly without dividing by 10^9 -- idle time appears as billions of seconds.
- [ ] **Fallback chain works:** But checks all three signals every time -- no caching, no short-circuit. 3 subprocess calls per notification during bursts.
- [ ] **Gating drops Available notifications:** But doesn't handle UNKNOWN status -- all notifications dropped when AX times out and ioreg fails.
- [ ] **Status in webhook payload:** But uses string "Unknown" instead of null -- downstream can't distinguish "checked, unknown" from "couldn't check."
- [ ] **pgrep checks for Teams:** But uses substring match -- also matches "Microsoft Teams Helper" and "Microsoft Teams (work preview)."
- [ ] **Cache implemented:** But uses `time.time()` instead of `time.monotonic()` -- cache validity affected by clock adjustments and sleep/wake.
- [ ] **Accessibility probe at startup:** But only checks for System Events access, not specifically for Teams window access -- probe passes but Teams-specific AX fails.
- [ ] **Status detection integrated:** But status is checked AFTER notification processing begins -- if status is Available, work was done to parse/filter the notification before it gets dropped. Check status FIRST, skip the batch if dropping.

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| osascript hangs (no timeout) | LOW | Kill daemon, add `timeout=5` to subprocess.run(), restart |
| Accessibility permission missing | LOW | Grant permission in System Settings, restart daemon (or wait for periodic re-probe) |
| AX tree broken after Teams update | MEDIUM | Re-inspect tree with Accessibility Inspector, update AppleScript path, redeploy |
| ioreg format changed after macOS update | LOW | Capture new ioreg output, update regex, redeploy |
| Status cache stale after sleep | LOW | Status auto-refreshes on next cache miss; may incorrectly gate 1-2 notifications after wake |
| pgrep matches wrong process | LOW | Update process name in config.json, restart daemon |
| Gating drops notifications incorrectly | LOW-MEDIUM | Change `forward_statuses` in config.json; lost notifications cannot be replayed |

## Sources

- [Apple Developer Forums: HIDIdleTime not being reset under Screen Sharing](https://developer.apple.com/forums/thread/721530) - HIDIdleTime behavior on headless Macs, nanosecond format. **MEDIUM confidence** (specific to Catalina; Sequoia may differ).
- [Microsoft Community Hub: Enable Accessibility Tree on macOS in new Teams](https://techcommunity.microsoft.com/discussions/teamsdeveloper/enable-accessibility-tree-on-macos-in-the-new-teams-work-or-school/4033014) - New Teams AX tree is broken, parent-child relationships inconsistent. **MEDIUM confidence** (community report, may be fixed in later updates).
- [DSSW: Inactivity and Idle Time on OS X](https://www.dssw.co.uk/blog/2015-01-21-inactivity-and-idle-time/) - HIDIdleTime overview, nanosecond conversion, IOKit architecture. **HIGH confidence** (well-documented, stable API).
- [MacScripter: entire contents is slow](https://www.macscripter.net/t/entire-contents-is-slow-make-it-faster/54743) - Performance issues with `entire contents`, alternatives for targeted element access. **HIGH confidence** (well-known AppleScript limitation).
- [Apple Developer Docs: Automating the User Interface](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AutomatetheUserInterface.html) - UI scripting requires Accessibility permission on the calling app. **HIGH confidence** (official Apple documentation).
- [Scripting OS X: Avoiding AppleScript Security and Privacy Requests](https://scriptingosx.com/2020/09/avoiding-applescript-security-and-privacy-requests/) - Permission model for osascript, which app receives the grant. **HIGH confidence** (well-researched blog by macOS admin expert).
- [Python docs: subprocess management](https://docs.python.org/3/library/subprocess.html) - subprocess.run() timeout behavior, TimeoutExpired exception, POSIX child process cleanup. **HIGH confidence** (official Python documentation).
- [Python discuss: Sporadic hang in subprocess.run](https://discuss.python.org/t/sporadic-hang-in-subprocess-run/26213) - Known issue with subprocess timeout on POSIX (busy loop implementation). **MEDIUM confidence** (CPython-specific behavior).
- [GitHub: alacritty/alacritty#7334](https://github.com/alacritty/alacritty/issues/7334) - Assistive access broken from non-standard terminals; Accessibility permission is per-parent-app. **HIGH confidence** (reproduced by multiple users).
- [XS-Labs: Detecting idle time with I/O Kit](https://xs-labs.com/en/archives/articles/iokit-idle-time/) - IOKit IOHIDSystem architecture, HIDIdleTime internals. **MEDIUM confidence** (older article, but IOKit API is stable).

**Confidence note:** The highest uncertainty is around the new Teams (com.microsoft.teams2) AX tree structure. Community reports suggest it is broken, but Microsoft may have fixed it in recent updates. This MUST be verified with Accessibility Inspector on the actual installed Teams version before writing the AppleScript. If the AX tree is non-functional, the entire AX signal should be deprioritized and the system designed primarily around idle+process detection.

---
*Pitfalls research for: Status detection integration (v1.1 milestone)*
*Researched: 2026-02-11*
*Previous version: v1.0 pitfalls (shipped, addressed in Phases 1-3)*
