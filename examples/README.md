# Examples

Generate the synthetic demo Takeout set (two parts, part 002 deliberately
missing, one duplicated file, one photo whose metadata sidecar is in the
other part):

```bash
python3 make_demo_takeout.py
python3 ../takeout_audit.py audit takeout-demo/*.zip
```

Expected verdict: exit code `1` — "missing archive part(s) …
takeout-20260101T120000Z: 002", one duplicate group, and **no** missing
sidecar for `IMG_0001.jpg` (its sidecar is in part 003).

Then extract the unified library:

```bash
python3 ../takeout_audit.py extract takeout-demo/*.zip -o my-library
# -> Wrote 5 file(s), skipped 1 duplicate(s)
# my-library/{Google Photos,Drive,Mail}/... plus _takeout-audit-log.json
```

All demo content is synthetic; no real user data is included.
