# Contributing

Thanks for your interest in Takeout Audit.

## Principles

- **Offline is the product.** No network calls, no telemetry, no optional
  "cloud features". Pull requests that add any are out of scope by design.
- **Standard library only** at runtime. If a feature seems to need a
  dependency, it probably needs a smaller design instead.
- **Never mutate the user's archives.** `audit` is read-only; `extract`
  writes only into a user-named, empty target directory.
- **Honesty in docs.** Example output in the README is captured from real
  runs on the synthetic demo set, never hand-written.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 examples/make_demo_takeout.py
python3 takeout_audit.py audit examples/takeout-demo/*.zip
```

- Python 3.9+ compatibility is required (CI runs 3.9–3.12).
- New behaviour needs a test; bug fixes need a regression test.
- Sample Takeout files in tests must be synthetic — never commit real
  exports.

## Reporting issues

Use the issue templates. For anything security-related, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.
