"""QA round 3 (2026-11-12): scale & performance — tar/zip content reads.

Defect: _entry_chunks re-opened the archive (and re-read its full
member index) on every content read — ZipFile() parses the whole
central directory per read, tarfile.open() + getmembers() re-scans
(and for .tar.gz fully re-decompresses) per read — so audit --sha256,
mbox counting and extract scaled quadratically with the entry count
(e.g. audit of 1,000 same-size files in a .tar.gz: ~92 s; extract of
3,000 files from a .zip: ~39 s). The same per-read getmembers() also
meant any truncation in a tar made every content read fail, so the
audit lost all mail counts and found no duplicates in that archive
even though its readable members were fine. Fixed by (a) one open
handle per source archive (_SourceReader, reused for the whole run)
and (b) a single ordered streaming pass over each tar source
(_prefetch_tar) that hashes the members dedupe will need and counts
every .mbox.

These tests pin the mechanism deterministically (handle reuse and
read counts) rather than with wall-clock assertions, which are flaky
on shared runners.
"""

import io
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import takeout_audit as ta


def _same_size_files(n_unique, size=100):
    """n_unique distinct contents, each appearing twice (2*n files)."""
    files = []
    for i in range(n_unique):
        data = bytes([i % 251]) * size
        files.append((f"Takeout/Drive/f{i:03d}.bin", data))
        files.append((f"Takeout/Drive/g{i:03d}.bin", data))
    return files


def _build_tar_gz(path, files):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _build_zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)


