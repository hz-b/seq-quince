# Changelog

All notable changes to `seq-quince` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed

- Rollback is now opt-in per move step and **off by default**. Add `rollback: true` to a device's
  entry in a preset (or pass `MoveStep(..., rollback=True)`) to have that step driven back to its
  pre-run snapshot when a later step fails. Steps without it are still stopped on failure, but no
  snapshot is taken and they are not moved back. `rollback` is now a reserved key in preset entries
  and must be `true` or `false`.

## [0.1.0] - 2026-11-13

### Added

- Initial standalone package, extracted from the `monopy` `src/scheduler` sub-package.
  - Python imports use `seq_quince`.
  - Console scripts: `scheduler_runner` and `device_runner`.

