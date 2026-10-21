#!/usr/bin/env python3
"""Generate a small, fake two-part Google Takeout set for the demo.

Creates `takeout-demo/` next to this script with:
  - takeout-20260101T120000Z-001.zip  (a photo, a Drive file, a mailbox)
  - takeout-20260101T120000Z-003.zip  (the photo's JSON sidecar, a duplicate
                                       of the Drive file, one unique file)

Part 002 is deliberately missing, the photo's metadata lives in the other
part, and one file is duplicated — the three things Takeout Audit exists to
catch. All content is synthetic.
"""
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "takeout-demo"

PHOTO = b"\xff\xd8\xff\xe0" + b"demo-photo-bytes" * 32
SIDECAR = b'{"title": "IMG_0001.jpg", "photoTakenTime": {"timestamp": "1704067200"},' \
          b' "geoData": {"latitude": 35.68, "longitude": 139.69}}'
REPORT = b"Demo quarterly report.\n" * 12
MBOX = b"From demo@example.com\nSubject: Demo\n\nHello from the demo mailbox.\n"


def main():
    OUT.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT / "takeout-20260101T120000Z-001.zip", "w",
                         zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Takeout/Google Photos/IMG_0001.jpg", PHOTO)
        zf.writestr("Takeout/Drive/report.txt", REPORT)
        zf.writestr("Takeout/Mail/All mail Including Spam and Trash.mbox", MBOX)
    with zipfile.ZipFile(OUT / "takeout-20260101T120000Z-003.zip", "w",
                         zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Takeout/Google Photos/IMG_0001.jpg.json", SIDECAR)
        zf.writestr("Takeout/Drive/report.txt", REPORT)
        zf.writestr("Takeout/Drive/notes.txt", b"Unique demo notes.\n")
    print(f"Demo set written to {OUT}")


if __name__ == "__main__":
    main()
