# Takeout Audit

**An offline auditor for Google Takeout archive sets.** You downloaded your
Google Takeout — maybe 3 archive parts, maybe 30, maybe for the second time
this year. Before you trust that download with the only copy of a decade of
photos, mail and files, Takeout Audit tells you whether the set is complete,
what is actually in it, how much of it is duplicated, and whether your Photos
metadata survived the trip. Then it can hand you one clean, unified,
deduplicated folder.

Everything runs **on your machine, offline**. Your archives are never
uploaded anywhere — which is more than the cloud "transfer" services that
currently monetise this exact pain can say.

## Why this exists

Leaving Google — for Immich, Nextcloud, an external drive, or just peace of
mind — starts with Takeout, and Takeout is where the trouble starts:

- Exports arrive as **many numbered parts** (1–50 GB each). Downloads fail,
  links expire after about a week, and nothing tells you whether the set you
  ended up with is complete.
  ([SysTools troubleshooting guide](https://www.systoolsgroup.com/updates/google-takeout-errors-issues/),
  [MultCloud](https://www.multcloud.com/tutorials/google-takeout-not-working-1003.amp.html))
- **A photo and its metadata JSON can land in different parts** — "a photo
  might be in `takeout-001.zip` while its JSON is in `takeout-003.zip`" — so
  processing parts separately silently loses dates and locations.
  ([Metadata Fixer](https://metadatafixer.com/learn/google-takeout-duplicate-photos-explained))
- Photos appear **once per album plus once in the year folder**, edited
  versions add `-EDIT` copies, and a fresh export overlaps almost entirely
  with last year's. Nobody tells you how much of your 200 GB is the same
  bytes three times.
- The tools that do exist either **upload your archive to someone's cloud**
  (transfer services), fix **Photos metadata only** (paid desktop apps), or
  are **tied to one destination** (Immich importers). The audit step itself —
  *is this set trustworthy, and what is in it?* — had no offline tool.

Takeout Audit is that audit step.

## What it checks

| Check | What it catches |
|---|---|
| **Archive integrity** | Corrupt or truncated downloads (full CRC test per zip; read test per tar) |
| **Part completeness** | Missing parts in a `takeout-…-001.zip … -NNN.zip` sequence |
| **Inventory** | File counts and sizes per Google service and per file type |
| **Duplicates** | Identical content across parts and across successive exports, with wasted space |
| **Photos sidecar pairing** | Media files whose metadata `.json` is missing *anywhere in the set*, and orphan sidecars with no media |
| **Unified extract** | One folder tree with duplicates removed and a JSON log of every skip |

## Install

Requires Python 3.9+. No third-party dependencies — standard library only.

```bash
pip install .            # from a clone of this repository
# or just run the single file:
python3 takeout_audit.py --help
```

## Usage

Audit a set of archive parts (`.zip`, `.tgz`/`.tar.gz`) and/or extracted
Takeout folders, in any combination:

```bash
takeout-audit audit ~/Downloads/takeout-*.zip
takeout-audit audit part1.zip part2.zip ~/old-exports/2024-takeout/
takeout-audit audit ~/Downloads/takeout-*.zip --json report.json
takeout-audit audit ~/Downloads/takeout-*.zip --sha256   # cryptographic hashing
takeout-audit audit ~/Downloads/takeout-*.zip --strict   # warnings also fail
```

Extract one unified, deduplicated library (the `Takeout/` prefix is
stripped; first copy of each file wins; every skip is logged):

```bash
takeout-audit extract ~/Downloads/takeout-*.zip -o ~/Pictures/my-library
```

### Real output

From the synthetic demo set in [`examples/`](examples/) — part 002 is
deliberately missing, one file is duplicated across parts, and the photo's
sidecar lives in the other part:

```
Takeout Audit — offline report
============================================================
Sources audited : 2
  - takeout-20260101T120000Z-001.zip [zip] 3 files — OK
  - takeout-20260101T120000Z-003.zip [zip] 3 files — OK

Total: 6 files, 1.2 KB

By service:
  Google Photos                       2 files       641 B
  Drive                               3 files       571 B
  Mail                                1 files        66 B

Duplicates: 1 group(s), 1 redundant file(s), 276 B wasted
  276 B x2:
    - takeout-20260101T120000Z-001.zip:Takeout/Drive/report.txt
    - takeout-20260101T120000Z-003.zip:Takeout/Drive/report.txt

Google Photos: 1 media file(s), 0 missing sidecar, 0 orphan sidecar(s)

PROBLEM: missing archive part(s) for takeout-20260101T120000Z: 002
Warning: 1 duplicated file(s), 276 B wasted
Result: PROBLEMS found (see above).
```

Note what it got right: the photo in part 001 is **not** reported as missing
its sidecar, because the sidecar is present in part 003.

### Exit codes

Shared with ShortsQC and DatasetDoctor conventions:

- `0` — audit clean (or extract succeeded)
- `1` — problems found (corrupt archive, missing part); with `--strict`,
  warnings (duplicates, sidecar issues) also return `1`
- `2` — usage / I/O error (missing path, unsupported source, non-empty
  extract target)

## How duplicate detection works

Files are grouped by size first; only same-size files are hashed. The
default hash is CRC-32 of the content — for zip archives that value is read
from the central directory, so even a very large set can be audited without
decompressing every byte. `--sha256` switches to SHA-256 (zip entries are
then decompressed and hashed) for cryptographic certainty. Directory and
tar inputs are always hashed by streaming their content once.

## What it does *not* do

- It does **not** write metadata back into your photos. Once the audit says
  your sidecars are all present, tools like
  [GPTH](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper) or
  [immich-go](https://github.com/simulot/immich-go) do that step well.
- It does not download anything from Google, and it never modifies your
  archives. `audit` is strictly read-only; `extract` writes only to the
  target folder you name and refuses a non-empty one.
- Sidecar pairing uses Takeout's naming rules (sidecar = file name +
  `.json`, truncated to 51 characters). Exotic renames can produce false
  orphan/missing reports — the JSON report lists every case so you can
  eyeball them.

## Roadmap

- v0.2: per-service deep checks (mbox message counts, YouTube history
  parsing), HTML report.
- v0.3: compare two audit reports ("what changed between my 2025 and 2026
  exports?").

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Google. "Google Takeout"
is a trademark of Google LLC; this tool only reads files you exported
yourself.