class TestQaRound3TarScale(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_zip_handle_reused_across_content_reads(self):
        files = _same_size_files(5)
        src = self.tmp / "set.zip"
        _build_zip(src, files)
        real_zipfile = ta.zipfile.ZipFile
        calls = []

        class CountingZipFile(real_zipfile):
            def __init__(self, *a, **k):
                calls.append(1)
                super().__init__(*a, **k)

        with mock.patch.object(ta.zipfile, "ZipFile", CountingZipFile):
            entries, _archives, openers = ta.load_sources([src])
            # _iter_zip opens the archive once at load time.
            self.assertEqual(1, len(calls))
            reader = openers[src.name]
            for e in entries[:5]:
                ta.content_key(e, openers, "sha256")
            # The reader opened the archive exactly once more and
            # reused that handle for all five reads.
            self.assertEqual(2, len(calls))
            self.assertIsNotNone(reader._zf)

    def test_tar_handle_and_members_reused(self):
        files = _same_size_files(5)
        src = self.tmp / "set.tar.gz"
        _build_tar_gz(src, files)
        real_open = ta.tarfile.open
        calls = []

        def counting_open(*a, **k):
            calls.append(1)
            return real_open(*a, **k)

        with mock.patch.object(ta.tarfile, "open", counting_open):
            entries, _archives, openers = ta.load_sources([src])
            self.assertEqual(1, len(calls))  # _iter_tar's scan
            reader = openers[src.name]
            ta.content_key(entries[0], openers, "crc32")
            self.assertEqual(2, len(calls))  # reader's first open
            members_first = reader._members
            self.assertIsNotNone(members_first)
            for e in entries[1:5]:
                ta.content_key(e, openers, "crc32")
            # No further opens: handle and member list are reused.
            self.assertEqual(2, len(calls))
            self.assertIs(members_first, reader._members)

    def test_audit_tar_gz_prefetch_serves_all_content_reads(self):
        # 100 duplicate pairs (all the same size) + two mboxes, one of
        # which is exactly the same size as the group so its bytes feed
        # both the hash and the message counter from a single read.
        files = _same_size_files(100)
        mbox_a = b"From a@x\n" + b"b" * 40 + b"\nFrom c@x\n" + b"d" * 40 + b"\n"
        self.assertEqual(100, len(mbox_a))
        files.append(("Takeout/Mail/a.mbox", mbox_a))
        files.append(("Takeout/Mail/empty.mbox", b""))
        src = self.tmp / "set.tar.gz"
        _build_tar_gz(src, files)
        real_chunks = ta._entry_chunks
        calls = []

        def counting_chunks(entry, openers):
            calls.append(1)
            yield from real_chunks(entry, openers)

        with mock.patch.object(ta, "_entry_chunks", counting_chunks):
            for sha in (False, True):
                calls.clear()
                rep = ta.audit_sources([src], sha256=sha)
                # Every content read was served by the prefetch pass.
                self.assertEqual(0, len(calls), f"sha256={sha}")
                self.assertEqual(100, len(rep.duplicate_groups))
                self.assertEqual(100, rep.duplicate_files)
                self.assertEqual(100 * 100, rep.wasted_bytes)
                self.assertEqual(2, rep.mail_messages)
                by_path = {m["path"]: m["messages"]
                           for m in rep.mail_mbox_files}
                self.assertEqual(2, by_path["Takeout/Mail/a.mbox"])
                self.assertEqual(0, by_path["Takeout/Mail/empty.mbox"])

    def test_extract_tar_gz_reads_each_entry_once(self):
        files = _same_size_files(60)
        src = self.tmp / "set.tar.gz"
        _build_tar_gz(src, files)
        real_chunks = ta._entry_chunks
        calls = []

        def counting_chunks(entry, openers):
            calls.append(1)
            yield from real_chunks(entry, openers)

        out = self.tmp / "out"
        with mock.patch.object(ta, "_entry_chunks", counting_chunks):
            log = ta.extract_sources([src], out)
        # 60 unique files written, 60 duplicates skipped. Each written
        # entry is read once (for the write); the content hashes all
        # came from the prefetch pass, not from extra reads.
        self.assertEqual(60, len(log["written"]))
        self.assertEqual(60, len(log["skipped_duplicates"]))
        self.assertEqual(60, len(calls))
        self.assertEqual(60, len(list(out.rglob("*.bin"))))

    def test_audit_tar_gz_agrees_with_zip(self):
        files = _same_size_files(150)
        files.append(("Takeout/Mail/a.mbox",
                      b"From a\nbody\nFrom b\nbody\n"))
        tgz = self.tmp / "set.tar.gz"
        zf_path = self.tmp / "set.zip"
        _build_tar_gz(tgz, files)
        _build_zip(zf_path, files)
        rep_tgz = ta.audit_sources([tgz])
        rep_zip = ta.audit_sources([zf_path])

        def groups(rep, src_name):
            return sorted(
                (g["size"], g["copies"],
                 sorted(p.split(":", 1)[1] for p in g["paths"]))
                for g in rep.duplicate_groups
            )

        self.assertEqual(groups(rep_tgz, "set.tar.gz"),
                         groups(rep_zip, "set.zip"))
        self.assertEqual(rep_tgz.mail_messages, rep_zip.mail_messages)
        self.assertEqual(rep_tgz.total_files, rep_zip.total_files)
        self.assertEqual(rep_tgz.total_bytes, rep_zip.total_bytes)

    def test_truncated_tar_still_audits_readable_members(self):
        files = [("Takeout/Mail/a.mbox",
                  b"From a\nbody\nFrom b\nbody\n")]
        files += _same_size_files(10)
        src = self.tmp / "set.tar"
        with tarfile.open(src, "w") as tf:
            for name, data in files:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        # Cut the file in the middle of the last member's data: the
        # member still lists (its header is intact) but only part of
        # its data survives, so it no longer matches its pair.
        with tarfile.open(src) as tf:
            offsets = [m.offset for m in tf.getmembers()]
        raw = src.read_bytes()
        src.write_bytes(raw[: offsets[-1] + 512 + 50])
        rep = ta.audit_sources([src])
        # No crash; the truncation is flagged, the mbox (first
        # member) is still counted, and the surviving pairs still
        # dedupe (the short member's pair loses its group).
        self.assertFalse(rep.archives[0].ok)
        self.assertTrue(rep.problems)
        self.assertEqual(2, rep.mail_messages)
        self.assertEqual(21, rep.total_files)
        self.assertEqual(9, len(rep.duplicate_groups))

    def test_zip_extract_opens_archive_once(self):
        files = _same_size_files(30)
        src = self.tmp / "set.zip"
        _build_zip(src, files)
        real_zipfile = ta.zipfile.ZipFile
        calls = []

        class CountingZipFile(real_zipfile):
            def __init__(self, *a, **k):
                calls.append(1)
                super().__init__(*a, **k)

        out = self.tmp / "out"
        with mock.patch.object(ta.zipfile, "ZipFile", CountingZipFile):
            log = ta.extract_sources([src], out)
        # One open for _iter_zip at load time, one for the reader;
        # before the fix every entry opened the archive again.
        self.assertEqual(2, len(calls))
        self.assertEqual(30, len(log["written"]))
