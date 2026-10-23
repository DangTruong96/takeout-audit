import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import takeout_audit as ta

PHOTO = b"\xff\xd8\xff\xe0" + b"fake-jpeg-data" * 16
SIDECAR = b'{"title": "IMG_0001.jpg", "photoTakenTime": {"timestamp": "1700000000"}}'
REPORT = b"quarterly report contents\n" * 8
MBOX = b"From someone@example.com\nSubject: hi\n\nbody\n"


def write_zip(path: Path, files: dict[str, bytes]):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)


class Fixture(unittest.TestCase):
    """A two-part Takeout set: part 002 is missing, a photo's sidecar is in
    the other part, and one Drive file is duplicated across parts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.part1 = self.dir / "takeout-20260101T120000Z-001.zip"
        self.part3 = self.dir / "takeout-20260101T120000Z-003.zip"
        write_zip(self.part1, {
            "Takeout/Google Photos/IMG_0001.jpg": PHOTO,
            "Takeout/Drive/report.txt": REPORT,
            "Takeout/Mail/All mail Including Spam and Trash.mbox": MBOX,
        })
        write_zip(self.part3, {
            "Takeout/Google Photos/IMG_0001.jpg.json": SIDECAR,
            "Takeout/Drive/report.txt": REPORT,  # duplicate of part 1 copy
            "Takeout/Drive/notes.txt": b"unique notes",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_inventory_and_services(self):
        report = ta.audit_sources([self.part1, self.part3])
        self.assertEqual(report.total_files, 6)
        self.assertEqual(report.services["Google Photos"]["files"], 2)
        self.assertEqual(report.services["Drive"]["files"], 3)
        self.assertEqual(report.services["Mail"]["files"], 1)

    def test_missing_part_detected(self):
        report = ta.audit_sources([self.part1, self.part3])
        key = "takeout-20260101T120000Z"
        self.assertEqual(report.missing_parts[key], [2])
        self.assertTrue(any("missing archive part" in p
                            for p in report.problems))

    def test_duplicate_across_parts(self):
        report = ta.audit_sources([self.part1, self.part3])
        self.assertEqual(len(report.duplicate_groups), 1)
        self.assertEqual(report.duplicate_files, 1)
        self.assertEqual(report.wasted_bytes, len(REPORT))

    def test_sidecar_split_across_parts_is_paired(self):
        report = ta.audit_sources([self.part1, self.part3])
        self.assertEqual(report.photos_media, 1)
        self.assertEqual(report.photos_missing_sidecar, [])
        self.assertEqual(report.photos_orphan_sidecars, [])

    def test_missing_sidecar_and_orphan(self):
        lonely = self.dir / "takeout-20260202T000000Z-001.zip"
        write_zip(lonely, {
            "Takeout/Google Photos/IMG_9999.jpg": PHOTO,
            "Takeout/Google Photos/IMG_5555.jpg.json": SIDECAR,
        })
        report = ta.audit_sources([lonely])
        self.assertEqual(len(report.photos_missing_sidecar), 1)
        self.assertEqual(len(report.photos_orphan_sidecars), 1)

    def test_corrupt_archive_flagged(self):
        broken = self.dir / "takeout-broken.zip"
        broken.write_bytes(b"this is not a zip at all" * 10)
        report = ta.audit_sources([broken])
        self.assertFalse(report.archives[0].ok)
        self.assertTrue(report.problems)

    def test_directory_source_dedupe(self):
        root = self.dir / "extracted"
        (root / "Takeout" / "Drive").mkdir(parents=True)
        (root / "Takeout" / "Drive" / "a.txt").write_bytes(REPORT)
        (root / "Takeout" / "Drive" / "b.txt").write_bytes(REPORT)
        report = ta.audit_sources([root])
        self.assertEqual(report.duplicate_files, 1)

    def test_tgz_source(self):
        tgz = self.dir / "takeout-20260303T000000Z-001.tgz"
        with tarfile.open(tgz, "w:gz") as tf:
            for name, data in {
                "Takeout/Drive/x.txt": REPORT,
                "Takeout/Drive/y.txt": REPORT,
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        report = ta.audit_sources([tgz])
        self.assertEqual(report.total_files, 2)
        self.assertEqual(report.duplicate_files, 1)


THREE_MESSAGE_MBOX = (
    b"From alice@example.com Mon Jan  5 10:00:00 2026\n"
    b"Subject: one\n\nfirst body\n"
    b"From bob@example.com Mon Jan  5 11:00:00 2026\n"
    b"Subject: two\n\n"
    b">From a line in the body, escaped, not a separator\n"
    b"Fromage is not a separator either\n"
    b"From carol@example.com Mon Jan  5 12:00:00 2026\n"
    b"Subject: three\n\nthird body"  # no trailing newline, on purpose
)


class MailCounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixture_mbox_counted(self):
        part1 = self.dir / "takeout-20260101T120000Z-001.zip"
        write_zip(part1, {
            "Takeout/Mail/All mail Including Spam and Trash.mbox": MBOX,
        })
        report = ta.audit_sources([part1])
        self.assertEqual(report.mail_messages, 1)
        self.assertEqual(len(report.mail_mbox_files), 1)
        entry = report.mail_mbox_files[0]
        self.assertEqual(entry["messages"], 1)
        self.assertEqual(
            entry["path"],
            "Takeout/Mail/All mail Including Spam and Trash.mbox",
        )

    def test_multiple_messages_and_escapes(self):
        arc = self.dir / "takeout-mail.zip"
        write_zip(arc, {
            "Takeout/Mail/inbox.mbox": THREE_MESSAGE_MBOX,
            "Takeout/Mail/empty.mbox": b"",
        })
        report = ta.audit_sources([arc])
        self.assertEqual(report.mail_messages, 3)
        counts = {m["path"]: m["messages"] for m in report.mail_mbox_files}
        self.assertEqual(counts["Takeout/Mail/inbox.mbox"], 3)
        self.assertEqual(counts["Takeout/Mail/empty.mbox"], 0)

    def test_mbox_in_directory_source(self):
        root = self.dir / "extracted"
        (root / "Takeout" / "Mail").mkdir(parents=True)
        (root / "Takeout" / "Mail" / "inbox.mbox").write_bytes(
            THREE_MESSAGE_MBOX
        )
        report = ta.audit_sources([root])
        self.assertEqual(report.mail_messages, 3)

    def test_non_mbox_files_not_counted(self):
        arc = self.dir / "takeout-nomail.zip"
        write_zip(arc, {"Takeout/Drive/readme.txt": REPORT})
        report = ta.audit_sources([arc])
        self.assertEqual(report.mail_messages, 0)
        self.assertEqual(report.mail_mbox_files, [])

    def test_mail_section_in_json_dict(self):
        arc = self.dir / "takeout-json.zip"
        write_zip(arc, {"Takeout/Mail/inbox.mbox": THREE_MESSAGE_MBOX})
        data = ta.audit_sources([arc]).to_dict()
        self.assertEqual(data["mail"]["total_messages"], 3)
        self.assertEqual(len(data["mail"]["mbox_files"]), 1)
        self.assertEqual(data["version"], "0.2.2")


class CliSmoke(unittest.TestCase):
    """End-to-end: run the CLI as a subprocess on realistic fixtures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.part1 = self.dir / "takeout-20260101T120000Z-001.zip"
        self.part3 = self.dir / "takeout-20260101T120000Z-003.zip"
        write_zip(self.part1, {
            "Takeout/Google Photos/IMG_0001.jpg": PHOTO,
            "Takeout/Drive/report.txt": REPORT,
        })
        write_zip(self.part3, {
            "Takeout/Google Photos/IMG_0001.jpg.json": SIDECAR,
            "Takeout/Drive/report.txt": REPORT,
        })
        self.script = Path(__file__).resolve().parent.parent / "takeout_audit.py"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(self.script), *args],
            capture_output=True, text=True,
        )

    def test_audit_cli_exit_1_and_json(self):
        out_json = self.dir / "report.json"
        proc = self.run_cli("audit", str(self.part1), str(self.part3),
                            "--json", str(out_json))
        self.assertEqual(proc.returncode, 1)  # missing part 002
        self.assertIn("Takeout Audit", proc.stdout)
        data = json.loads(out_json.read_text())
        self.assertEqual(data["total_files"], 4)
        self.assertEqual(data["duplicates"]["duplicate_files"], 1)
        self.assertEqual(data["photos"]["missing_sidecar"], [])

    def test_clean_set_exit_0(self):
        clean = self.dir / "takeout-20260404T000000Z-001.zip"
        write_zip(clean, {"Takeout/Drive/only.txt": b"hello"})
        proc = self.run_cli("audit", str(clean))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("clean", proc.stdout)

    def test_missing_path_exit_2(self):
        proc = self.run_cli("audit", str(self.dir / "nope.zip"))
        self.assertEqual(proc.returncode, 2)

    def test_extract_cli_unifies_and_dedupes(self):
        out = self.dir / "library"
        proc = self.run_cli("extract", str(self.part1), str(self.part3),
                            "-o", str(out))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        written = sorted(
            p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()
        )
        self.assertEqual(written, [
            "Drive/report.txt",
            "Google Photos/IMG_0001.jpg",
            "Google Photos/IMG_0001.jpg.json",
            "_takeout-audit-log.json",
        ])
        log = json.loads((out / "_takeout-audit-log.json").read_text())
        self.assertEqual(len(log["skipped_duplicates"]), 1)

    def test_extract_refuses_nonempty_output(self):
        out = self.dir / "busy"
        out.mkdir()
        (out / "keep.txt").write_text("mine")
        proc = self.run_cli("extract", str(self.part1), "-o", str(out))
        self.assertEqual(proc.returncode, 2)

    def test_extract_skips_path_traversal(self):
        evil = self.dir / "takeout-evil.zip"
        write_zip(evil, {
            "Takeout/../evil.txt": b"pwned",
            "Takeout/Drive/good.txt": b"fine",
        })
        out = self.dir / "lib2"
        proc = self.run_cli("extract", str(evil), "-o", str(out))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.dir / "evil.txt").exists())
        self.assertFalse((out.parent / "evil.txt").exists())
        self.assertTrue((out / "Drive" / "good.txt").exists())
        log = json.loads((out / "_takeout-audit-log.json").read_text())
        self.assertTrue(any(s.get("reason") for s in log["skipped_duplicates"]))

    def test_help(self):
        proc = self.run_cli("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("audit", proc.stdout)


class SameBasenameSources(unittest.TestCase):
    """Regression: two sources with the same file/folder name (two
    extracted exports are both called "Takeout") must never have their
    contents read through each other."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_export(self, parent: Path, tag: bytes, mbox: bytes) -> Path:
        root = parent / "Takeout"
        (root / "Drive").mkdir(parents=True)
        (root / "Mail").mkdir(parents=True)
        (root / "Drive" / "same.txt").write_bytes(tag + b"x" * 100)
        (root / "Mail" / "inbox.mbox").write_bytes(mbox)
        return root

    def test_same_name_directories_keep_their_own_content(self):
        old = self._make_export(
            self.dir / "2024", b"OLD-",
            b"From a@x\nSubject: old\n\nbody\n",
        )
        new = self._make_export(self.dir / "2026", b"NEW-", THREE_MESSAGE_MBOX)
        report = ta.audit_sources([old, new])
        # Different same-size contents are NOT duplicates...
        self.assertEqual(report.duplicate_files, 0)
        # ...each mbox is counted from its own directory (1 + 3)...
        self.assertEqual(report.mail_messages, 4)
        # ...and the report can tell the two sources apart.
        labels = {a.source for a in report.archives}
        self.assertEqual(len(labels), 2)

    def test_same_name_zips_with_sha256(self):
        (self.dir / "a").mkdir()
        (self.dir / "b").mkdir()
        write_zip(self.dir / "a" / "takeout-p.zip",
                  {"Takeout/Drive/f.txt": b"A" * 50})
        write_zip(self.dir / "b" / "takeout-p.zip",
                  {"Takeout/Drive/f.txt": b"B" * 50})
        report = ta.audit_sources(
            [self.dir / "a" / "takeout-p.zip",
             self.dir / "b" / "takeout-p.zip"],
            sha256=True,
        )
        self.assertEqual(report.duplicate_files, 0)


class CorruptContentDoesNotCrash(unittest.TestCase):
    """Regression: corrupt/truncated archives are the tool's home turf —
    they must be reported as problems, never crash with a traceback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_zip_entry_flagged_not_fatal(self):
        path = self.dir / "takeout-corrupt.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("Takeout/Mail/inbox.mbox", THREE_MESSAGE_MBOX)
            zf.writestr("Takeout/Drive/ok.txt", b"fine content")
        raw = bytearray(path.read_bytes())
        i = raw.find(b"From alice@example.com")
        raw[i + 12] ^= 0xFF  # break the mbox entry's CRC
        path.write_bytes(bytes(raw))
        report = ta.audit_sources([path])
        self.assertFalse(report.archives[0].ok)
        self.assertTrue(report.problems)
        # The unreadable mbox is not silently counted.
        self.assertEqual(report.mail_mbox_files, [])

    def test_truncated_tgz_flagged_not_fatal(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("Takeout/Drive/a.txt", "Takeout/Drive/b.txt"):
                data = b"z" * 5000
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        path = self.dir / "takeout-trunc.tgz"
        blob = buf.getvalue()
        path.write_bytes(blob[: len(blob) // 2])
        report = ta.audit_sources([path])
        self.assertFalse(report.archives[0].ok)
        self.assertTrue(report.problems)


class CliErrorHandling(unittest.TestCase):
    """Regression: I/O errors are exit 2 with a message, per the README's
    exit-code contract — not an unhandled traceback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.script = Path(__file__).resolve().parent.parent / "takeout_audit.py"
        self.src = self.dir / "src"
        (self.src / "Takeout" / "Drive").mkdir(parents=True)
        (self.src / "Takeout" / "Drive" / "a.txt").write_bytes(b"hello")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(self.script), *args],
            capture_output=True, text=True,
        )

    def test_json_into_missing_directory_is_exit_2(self):
        proc = self.run_cli("audit", str(self.src), "--json",
                            str(self.dir / "nope" / "report.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)

    def test_extract_output_existing_file_is_exit_2(self):
        target = self.dir / "afile"
        target.write_text("x")
        proc = self.run_cli("extract", str(self.src), "-o", str(target))
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)

    def test_empty_directory_warns_instead_of_clean(self):
        empty = self.dir / "empty"
        empty.mkdir()
        proc = self.run_cli("audit", str(empty))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no files found", proc.stdout)
        strict = self.run_cli("audit", str(empty), "--strict")
        self.assertEqual(strict.returncode, 1)


class SidecarPairingAtScale(unittest.TestCase):
    """Regression: pairing used to scan every media path for every
    sidecar (quadratic). 1,200 pairs must pair up correctly — and the
    suite's runtime shows the fix (this took minutes at 3,000 pairs)."""

    def test_many_photos_pair_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            photos = Path(tmp) / "Takeout" / "Google Photos"
            photos.mkdir(parents=True)
            n = 1200
            for i in range(n):
                (photos / f"IMG_{i:05d}.jpg").write_bytes(b"\xff\xd8data")
                (photos / f"IMG_{i:05d}.jpg.json").write_bytes(b"{}")
            (photos / "IMG_lonely.jpg").write_bytes(b"\xff\xd8data")
            (photos / "IMG_ghost.jpg.json").write_bytes(b"{}")
            report = ta.audit_sources([Path(tmp)])
            self.assertEqual(report.photos_media, n + 1)
            self.assertEqual(len(report.photos_missing_sidecar), 1)
            self.assertEqual(len(report.photos_orphan_sidecars), 1)


if __name__ == "__main__":
    unittest.main()
