# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions use
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-10-23

### Added
- Mail deep check: every `.mbox` in the audited set is streamed line by
  line and its messages counted (mbox `From ` separators; escaped
  `>From ` body lines are not counted). Per-file and total counts appear
  in the text report and in the JSON report under `mail`.

### Tests
- Five new tests for the Mail check: single-message fixture, multi-message
  mbox with escaped separators, empty mbox, directory sources, and the
  JSON `mail` section. Suite is now 20 tests.

## [0.1.0] — 2026-10-22

### Added
- `audit` command (read-only): archive integrity (CRC/read tests), Takeout
  part-sequence completeness, per-service and per-extension inventory,
  duplicate detection across parts and exports, Google Photos sidecar
  pairing across the whole set.
- `extract` command: unified, deduplicated extraction with a JSON skip log;
  refuses non-empty targets.
- Inputs: `.zip`, `.tgz`/`.tar.gz`, and extracted directories, mixable in
  one run.
- `--json` full report, `--sha256` cryptographic hashing mode, `--strict`
  exit-code control. Exit codes 0/1/2.
- Demo-set generator in `examples/` and a 15-test suite including
  subprocess end-to-end coverage.
