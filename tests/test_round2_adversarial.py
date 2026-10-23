"""Round-2 adversarial tests: hostile archives, duplicate member names,
Unicode normalization in sidecar pairing, mail counting at awkward
chunk boundaries, and extract log accounting.

Every test here pins a defect that was first reproduced by hand
against v0.2.1 (or, where noted, verifies behaviour that proved
correct under attack).
"""

import io
import json
import sys
import tarfile
import tempfile
import unicodedata
import unittest
import warnings
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import takeout_audit as ta

PHOTO = b"\xff\xd8\xff\xe0" + b"fake-jpeg-data" * 16
SIDECAR = b'{"title": "x"}'


def write_zip_multi(path: Path, members: list[tuple[str, bytes]]):
    """Write a zip allowing duplicate member names (zipfile warns)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in members:
                zf.writestr(name, data)


def write_tar(path: Path, members: list[tuple[str, bytes]]):
    with tarfile.open(path, "w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


class DuplicateMemberNames(unittest.TestCase):
    """An archive may legally contain several members with the SAME
    path. Reading a member by name resolves to just one of them, which
    made --sha256 hash the last member's bytes for every same-named
    entry (false duplicates) and made extract write the wrong content.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_same_name_same_size_different_content_not_duplicates(self):
        zpath = self.dir / "dup.zip"
        write_zip_multi(zpath, [
            ("Takeout/Drive/same.txt", b"A" * 20),
            ("Takeout/Drive/same.txt", b"B" * 20),  # same size, other bytes
        ])
        for sha in (False, True):
            report = ta.audit_sources([zpath], sha256=sha)
            self.assertEqual(0, report.duplicate_files, f"sha256={sha}")
            self.assertEqual([], report.duplicate_groups, f"sha256={sha}")

    def test_zip_same_name_identical_content_is_a_duplicate(self):
        zpath = self.dir / "dup2.zip"
        write_zip_multi(zpath, [
            ("Takeout/Drive/same.txt", b"SAME" * 5),
            ("Takeout/Drive/same.txt", b"SAME" * 5),
        ])
        report = ta.audit_sources([zpath], sha256=True)
        self.assertEqual(1, report.duplicate_files)

    def test_tar_same_name_members_read_their_own_content(self):
        tpath = self.dir / "dup.tar"
        write_tar(tpath, [
            ("Takeout/Drive/same.txt", b"A" * 20),
            ("Takeout/Drive/same.txt", b"B" * 20),
        ])
        report = ta.audit_sources([tpath], sha256=True)
        self.assertEqual(0, report.duplicate_files)
        self.assertEqual(2, report.total_files)

    def test_extract_keeps_first_member_and_logs_the_collision(self):
        zpath = self.dir / "dup3.zip"
        write_zip_multi(zpath, [
            ("Takeout/Drive/report.txt", b"FIRST"),
            ("Takeout/Drive/report.txt", b"SECOND-DIFFERENT"),
        ])
        out = self.dir / "out"
        log = ta.extract_sources([zpath], out)
        self.assertEqual(b"FIRST", (out / "Drive" / "report.txt").read_bytes())
        self.assertEqual(["Takeout/Drive/report.txt"], log["written"])
        self.assertEqual(1, len(log["skipped_duplicates"]))
        self.assertIn("already written", log["skipped_duplicates"][0]["reason"])


class UnicodeSidecarPairing(unittest.TestCase):
    """The same file name stored NFC in one place and NFD in another
    (typical for sets zipped on macOS) must still pair with its
    sidecar instead of being reported as both missing and orphaned.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nfc_media_pairs_with_nfd_sidecar_and_vice_versa(self):
        nfc_media = unicodedata.normalize("NFC", "café.jpg")
        nfd_sidecar = unicodedata.normalize("NFD", "café.jpg.json")
        nfd_media = unicodedata.normalize("NFD", "thé.jpg")
        nfc_sidecar = unicodedata.normalize("NFC", "thé.jpg.json")
        self.assertNotEqual(nfc_media, unicodedata.normalize("NFD", "café.jpg"))
        zpath = self.dir / "uni.zip"
        write_zip_multi(zpath, [
            (f"Takeout/Google Photos/{nfc_media}", PHOTO),
            (f"Takeout/Google Photos/{nfd_sidecar}", SIDECAR),
            (f"Takeout/Google Photos/{nfd_media}", PHOTO),
            (f"Takeout/Google Photos/{nfc_sidecar}", SIDECAR),
        ])
        report = ta.audit_sources([zpath])
        self.assertEqual(2, report.photos_media)
        self.assertEqual([], report.photos_missing_sidecar)
        self.assertEqual([], report.photos_orphan_sidecars)


class MailCountingBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_separator_split_across_chunk_boundary_is_counted(self):
        # Place a separator so "Fro" ends the first 1 MiB chunk and
        # "m " starts the next: the counter must stitch chunks.
        chunk = ta.CHUNK
        first = b"From a@b.c\n"
        filler = b"A" * (chunk - len(first) - 3)  # then "\n" + "Fro"
        body = first + filler + b"\nFrom b@c.d\nbody\n"
        self.assertEqual(chunk, len(first) + len(filler) + 3)
        mbox = self.dir / "split.mbox"
        mbox.write_bytes(body)
        entries, _archives, openers = ta.load_sources([self.dir])
        entry = next(e for e in entries if e.path.endswith(".mbox"))
        self.assertEqual(2, ta.count_mbox_messages(entry, openers))

    def test_many_messages_and_one_giant_line_counted(self):
        messages = []
        for i in range(5000):
            messages.append(b"From u%d@x.y\nSubject: %d\n\nbody\n" % (i, i))
        messages.append(b"From last@x.y\n" + b"A" * (3 * 1024 * 1024))
        mbox = self.dir / "big.mbox"
        mbox.write_bytes(b"".join(messages))
        entries, _archives, openers = ta.load_sources([self.dir])
        entry = next(e for e in entries if e.path.endswith(".mbox"))
        self.assertEqual(5001, ta.count_mbox_messages(entry, openers))


class ExtractAccounting(unittest.TestCase):
    def test_file_named_takeout_is_accounted_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            zpath = d / "root.zip"
            write_zip_multi(zpath, [
                ("Takeout", b"a file literally named Takeout"),
                ("Takeout/Drive/real.txt", b"real"),
            ])
            out = d / "out"
            log = ta.extract_sources([zpath], out)
            self.assertEqual(["Takeout/Drive/real.txt"], log["written"])
            skipped_paths = [s["path"] for s in log["skipped_duplicates"]]
            self.assertIn("Takeout", skipped_paths)
            reason = log["skipped_duplicates"][0]["reason"]
            self.assertIn("library root", reason)
            # The on-disk log agrees with the returned one.
            disk = json.loads((out / "_takeout-audit-log.json").read_text())
            self.assertEqual(log["written"], disk["written"])


if __name__ == "__main__":
    unittest.main()
