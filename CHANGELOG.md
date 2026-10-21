# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions use
[Semantic Versioning](https://semver.org/).

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
