## What this changes

<!-- One or two sentences. Link the issue it closes, if any. -->

## The failure it catches (new or changed checks only)

<!-- Every check in Takeout Audit exists because a real export broke that
     way. Describe the incident behind it. -->

## Checklist

- [ ] Runtime is still standard-library only, with no network code
- [ ] `audit` still never writes to disk; `extract` writes only into the named, empty target
- [ ] Tests added or updated, and `python -m unittest discover -s tests -v` passes
- [ ] Exit codes unchanged: `0` clean, `1` problems found, `2` tool error
- [ ] README / examples updated if behaviour or flags changed (example output re-captured from a real run, not hand-edited)
