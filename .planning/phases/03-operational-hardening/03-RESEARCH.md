# Phase 3: Operational Hardening - Research

**Researched:** 2026-02-11
**Domain:** Python signal handling, CLI argument parsing, daemon lifecycle management
**Confidence:** HIGH

## Summary

Phase 3 adds two features to the existing single-file Python daemon (`nchook.py`): graceful shutdown on SIGINT/SIGTERM (OPER-03) and a `--dry-run` CLI flag (OPER-04). Both features are straightforward additions to the existing codebase because Phase 1 and Phase 2 deliberately prepared for them:

- A module-level `running = True` flag already controls the `while running:` event loop (placed in Phase 1 explicitly for Phase 3 signal handlers)
- The `signal` and `argparse` modules are already imported
- The event loop has a `finally` block that cleans up kqueue FDs and the DB connection
- The `save_state()` function already implements atomic writes (tempfile + fsync + os.replace)
- Webhook delivery is already gated on `config is not None and config.get("webhook_url")`, making dry-run a simple config-level toggle

The technical challenge is minor: understanding the interaction between Python signal handlers and PEP 475 auto-retry of `kq.control()`. The conclusion is that a flag-based handler (no exception raised) works correctly -- the blocking kqueue call auto-retries with a recomputed timeout, and the loop exits naturally within at most one poll interval (5 seconds). This is acceptable for graceful shutdown.

**Primary recommendation:** Use the flag-based signal handler pattern (set `running = False` in handler, let event loop exit naturally within one poll interval) combined with a final `save_state()` call before exit. For `--dry-run`, add an `argparse.ArgumentParser` and thread the flag through to skip `post_webhook()` while still logging the payload.

## Standard Stack

### Core

This phase uses ONLY Python standard library modules. No new dependencies.

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| signal | stdlib | Register SIGINT/SIGTERM handlers | Only way to handle Unix signals in Python |
| argparse | stdlib | Parse --dry-run flag | Standard CLI parsing, already imported |
| logging | stdlib | Log shutdown events and dry-run payloads | Already used throughout codebase |
| json | stdlib | Format payload for dry-run logging | Already used for state/config |

### Supporting

No additional libraries needed. Everything required is already imported in `nchook.py`.

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| signal.signal() flag-based | signal.set_wakeup_fd() pipe-based | Pipe-based wakes kqueue instantly but adds complexity for zero benefit (5s max latency is fine) |
| argparse | sys.argv manual parsing | argparse gives --help for free and is already imported |
| Module-level `running` flag | threading.Event | Event adds unnecessary complexity for single-threaded daemon |

**Installation:**
```bash
# No installation needed -- all stdlib
```

## Architecture Patterns

### Current Code Structure (single-file daemon)
```
nchook.py          # Everything in one file (~815 lines after Phase 2)
config.json        # Runtime configuration
state.json         # Persisted last_rec_id
```

Phase 3 does NOT change the file structure. All changes are within `nchook.py`.

### Pattern 1: Flag-Based Signal Handler for Graceful Shutdown

**What:** Signal handler sets a module-level boolean flag; the main loop checks the flag and exits naturally.
**When to use:** Single-threaded daemon with a polling loop that has a bounded timeout.
**Why it works here:** `kq.control([kev], 1, poll_interval)` has a 5-second timeout. After the signal handler sets `running = False`, PEP 475 auto-retries the kqueue call with recomputed timeout. The loop exits within at most one poll interval when `while running:` re-evaluates.

**Example:**
```python
# Source: Python signal docs + PEP 475
import signal

running = True

def _shutdown_handler(signum, frame):
    """Signal handler: set running flag to False for graceful shutdown."""
    global running
    signame = signal.Signals(signum).name
    logging.info("Received %s, shutting down...", signame)
    running = False

# Register in main() BEFORE entering event loop
signal.signal(signal.SIGINT, _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)
```

