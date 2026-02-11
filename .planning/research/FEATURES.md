# Feature Landscape: Status-Aware Notification Gating

**Domain:** macOS status detection and context-aware notification filtering for Teams daemon
**Researched:** 2026-02-11
**Confidence:** MEDIUM (verified ecosystem patterns; AX approach for new Teams requires runtime validation)

## Table Stakes

Features users expect from a daemon that gates notifications based on user presence. Missing any of these = the status gating feels broken or untrustworthy.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **System idle time detection** | The most basic presence signal. If the user has not touched keyboard/mouse in N minutes, they are "away." Every presence-aware tool (screensavers, Slack, Teams itself) uses HIDIdleTime as the ground truth. Without this, the daemon cannot determine basic presence. | Low | `ioreg -c IOHIDSystem` returns HIDIdleTime in nanoseconds. Divide by 1e9 for seconds. Single subprocess call, no dependencies. Well-documented, works on Sequoia. |
| **Configurable idle threshold** | Different users have different idle definitions. A developer debugging might not touch the keyboard for 10 minutes but is still "present." A 300s (5 min) default is reasonable (matches Teams' own idle threshold) but MUST be configurable. | Low | Config field `idle_threshold_seconds` with default 300. Trivial to implement. |
| **Process-level Teams detection** | The daemon must know if Teams is running at all. If Teams is not running, the user cannot see notifications in Teams, so ALL notifications should be forwarded regardless of idle state. This is the simplest, most reliable signal. | Low | `pgrep -x "Microsoft Teams"` for new Teams. Must also check for `com.microsoft.teams2` bundle. New Teams (2024+) changed process naming; needs runtime verification on target machine. |
| **Status-aware gating logic** | The core value proposition: forward notifications only when the user is Away or Busy (cannot see them in Teams), suppress when Available (would see them directly). Without this logic, the daemon is just a webhook forwarder with no intelligence. | Med | Requires combining status signals into a decision. Map detected status to forward/suppress action. Must handle ambiguous states gracefully (default to forward -- better to get a duplicate than miss a message). |
| **Fallback chain for status detection** | No single status detection method is 100% reliable on macOS. AX access may be denied, HIDIdleTime has known edge cases (Karabiner-Elements causes keyboard-only idle to not reset), process checks tell you nothing about actual status. A fallback chain (AX text -> idle time -> process check) provides resilience. | Med | Three detection methods tried in priority order. If higher-priority method fails or is unavailable, fall through to next. Each method returns a status + confidence level. |
| **Status metadata in webhook payload** | Downstream consumers need to know WHY a notification was forwarded. Was the user detected as Away via AX reading, or was it inferred from idle time? This transparency lets downstream systems apply their own logic (e.g., treat AX-detected Away differently than idle-inferred Away). | Low | Add `detected_status`, `status_source`, and `status_confidence` fields to existing webhook JSON payload. Minimal code change -- extend `build_webhook_payload()`. |
| **Graceful degradation without Accessibility permission** | Accessibility access requires explicit user grant in System Settings. Many users will not grant it, or corporate MDM may block it. The daemon MUST work without AX access, falling back to idle + process detection. Crashing or refusing to start without AX access is a dealbreaker. | Med | On AX failure, log a warning with instructions (not an error), set AX as unavailable, continue with remaining signals. Re-check AX availability periodically (user might grant it later). |
| **"Always forward" escape hatch** | Users must be able to disable status gating entirely and revert to v1.0 behavior (forward everything). This is critical for debugging, for users who find the gating too aggressive, and as a safety net if status detection breaks. | Low | Config field `status_gating_enabled` (default: true). When false, skip all status checks and forward everything. Existing filter pipeline remains unchanged. |

## Differentiators

Features that go beyond minimum expectations. Not required for the gating to "work" but significantly improve trust and usability.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **Teams AX status text reading** | The only way to get the ACTUAL Teams status (Available, Away, Busy, DND, Be Right Back, Offline) without Microsoft Graph API. Reading the status text from Teams' UI via Accessibility API gives ground truth rather than inference. This is the primary differentiator over simple idle detection. | High | New Teams (2024+) moved from Electron to WebView2/React. The old AX tree exposure method no longer works. The new Teams menu bar extension (rolled out Oct-Nov 2024) shows presence status as an icon -- this is a potential AX target. Must use Accessibility Inspector to map the actual AX element hierarchy on the target machine. Requires `osascript` calling AppleScript with System Events. MEDIUM confidence this works -- needs runtime validation. |
| **Teams menu bar icon status reading** | The Teams menu bar extension (GA since Nov 2024) displays a persistent presence indicator. If this menu bar extra exposes an AX `description` attribute containing status text, it provides a lightweight, always-available status reading without needing to inspect the main Teams window. | Med-High | Menu bar extras are accessible via `tell application "System Events" to get description of menu bar items of menu bar 1 of process "Microsoft Teams"`. Whether the Teams menu bar extra actually exposes useful description text (vs. generic "menu extra") is UNVERIFIED and needs runtime testing. LOW confidence until validated. |
| **Screen lock / session state detection** | If the screen is locked, the user is definitively away. This is a stronger signal than idle time (user might be idle but looking at the screen). macOS provides `CGSessionCopyCurrentDictionary` which reports `CGSSessionScreenIsLocked`. | Med | Requires calling into Core Graphics framework. Python stdlib cannot do this natively. Options: (a) `osascript` to check if screen saver is running, (b) parse output of `ioreg` for screen state, (c) ctypes/objc bridge to call CG function. Option (a) is simplest but not perfectly reliable. |
| **macOS Focus/DND mode detection** | If the user has Focus mode (Do Not Disturb) enabled, they have explicitly chosen to suppress notifications. This is a strong signal that forwarding is appropriate (notifications are being silenced, so the webhook ensures they still arrive somewhere). | Med | Can read from `~/Library/DoNotDisturb/DB/` using JXA/osascript or check `com.apple.controlcenter.plist` with `plutil`. The plist path has changed across macOS versions. Needs Sequoia-specific verification. |
| **Status change event logging** | Log every status transition (Available -> Away, Away -> Available) with timestamp. Useful for debugging, for understanding the daemon's decision-making, and for downstream consumers who want a status history. | Low | Log at INFO level on each status change. Include old status, new status, detection source, and timestamp. Trivial to implement. |
| **Status check caching / rate limiting** | AX queries and subprocess calls (ioreg, pgrep) have non-trivial overhead if run on every poll cycle (every 5 seconds). Caching the last status for a configurable TTL (e.g., 30 seconds) reduces overhead while maintaining responsiveness. | Low | Cache last status result with timestamp. If cache age < TTL, return cached value. Simple dict/timestamp check. |
| **Hysteresis / debounce for status transitions** | Avoid rapid forward/suppress flipping when status oscillates (e.g., user briefly touches mouse during an "Away" period). Require a status to be stable for N seconds before acting on it. Prevents notification storm on brief activity. | Med | Track status stability duration. Only transition gating state after threshold (e.g., 30s stable). Adds state complexity but prevents a common annoyance. |
| **Per-status gating rules** | Instead of binary "forward when Away/Busy, suppress otherwise," allow per-status configuration: `{"Available": "suppress", "Away": "forward", "Busy": "forward", "DND": "forward", "BeRightBack": "suppress", "Offline": "suppress"}`. Different users want different behavior. | Low | Config dict mapping status strings to actions. Straightforward lookup. Default rules cover the common case. |

## Anti-Features

Features to deliberately NOT build for status detection. Each adds complexity that does not justify the value for this daemon.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| **Microsoft Graph API for presence** | Graph API requires Azure AD app registration, OAuth2 token flow, tenant admin consent, token refresh logic, and network connectivity. This is the exact complexity the project exists to avoid. It also means the daemon needs outbound internet access to Microsoft's APIs, adding a failure mode. | Stick with local-only detection (AX, idle, process). Accept that local detection is inferential rather than authoritative. The webhook payload's `status_confidence` field lets downstream consumers account for uncertainty. |
| **Teams log file parsing for status** | The new Teams client (2024+) no longer writes presence status to local log files. The old `~/Library/Application Support/Microsoft/Teams/logs.txt` approach is dead. Multiple community projects (TeamsStatusMacOS, ms-teams-status-log) have been archived or pivoted for this reason. | Do not invest time in log parsing. The log file approach is a dead end for new Teams. |
| **powerd log parsing for call detection** | TeamsStatusMacOS uses `powerd` process logs to detect meeting/call status as a workaround for the log file loss. This is clever but fragile -- it only detects "in a call" vs "not in a call," not full presence status. It also depends on undocumented system log behavior that may change across macOS versions. | If call detection becomes important, consider it as a supplementary signal in the fallback chain. Do not build the entire status detection around it. |
| **Screen recording / pixel scraping** | Reading the Teams status dot color by taking screenshots and analyzing pixels. Requires Screen Recording permission (even more intrusive than Accessibility), is fragile to UI changes, and is computationally expensive for a polling daemon. | Use AX text reading which is the intended programmatic access pattern. Fall back to behavioral inference (idle time) when AX is unavailable. |
| **Continuous AX monitoring (observer pattern)** | Setting up an AX observer to watch for status changes in real-time (push model) instead of polling (pull model). Adds significant complexity (CFRunLoop integration, observer lifecycle management) for marginal latency improvement. | Poll-based status checking at the existing poll interval (5s) or a separate status check interval (30s) is adequate. The notification latency already includes the 5s poll cycle; adding another 30s for status is acceptable. |
| **Network-based idle detection** | Monitoring network traffic to/from Teams to infer activity. Requires elevated permissions (packet capture or network extension), adds privacy concerns, and is unreliable (Teams sends background traffic even when idle). | Stick with HIDIdleTime which directly measures what matters: human input device activity. |
| **Calendar integration for auto-status** | Reading the user's calendar to predict when they are in meetings and adjusting gating accordingly. Adds OAuth complexity (for Exchange/Google), requires new permissions, and duplicates functionality Teams already has (calendar-based status). | If the user's Teams status is "In a meeting" (set by Teams from their calendar), the AX reader will pick that up. No need to duplicate the calendar -> status logic. |

## Feature Dependencies

```
[Existing] Notification filter pipeline --> Status-aware gating (gating wraps existing filters)
[Existing] Webhook payload builder --> Status metadata fields (extends existing payload)
[Existing] Config loader --> Status gating config (new fields in existing config.json)
[Existing] Event loop --> Status check integration (check status before/during notification processing)

System idle detection --> Fallback chain (idle is the middle-priority signal)
Process-level Teams detection --> Fallback chain (process check is the lowest-priority signal)
Teams AX status text reading --> Fallback chain (AX is the highest-priority signal)
Fallback chain --> Status-aware gating logic (chain produces the status; gating acts on it)
Status-aware gating logic --> Status metadata in webhook (gating decision feeds payload)
Configurable idle threshold --> System idle detection (threshold parameterizes idle check)
"Always forward" escape hatch --> Status-aware gating logic (escape hatch bypasses gating)
Graceful degradation without AX --> Fallback chain (drives fall-through behavior)
```

Dependency ordering (build in this order):

```
1. System idle detection (ioreg HIDIdleTime) -- simplest, no permissions needed
2. Process-level Teams detection (pgrep) -- simple, no permissions needed
3. Configurable idle threshold + "always forward" config -- wire config before logic
4. Fallback chain assembly (idle + process for now; AX slot prepared but empty)
5. Status-aware gating logic (forward/suppress decision)
6. Status metadata in webhook payload (extend existing builder)
7. Teams AX status text reading (most complex, requires permission, may fail)
8. Graceful AX degradation (handle permission denial, fall through)
9. [Differentiators] Status caching, hysteresis, per-status rules, screen lock detection
```

## MVP Recommendation

**Prioritize (in dependency order):**

1. **System idle detection via HIDIdleTime** -- most reliable signal, zero permissions, zero dependencies, works immediately. This alone delivers 80% of the value: if the user is idle for 5+ minutes, forward notifications.
2. **Process-level Teams detection** -- complements idle detection. If Teams is not running, always forward. Simple subprocess call.
3. **Config additions** (idle threshold, always-forward escape hatch, per-status rules) -- wire configuration before building the logic that consumes it.
4. **Fallback chain with status-aware gating** -- the core decision engine. Combine signals, produce a status, gate notifications based on it.
5. **Status metadata in webhook payload** -- low-cost, high-value transparency for downstream consumers.
6. **Teams AX status text reading** -- attempt to read actual Teams status. This is the highest-value signal but also the highest-risk. The new Teams client's AX tree exposure is uncertain. The menu bar extension is a potential target but unverified.
7. **Graceful AX degradation** -- handle the case where AX reading fails or is not permitted.

**Defer to post-MVP:**

- **Screen lock detection**: Useful but requires calling into Core Graphics (not stdlib). Add after core gating works.
- **macOS Focus/DND mode detection**: Supplementary signal. The idle time check already covers most "DND because away" scenarios. Add later for users who actively use Focus mode while present.
- **Hysteresis / debounce**: Only build if users report flapping behavior in practice. The 5s poll interval already provides natural damping.
- **Status check caching**: Only needed if status checks cause measurable performance impact. Profile first, optimize later.

**Build immediately as polish:**

- **Status change event logging**: Trivial to implement (one `logging.info()` call on status change). Essential for debugging status detection. Include from day one.

## User Expectations for Status-Based Gating

These are behaviors users expect from a presence-aware notification forwarder. Violating any of these will erode trust in the daemon.

### Must-have behaviors

| Expectation | Why | Implementation |
|-------------|-----|----------------|
| **Never miss a message when truly away** | This is the core promise. If the user is away from their Mac and a Teams message arrives, the webhook MUST fire. Missing a message is worse than sending a duplicate. | Default to "forward" on any ambiguous or error state. If status detection fails entirely, forward everything (fail-open). |
| **Never send duplicates when at the computer** | If the user is actively using Teams, they see the notification in Teams AND receive a webhook. This creates annoying duplicates for downstream consumers (e.g., a phone notification they don't need). | Suppress webhooks when status is Available or when idle time is below threshold. But err toward forwarding if uncertain -- duplicates are annoying but not harmful. |
| **Transparent about its decisions** | Users need to understand why a notification was or was not forwarded. "Magic" gating that silently drops messages is anxiety-inducing. | Status metadata in webhook payload. INFO-level logging of status transitions and gating decisions. --dry-run mode shows what would happen. |
| **Quick to recognize "away"** | If the user walks away, the daemon should start forwarding within a reasonable time (5-10 minutes, not 30+). | 300s (5 min) idle threshold default matches Teams' own behavior. Configurable for users who want faster detection. |
| **Quick to recognize "back"** | When the user returns and moves the mouse, stop forwarding immediately. Don't keep sending webhooks for 5 minutes after the user is back. | HIDIdleTime resets to 0 on any input. Next poll cycle (5s) detects the change. No delay beyond the poll interval. |
| **Startup behavior: forward by default** | On daemon startup, before the first status check completes, the daemon should forward notifications (not suppress). This avoids a window where messages are silently dropped. | Initialize status as "unknown" which maps to "forward." First status check happens within one poll cycle. |

### Nice-to-have behaviors

| Expectation | Why | Implementation |
|-------------|-----|----------------|
| **Respect Teams "Do Not Disturb" status** | If the user set DND in Teams, they probably don't want ANY notifications forwarded. Some users will disagree (they want webhooks even in DND). Make it configurable. | DND maps to "suppress" by default in per-status rules. Config override available. |
| **Handle sleep/wake correctly** | When the Mac sleeps, HIDIdleTime keeps incrementing. On wake, the user is "back" but idle time is very high. Must reset state on wake, not assume "very idle = very away." | HIDIdleTime resets on wake (mouse/keyboard required to unlock). Natural behavior handles this correctly. |
| **Handle Teams quit/restart** | If Teams quits (update, crash) and restarts, the daemon should detect this and not assume "user is offline" during the restart window. | Brief absence of Teams process (< 60s) should not trigger status change. Debounce process detection. |

## Confidence Notes

| Feature Category | Confidence | Rationale |
|-----------------|------------|-----------|
| System idle detection (HIDIdleTime) | HIGH | Well-documented macOS API, confirmed working via ioreg on modern macOS. Known Karabiner-Elements edge case documented. |
| Process-level Teams detection | HIGH | pgrep/subprocess is straightforward. Process naming for new Teams needs one-time runtime verification. |
| Status-aware gating logic | HIGH | Straightforward decision logic. No external dependencies. Pattern is well-established in tools like Muzzle, DND automation. |
| Teams AX status text (main window) | LOW | New Teams (WebView2/React) no longer exposes AX tree the same way as old Electron Teams. Multiple sources confirm the old method is broken. Must discover new element hierarchy. |
| Teams menu bar extension AX reading | LOW | Menu bar extension is new (Nov 2024). Whether its AX description attribute contains useful status text is completely unverified. Requires runtime testing with Accessibility Inspector. |
| Screen lock / DND detection | MEDIUM | Multiple documented approaches exist but macOS version-specific behavior (Sequoia) needs verification. |
| Fallback chain pattern | HIGH | Established pattern in presence detection tools. Multiple successful implementations (macos-notification-state, TeamsStatusMacOS). |
| User expectations | HIGH | Derived from common patterns in notification forwarding tools (Pushover quiet hours, Slack presence, Teams own behavior). Well-understood domain. |

## Sources

- [Apple: Mac Automation Scripting Guide - UI Automation](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AutomatetheUserInterface.html)
- [Microsoft Community: New Teams AX tree issue](https://techcommunity.microsoft.com/discussions/teamsdeveloper/enable-accessibility-tree-on-macos-in-the-new-teams-work-or-school/4033014)
- [GitHub: TeamsStatusMacOS - powerd-based status detection](https://github.com/RobertD502/TeamsStatusMacOS)
- [GitHub: teams-call - Shell script for Teams call detection](https://github.com/mre/teams-call)
- [GitHub: macos-notification-state - Native notification state detection](https://github.com/felixrieseberg/macos-notification-state)
- [GitHub: Karabiner-Elements HIDIdleTime bug](https://github.com/pqrs-org/Karabiner-Elements/issues/385)
- [Apple Developer Forums: HIDIdleTime not resetting](https://developer.apple.com/forums/thread/721530)
- [DSSW: Inactivity and Idle Time on OS X](https://www.dssw.co.uk/blog/2015-01-21-inactivity-and-idle-time/)
- [Microsoft: Teams Menu Bar Icon for Mac](https://websites.uta.edu/oit/2024/10/16/microsoft-teams-menu-bar-icon-for-mac-devices/)
- [GitHub: Microsoft Teams AppleScripts](https://github.com/kpshek/microsoft-teams-applescripts)
- [Microsoft: Stale presence status in Teams for Mac](https://learn.microsoft.com/en-us/troubleshoot/microsoftteams/teams-on-mac/incorrect-presence-status-teams-for-mac)
- [GitHub: Microsoft Teams Presence Detector (archived)](https://github.com/EthyMoney/Microsoft-Teams-Presence-Detector)
- [Automators Talk: Get current focus mode via script](https://talk.automators.fm/t/get-current-focus-mode-via-script/12423)
- [XS-Labs: Detecting idle time with I/O Kit](https://xs-labs.com/en/archives/articles/iokit-idle-time/)
- [GitHub: Idle time detection Python gist](https://gist.github.com/KingYes/da8b0f1b9f290d7378f4)
- Project context: `~/Projects/macos-notification-intercept/.planning/PROJECT.md`
