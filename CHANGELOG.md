# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions use
[Semantic Versioning](https://semver.org/).

## [0.2.1] — 2026-10-23

### Fixed
- Sources that share a file/folder name (e.g. two extracted exports
  both called `Takeout`, or two parts with the same name in different
  folders) no longer have their contents read through each other. The
  later source used to shadow the earlier one, so Mail counts came from
  the wrong mailbox and different same-size files were reported as
  duplicates. Same-named sources are now labelled by full path and read
  independently.
- Corrupt or truncated archives no longer crash with a traceback: a zip
  entry with a bad CRC is reported as an archive problem (the Mail check
  and content hashing skip unreadable entries), a truncated tar is
  flagged the same way, and `extract` skips unreadable entries — logged,
  with no partial file left behind.
- I/O errors honour the documented exit-code contract again: `--json`
  pointing into a directory that does not exist, or `extract -o`
  naming an existing regular file, now exit 2 with a one-line error
  instead of an unhandled traceback.
- Auditing sources that contain no files at all now warns ("no files
  found in any source") instead of reporting a clean result.

### Performance
- Photos sidecar pairing is indexed by folder instead of scanning every
  media path for every sidecar (was quadratic). A synthetic 3,000-photo
  set that took ~2m52s now audits in ~9s.

### Tests
- Eight new regression tests covering all of the above; suite is now
  28 tests.

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