**Critical detail:** The handler must NOT raise an exception. If it did, the exception would propagate from inside `kq.control()`, potentially bypassing the state-save logic. The flag-based approach lets the loop exit through normal control flow, hitting the `finally` block and any post-loop cleanup.

### Pattern 2: Dry-Run as Config Flag Threading

**What:** Parse `--dry-run` from CLI, pass it through to the event loop, skip HTTP POST but still log the payload.
**When to use:** When you want to test the full pipeline without side effects.

**Example:**
```python
# Source: Python argparse docs
parser = argparse.ArgumentParser(description="Teams Notification Interceptor")
parser.add_argument("--dry-run", action="store_true",
                    help="Log webhook payloads without sending HTTP requests")
args = parser.parse_args()

# Thread through to run_watcher
run_watcher(db_path, wal_path, STATE_FILE, config, dry_run=args.dry_run)

# Inside run_watcher, in the webhook delivery section:
if dry_run:
    logging.info("DRY-RUN | Would POST: %s", json.dumps(payload, indent=2))
else:
    post_webhook(payload, config["webhook_url"], ...)
```

**Key:** `--dry-run` becomes `args.dry_run` (hyphen to underscore). Use `action="store_true"` so the flag defaults to `False`.

### Pattern 3: Post-Loop State Flush

**What:** After the `while running:` loop exits (but inside the `try` block, before `finally`), flush state to disk one final time.
**When to use:** Ensuring no data loss on shutdown when state may have been updated mid-batch.

**Example:**
```python
try:
    while running:
        # ... process notifications ...
        if notifications:
            last_rec_id = notifications[-1]["rec_id"]
            save_state(last_rec_id, state_path)
    # Loop exited cleanly via running=False
    logging.info("Flushing state before exit: last_rec_id=%d", last_rec_id)
    save_state(last_rec_id, state_path)
finally:
    # Clean up resources (FDs, kqueue, DB connection)
    ...
```

### Anti-Patterns to Avoid

- **Raising exceptions from signal handlers:** Causes the exception to propagate from inside the blocking C call (`kq.control()`). This can leave the program in an inconsistent state if the exception is caught at an unexpected level.

- **Calling `sys.exit()` from signal handlers:** `sys.exit()` raises `SystemExit`, which is an exception. Same problem as above -- it would bypass the normal loop exit and state flush.

- **Performing I/O in signal handlers:** Signal handlers should be minimal. Do NOT call `save_state()` from inside the signal handler. Python signal handlers are not async-signal-safe for general I/O operations. Set the flag and let the main loop handle cleanup.

- **Using `os._exit()` for shutdown:** Skips all Python cleanup (finally blocks, atexit handlers, logging flush). Never use this for graceful shutdown.

- **Registering signal handlers in threads:** `signal.signal()` can only be called from the main thread. This daemon is single-threaded, so not an issue, but worth noting.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Signal registration | Custom signal dispatch | `signal.signal()` | Standard API, handles all edge cases |
| CLI argument parsing | `sys.argv` string splitting | `argparse.ArgumentParser` | Gives `--help`, type checking, error messages for free |
| Atomic state persistence | Custom file locking | Existing `save_state()` (tempfile+fsync+os.replace) | Already built in Phase 1, battle-tested pattern |

**Key insight:** Both features in this phase are thin wrappers around stdlib APIs wired into the existing event loop. There is nothing to hand-roll.

## Common Pitfalls

### Pitfall 1: PEP 475 Auto-Retry Hiding Signal Delivery

**What goes wrong:** Developer expects signal handler to immediately interrupt `kq.control()` and break the loop, but PEP 475 auto-retries the call silently.
**Why it happens:** Since Python 3.5, system calls interrupted by signals are automatically retried (with recomputed timeout) when the signal handler does not raise an exception.
**How to avoid:** Accept the design: the signal sets the flag, the loop exits after the current `kq.control()` timeout expires (at most `poll_interval` seconds). This is the correct behavior.
**Warning signs:** Shutdown takes up to 5 seconds. This is normal, not a bug.

### Pitfall 2: Removing KeyboardInterrupt Handler Prematurely

