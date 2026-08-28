# Changelog

All notable changes to this project will be documented in this file.

## [0.1.22] - 2026-08-29

### Changed

- Remove the watchdog-owned `max_compactions` thread-health trigger. Codex may
  compact a thread any number of times, and historical or native compaction
  events can no longer cause an unnecessary thread rotation.
- Keep the legacy `--thread-max-compactions` option parseable but ignored, so
  existing tmux and guardian configurations remain compatible.
- Ordinary fatal recovery and the 5m0s compact flow continue the pinned Codex
  thread. Fresh-thread recovery remains limited to an upstream access denial
  or a compact timeout, plus explicitly configured watchdog health detectors.

## [0.1.21] - 2026-08-28

### Fixed

- Retry Enter when recovery text remains in the Codex composer or interactive
  shell input, so shared recovery steps cannot silently stop at an unsent
  prompt.

## [0.1.20] - 2026-08-28

### Changed

- Increase the default watchdog rollout-size threshold from 128 MiB to
  512 MiB.

## [0.1.19] - 2026-08-28

### Changed

- Remove watchdog actions based on rollout context usage or
  model_context_window; Codex now exclusively owns context limits and
  automatic compaction.
- Leave Codex's native context-window exhaustion message untouched instead of
  compacting or rotating the thread from the monitor.
- Keep --thread-max-context-tokens as an ignored compatibility option for
  existing launch scripts.

## [0.1.18] - 2026-08-27

### Added

- Detect consecutive repeated assistant output and repeated shell `exec` calls
  inside one active Codex turn, then route the loop through the existing bounded
  handoff and fresh-thread recovery flow.
- Expose `--thread-max-repeated-content` and
  `--thread-max-repeated-commands`; both default to three and accept `0` to
  disable their detector.

### Changed

- Arm repetition detection only for rollout events observed after the monitor
  starts, preventing a monitor restart from replaying historical loops.
- Exclude `write_stdin` and `wait` polling calls from repeated-command signals.

## [0.1.17] - 2026-08-27

### Added

- Persist recovery counters and successful compaction counts in the watchdog
  session binding so monitor and guardian reconnects cannot reset retry state.
- Track rollout size, context usage, compaction count, no-progress tokens, and
  no-event duration locally without asking the Codex model to poll status.
- Write a bounded private handoff and rotate to a fresh Codex thread when a
  long-running thread crosses a health threshold or compaction waits time out.

### Changed

- Reset recovery counters only after a new verified successful task completion
  or successful context compaction; `--max-recoveries 0` remains unlimited.
- Aggregate repeated redraws of one rollout incident into one first log line
  and one suppressed-count summary.

## [0.1.16] - 2026-08-17

### Added

- Treat `502 Bad Gateway: Upstream access denied` as a banned-thread failure:
  start a fresh Codex thread instead of resuming the rejected thread, extract
  the previous Goal Objective from its rollout, and instruct the new thread to
  recreate that Goal after reconciling the worktree, ACTIVE plan, canonical
  project records, and potentially stale handoff state.
- Rebind tmux and the persistent watchdog session to the rotated thread while
  preserving the recovery count and cooldown across repeated automatic
  rotations.
- Preserve the manual-review boundary when the banned thread's Goal was
  blocked instead of using rotation to resume product execution.

### Fixed

- Prefer the latest fatal row when guardian inspects a screen containing
  several historical errors.
- Recognize `Goal active Objective:` after Codex exits to the shell, allowing
  guardian takeover to retain the active-Goal recovery gate.

## [0.1.15] - 2026-08-17

### Fixed

- Re-enable and start the matching installed guardian service whenever
  `codex-watch` starts a session, so a previously disabled guardian cannot
  remain detached from the watchdog lifecycle.
- Let guardian recover a new rollout-correlated fatal error that was already
  visible before the monitor pipe attached, even when that pipe is currently
  active.
- Limit active-pipe inspection to the guardian handoff window and atomically
  claim fatal incidents across monitor and guardian, preventing duplicate
  recovery attempts and repeated scans of large rollout files.

## [0.1.14] - 2026-08-16

### Fixed

- Allow a new rollout-correlated fatal error to enter recovery while the Goal
  is stalled, then resume that stalled Goal only after the process restart.
- Keep manually stalled Goals paused during startup, history replay, Codex
  updates, and redraws of already handled fatal incidents.

## [0.1.13] - 2026-08-10

### Fixed

- Treat `stream disconnected before completion: Our servers are currently
  overloaded. Please try again later.` as a fatal upstream error and recover
  the pinned thread through the standard primary-model retry flow.
- Keep the overload recovery out of the Luna compaction path.

## [0.1.12] - 2026-08-10

### Fixed

- Keep `Goal blocked (/goal resume)` paused for human review instead of
  automatically sending `/goal resume` or a fallback continuation prompt.
