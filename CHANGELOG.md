# Changelog

All notable changes to this project will be documented in this file.

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