**What goes wrong:** After adding SIGINT handler, developer removes the `except KeyboardInterrupt` in `main()`, not realizing the signal handler changes the control flow.
**Why it happens:** With a custom SIGINT handler, `KeyboardInterrupt` is no longer raised. The loop exits via `running = False` instead. However, if the signal handler is registered AFTER the event loop starts (a timing bug), a Ctrl+C could still raise `KeyboardInterrupt`.
**How to avoid:** Keep the `except KeyboardInterrupt` in `main()` as a safety net. Register signal handlers BEFORE entering the event loop. Belt and suspenders.
**Warning signs:** Traceback on Ctrl+C during startup (before signal handlers registered).

### Pitfall 3: State Loss on Signal During Batch Processing

**What goes wrong:** Signal arrives while processing a batch of notifications. Some notifications were delivered via webhook but `save_state()` hasn't been called yet (it's called at the end of the batch). On restart, those notifications are replayed.
**Why it happens:** Current code calls `save_state()` after the entire batch, not after each notification.
**How to avoid:** Add a final `save_state()` call after the `while running:` loop exits. The `last_rec_id` variable holds the high-water mark of the last processed notification in the current iteration. Flushing it on shutdown prevents replay.
**Warning signs:** Duplicate webhook deliveries after restart.

### Pitfall 4: Dry-Run Flag Not Reaching Webhook Delivery

**What goes wrong:** `--dry-run` is parsed in `main()` but not threaded through to `run_watcher()` where webhook delivery happens.
**Why it happens:** `run_watcher()` signature needs a new parameter; forgetting to add it means the flag is silently ignored.
**How to avoid:** Add `dry_run=False` parameter to `run_watcher()` and use it in the webhook delivery conditional.
**Warning signs:** `--dry-run` appears to work but HTTP requests are still being sent (visible in webhook endpoint logs).

### Pitfall 5: Dry-Run Suppressing All Output

**What goes wrong:** `--dry-run` skips the webhook POST but also skips the notification logging, so the user sees nothing.
**Why it happens:** The webhook delivery code is interleaved with the notification logging code.
**How to avoid:** `--dry-run` should ONLY suppress the `post_webhook()` call. All other logging (notification detection, payload construction, classification) should remain active. Additionally, log the JSON payload that WOULD have been sent.
**Warning signs:** Running with `--dry-run` produces no output after the startup banner.

### Pitfall 6: Signal Handler Re-Entrancy

**What goes wrong:** User presses Ctrl+C twice quickly. Second signal arrives while the shutdown logging in the handler is executing.
**Why it happens:** Python signal handlers are not reentrant by default, but a second signal delivery CAN interrupt the first handler.
**How to avoid:** Keep the signal handler minimal (just set the flag). The `logging.info()` call in the handler is technically I/O but is very fast and unlikely to cause issues. However, do not put complex logic in the handler.
**Warning signs:** Garbled log output on double Ctrl+C.

## Code Examples

Verified patterns from official sources:

### Signal Handler Registration (OPER-03)
```python
# Source: https://docs.python.org/3/library/signal.html
import signal

running = True  # Already exists as module-level variable

def _shutdown_handler(signum, frame):
    global running
    logging.info("Received %s, initiating shutdown...", signal.Signals(signum).name)
    running = False

# In main(), BEFORE entering event loop:
signal.signal(signal.SIGINT, _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)
```

### argparse with --dry-run (OPER-04)
```python
# Source: https://docs.python.org/3/library/argparse.html
parser = argparse.ArgumentParser(
    description="macOS Teams Notification Interceptor"
)
parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Print webhook payloads to log without sending HTTP requests",
)
args = parser.parse_args()
# args.dry_run is True if --dry-run was passed, False otherwise
```

### Dry-Run Conditional in Event Loop
```python
# Inside run_watcher notification processing loop:
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
```