- Preserve fatal process recovery for blocked Goals while restoring the pinned
  thread into a paused state. A blocked status by itself does not trigger a
  recovery attempt or cooldown.
- Apply the blocked-Goal policy consistently during manual startup, delayed
  history replay, monitor recovery, guardian takeover, and Codex update restart.

## [0.1.11] - 2026-08-03

### Fixed

- Run the tmux fallback shell with command history disabled and `HISTFILE`
  isolated to `/dev/null`, preventing watchdog recovery commands from being
  written to the host user's shell history.
- Skip Bash startup files for the fallback shell so host configuration cannot
  silently restore the shared history file.

## [0.1.10] - 2026-07-30

### Fixed

- Correlate visible fatal rows with the matching rollout `task_complete`
  `turn_id` before recovery, so inline TUI redraws cannot replay an old 503 or
  capacity error and interrupt a newly resumed Goal.
- Persist the last handled fatal turn in tmux and apply the same deduplication
  in the pipe monitor and guardian. A later turn with the same error still
  enters the configured unlimited immediate-then-delayed recovery flow.

## [0.1.9] - 2026-07-30

### Fixed

- Suppress fatal recovery when Codex is no longer inside an active Goal, such
  as after `Goal achieved`, so stale terminal errors do not repeatedly restart
  a completed session and inject continuation prompts.
- Allow fatal recovery only when the latest observed Goal state is `Pursuing
  goal` or `Goal blocked (/goal resume)`; guardian visible-screen recovery uses
  the same gate as the pipe monitor.

## [0.1.8] - 2026-07-26

### Fixed

- Persist each watchdog session's pinned Codex thread outside tmux and restore
  that exact thread when the named tmux session no longer exists.
- Update the persistent binding after Codex `/clear`, while keeping subagent and
  unrelated Codex rollouts excluded from thread rebinding.
- Add `--new` to explicitly replace a saved watchdog-session binding with a
  fresh Codex thread; keep `--resume` as an explicit latest-thread-by-directory
  operation.

## [0.1.7] - 2026-07-26

### Changed

- Treat terminal HTTP 401 errors, including `API DISABLE`, as fatal errors
  handled by the standard immediate-then-delayed recovery flow.

## [0.1.6] - 2026-07-23

### Fixed

- Rebind the watched thread after Codex `/clear` by resolving the newest
  top-level CLI rollout opened by the tmux pane process.
- Ignore subagent rollouts and unrelated Codex processes while rebinding, then
  persist the new thread ID and reset its recovery count.

## [0.1.5] - 2026-07-22

### Fixed

- Treat the terminal `Selected model is at capacity` warning as a fatal error
  handled by the standard immediate-then-delayed recovery flow.
- Wait for tmux-injected text to settle before pressing Enter, preventing Codex
  from leaving `/quit`, `/compact`, or Goal resume commands as multiline input.

## [0.1.4] - 2026-07-22

### Fixed

- Handle the Codex update picker before a fresh session has created its thread
  ID, preventing an unmanaged tmux session from being left behind.
- Pin a fresh blank Codex session from its shell snapshot before the rollout
  file exists, without inheriting an outer Codex process's internal variables.
- Verify the installed Codex version after the official updater exits and run
  `codex update` once more when the requested version was not installed.
- Resume pinned threads only after update verification succeeds, while keeping
  interrupted updates recoverable by the output monitor and guardian.

## [0.1.3] - 2026-07-21

### Fixed

- Resume a visible paused Goal when `codex-watch` is started or reattached
  manually.
- Detect delayed `Resume paused goal?` pickers in the output monitor and select
  `Resume goal` without injecting fallback text into ordinary idle sessions.
- Explain how to connect an unmanaged tmux session to an existing Codex thread.

## [0.1.2] - 2026-07-20

### Changed

- Treat terminal HTTP 402 responses as fatal errors handled by the standard
  primary-model recovery flow.
- Attempt the first fatal recovery immediately, then wait five minutes before
  every subsequent retry while keeping recovery attempts unlimited by default.
- Apply the cooldown as a real serialized delay instead of discarding fatal
  events observed during the cooldown window.

## [0.1.1] - 2026-07-19

### Fixed

- Wait for delayed paused-Goal prompts during large session replays instead of
  sending the continuation text after a fixed five-second startup delay.
- Avoid injecting fallback text when the resumed Goal is already active.

## [0.1.0] - 2026-07-19

### Added

- tmux-based Codex CLI session launcher with pinned thread recovery.
- Luna compaction and Sol resume flow for upstream stalls and context exhaustion.
- Sol-only recovery for retryable HTTP, network, and structured upstream errors.
- Codex self-update restart handling and persisted Goal resume support.
- Unlimited serialized recovery attempts by default.
- Python package metadata, console scripts, XDG state paths, and user systemd unit.