### Post-Loop State Flush
```python
# At the end of run_watcher, after while loop but before finally:
try:
    while running:
        # ... existing loop body ...
        pass

    # Graceful shutdown: flush state
    logging.info("Saving state before exit: last_rec_id=%d", last_rec_id)
    save_state(last_rec_id, state_path)
    logging.info("Shutdown complete.")
finally:
    # Resource cleanup (existing code)
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    # ... etc
```

### Startup Summary with Dry-Run Indicator
```python
# In print_startup_summary, add dry_run parameter:
def print_startup_summary(db_path, last_rec_id, config=None, dry_run=False):
    # ... existing banner ...
    if dry_run:
        logging.info("  Mode:        DRY-RUN (no HTTP requests)")
    logging.info("=" * 60)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Raw KeyboardInterrupt for shutdown | signal.signal() with flag-based handler | Always best practice, but PEP 475 (Python 3.5) made flag-based even more reliable | Flag-based is the standard pattern for daemons |
| Manual EINTR retry loops | PEP 475 auto-retry (Python 3.5+) | 2015 (Python 3.5) | No need to handle InterruptedError in kqueue code |
| sys.argv manual parsing | argparse (stdlib since Python 3.2) | 2011 (Python 3.2) | Always use argparse for CLI flags |

**Deprecated/outdated:**
- `InterruptedError` handling: Not needed since Python 3.5; PEP 475 handles EINTR automatically
- `optparse`: Deprecated since Python 3.2 in favor of argparse

## Open Questions

1. **Should dry-run also suppress state persistence?**
   - What we know: `--dry-run` is about not making HTTP requests. State persistence (rec_id tracking) is about not replaying old notifications.
   - What's unclear: If running in dry-run mode, should state still advance? If yes, a subsequent non-dry-run launch skips the dry-run notifications. If no, re-running without dry-run replays them (which could be desired for testing).
   - Recommendation: YES, still persist state in dry-run mode. The purpose of dry-run is to test the pipeline without side effects on the WEBHOOK ENDPOINT. State persistence is a local concern. If the user wants to replay, they can delete state.json.

2. **Should the startup banner explicitly show the mode (dry-run vs live)?**
   - What we know: The startup banner already shows webhook URL, bundle IDs, etc.
   - What's unclear: How prominently to surface dry-run mode.
   - Recommendation: YES, add a "Mode: DRY-RUN" line to the startup banner. Makes it immediately obvious that no HTTP requests will be made.

## Sources

### Primary (HIGH confidence)
- [Python signal module docs](https://docs.python.org/3/library/signal.html) - Signal handler registration, main-thread restriction, handler execution model, set_wakeup_fd
- [Python argparse docs](https://docs.python.org/3/library/argparse.html) - action='store_true', hyphen-to-underscore dest conversion
- [Python select module docs](https://docs.python.org/3/library/select.html) - kqueue.control() timeout and EINTR retry behavior
- [PEP 475](https://peps.python.org/pep-0475/) - Auto-retry of system calls on EINTR (Python 3.5+), kqueue.control() listed explicitly

### Secondary (MEDIUM confidence)
- [Signal Handling in Python: Custom Handlers for Graceful Shutdowns](https://johal.in/signal-handling-in-python-custom-handlers-for-graceful-shutdowns/) - Flag-based handler pattern, verified against official docs
- [How to Handle Docker Container Graceful Shutdown](https://oneuptime.com/blog/post/2026-01-16-docker-graceful-shutdown-signals/view) - Confirms flag-based pattern as standard for daemon shutdown

### Tertiary (LOW confidence)
- None. All findings verified against official Python documentation.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - All stdlib, no version concerns, all modules already imported in codebase
- Architecture: HIGH - Pattern is well-documented in official Python docs (signal, PEP 475), and the existing codebase was explicitly designed for this (module-level `running` flag, `finally` cleanup block)
- Pitfalls: HIGH - PEP 475 interaction is the only subtle point, and it is explicitly documented in Python docs for kqueue.control()

**Research date:** 2026-02-11
**Valid until:** Indefinite -- stdlib APIs are stable across Python 3.x releases
