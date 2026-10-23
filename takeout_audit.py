#!/usr/bin/env python3
"""Takeout Audit — an offline auditor for Google Takeout archive sets.

You downloaded your Google Takeout: maybe three archive parts, maybe thirty,
maybe for the second time this year. Before you trust that download with the
only copy of a decade of photos, mail and files — and especially before you
delete your Google account or cancel Google One — this tool answers, fully
offline and without extracting anything:

  1. Is every archive part present, and is every archive intact?
  2. What is actually in the set (per service, file counts, sizes) —
     including how many messages are in your Mail mbox archives?
  3. How much content is duplicated across parts and across successive
     exports, and how much space is that wasting?
  4. Is every Google Photos media file matched by its metadata sidecar,
     even though Google scatters media and JSON across different parts?
  5. (``extract``) Give me one clean, unified, deduplicated folder.

Nothing is uploaded anywhere. Nothing on disk is modified by ``audit``.

Duplicate detection defaults to (size, CRC-32 of content): for zip archives
that value comes straight from the central directory, so even very large
sets can be audited without decompressing every byte. Pass ``--sha256``
for a cryptographic content hash instead. For plain directories and tar
archives, content is streamed once to compute the hash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__version__ = "0.2.2"

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".bmp",
    ".tif", ".tiff", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp",
    ".raw", ".dng", ".cr2", ".nef", ".arw",
}

# Google truncates Photos sidecar file names: the sidecar for `name` is
# `name + ".json"`, shortened so the base name is at most 51 characters.
SIDECAR_SUFFIX = ".json"
SIDECAR_MAX_BASENAME = 51

PART_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<num>\d{3,})\.(?:zip|tgz|tar\.gz|tar)$",
    re.IGNORECASE,
)

CHUNK = 1024 * 1024


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Entry:
    """One file inside the audited set."""

    source: str          # archive file name or directory root name
    path: str            # POSIX-style path inside the set
    size: int
    kind: str            # "zip" | "tar" | "dir"
    crc: int | None = None        # CRC-32 from zip metadata, if any
    digest: str | None = None     # content hash, computed on demand
    # Position of this entry within its archive's member list. Needed
    # because an archive may contain several members with the SAME path
    # (legal in both zip and tar): opening a member by name resolves to
    # just one of them, so content reads must address the exact member.
    index: int | None = None

    @property
    def service(self) -> str:
        parts = PurePosixPath(self.path).parts
        if "Takeout" in parts:
            rest = parts[parts.index("Takeout") + 1:]
            if len(rest) >= 2:
                return rest[0]
            return "(root)"
        if len(parts) > 1:
            return parts[0]
        return "(root)"

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name


@dataclass
class ArchiveReport:
    source: str
    kind: str                    # "zip" | "tar" | "dir"
    ok: bool = True
    error: str | None = None     # unreadable archive / corrupt entry info
    entries: int = 0


@dataclass
class Report:
    sources: list[str] = field(default_factory=list)
    archives: list[ArchiveReport] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0
    services: dict = field(default_factory=dict)       # name -> {files, bytes}
    extensions: dict = field(default_factory=dict)     # ext -> count
    missing_parts: dict = field(default_factory=dict)  # prefix -> [missing nums]
    duplicate_groups: list = field(default_factory=list)  # {size, copies, paths}
    duplicate_files: int = 0
    wasted_bytes: int = 0
    photos_media: int = 0
    photos_missing_sidecar: list = field(default_factory=list)
    photos_orphan_sidecars: list = field(default_factory=list)
    mail_mbox_files: list = field(default_factory=list)  # {source, path, messages}
    mail_messages: int = 0
    hash_mode: str = "crc32"
    empty: bool = False           # no files found in any source

    @property
    def problems(self) -> list[str]:
        out = []
        for arc in self.archives:
            if not arc.ok:
                out.append(f"archive problem: {arc.source}: {arc.error}")
        for prefix, missing in self.missing_parts.items():
            nums = ", ".join(f"{n:03d}" for n in missing)
            out.append(f"missing archive part(s) for {prefix}: {nums}")
        return out

    @property
    def warnings(self) -> list[str]:
        out = []
        if self.empty:
            out.append(
                "no files found in any source — check the paths you passed"
            )
        if self.duplicate_files:
            out.append(
                f"{self.duplicate_files} duplicated file(s), "
                f"{_fmt_bytes(self.wasted_bytes)} wasted"
            )
        if self.photos_missing_sidecar:
            out.append(
                f"{len(self.photos_missing_sidecar)} Photos media file(s) "
                "without a metadata sidecar"
            )
        if self.photos_orphan_sidecars:
            out.append(
                f"{len(self.photos_orphan_sidecars)} orphan Photos sidecar(s) "
                "with no matching media file"
            )
        return out

    def to_dict(self) -> dict:
        return {
            "version": __version__,
            "hash_mode": self.hash_mode,
            "sources": self.sources,
            "archives": [
                {
                    "source": a.source,
                    "kind": a.kind,
                    "ok": a.ok,
                    "error": a.error,
                    "entries": a.entries,
                }
                for a in self.archives
            ],
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "services": self.services,
            "extensions": self.extensions,
            "missing_parts": {
                k: v for k, v in self.missing_parts.items()
            },
            "duplicates": {
                "groups": self.duplicate_groups,
                "duplicate_files": self.duplicate_files,
                "wasted_bytes": self.wasted_bytes,
            },
            "photos": {
                "media_files": self.photos_media,
                "missing_sidecar": self.photos_missing_sidecar,
                "orphan_sidecars": self.photos_orphan_sidecars,
            },
            "mail": {
                "mbox_files": self.mail_mbox_files,
                "total_messages": self.mail_messages,
            },
            "problems": self.problems,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------
# Source loading
# --------------------------------------------------------------------------

def _classify(path: Path) -> str | None:
    if path.is_dir():
        return "dir"
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tgz", ".tar.gz", ".tar")):
        return "tar"
    return None


def _iter_zip(path: Path, label: str, arc: ArchiveReport) -> list[Entry]:
    entries: list[Entry] = []
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        arc.ok = False
        arc.error = f"not a readable zip archive ({exc})"
        return entries
    with zf:
        bad = zf.testzip()
        if bad is not None:
            arc.ok = False
            arc.error = f"corrupt entry (CRC check failed): {bad}"
        for idx, info in enumerate(zf.infolist()):
            if info.is_dir():
                continue
            entries.append(
                Entry(
                    source=label,
                    path=info.filename.replace(os.sep, "/"),
                    size=info.file_size,
                    kind="zip",
                    crc=info.CRC,
                    index=idx,
                )
            )
    arc.entries = len(entries)
    return entries


def _iter_tar(path: Path, label: str, arc: ArchiveReport) -> list[Entry]:
    entries: list[Entry] = []
    try:
        tf = tarfile.open(path, "r:*")
    except (tarfile.TarError, EOFError, OSError) as exc:
        arc.ok = False
        arc.error = f"not a readable tar archive ({exc})"
        return entries
    with tf:
        try:
            for idx, member in enumerate(tf):
                if not member.isfile():
                    continue
                entries.append(
                    Entry(
                        source=label,
                        path=member.name.replace(os.sep, "/"),
                        size=member.size,
                        kind="tar",
                        index=idx,
                    )
                )
        except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
            # A truncated download fails mid-iteration, not at open time.
            # Keep the members listed so far and report the problem
            # instead of crashing.
            arc.ok = False
            arc.error = f"corrupt or truncated tar archive ({exc})"
    arc.entries = len(entries)
    return entries


def _iter_dir(path: Path, label: str, arc: ArchiveReport) -> list[Entry]:
    entries: list[Entry] = []
    for root, _dirs, files in os.walk(path):
        for fname in files:
            full = Path(root) / fname
            rel = full.relative_to(path).as_posix()
            try:
                size = full.stat().st_size
            except OSError:
                continue
            entries.append(
                Entry(source=label, path=rel, size=size, kind="dir")
            )
    arc.entries = len(entries)
    return entries


def _source_labels(paths: list[Path]) -> list[str]:
    """A unique display/lookup label per source. The plain file or folder
    name is used unless two sources share it — extracted exports are very
    often both called ``Takeout`` — in which case the full path is used
    so the sources stay distinguishable and, critically, so content
    lookups never open one source's files through another's."""
    counts: dict[str, int] = {}
    for p in paths:
        counts[p.name] = counts.get(p.name, 0) + 1
    return [str(p) if counts[p.name] > 1 else p.name for p in paths]


def load_sources(paths: list[Path]):
    """Return (entries, archive_reports, entry_opener)."""
    entries: list[Entry] = []
    archives: list[ArchiveReport] = []
    openers: dict[str, object] = {}

    for path, label in zip(paths, _source_labels(paths)):
        kind = _classify(path)
        if kind is None:
            raise ValueError(
                f"unsupported source (expected .zip, .tgz/.tar.gz or a "
                f"directory): {path}"
            )
        arc = ArchiveReport(source=label, kind=kind)
        if kind == "zip":
            found = _iter_zip(path, label, arc)
        elif kind == "tar":
            found = _iter_tar(path, label, arc)
        else:
            found = _iter_dir(path, label, arc)
        openers[label] = (kind, path)
        entries.extend(found)
        archives.append(arc)
    return entries, archives, openers


# Exceptions a single corrupt entry can raise while its content is being
# streamed (bad CRC, truncated stream, missing member, ...). One bad
# entry must never abort the whole audit — it is exactly the "corrupt or
# truncated download" case this tool exists to report.
CONTENT_READ_ERRORS = (
    OSError, EOFError, RuntimeError, zlib.error,
    zipfile.BadZipFile, tarfile.TarError,
)


def _mark_unreadable(report: Report, entry: Entry, exc: Exception) -> None:
    """Record a content-read failure as a problem on the entry's source
    (unless that source already carries a problem)."""
    for arc in report.archives:
        if arc.source == entry.source:
            if arc.ok:
                arc.ok = False
                arc.error = f"unreadable entry: {entry.path} ({exc})"
            return


# --------------------------------------------------------------------------
# Content hashing (only same-size groups ever get hashed)
# --------------------------------------------------------------------------

def _stream_hash(read_chunks, mode: str) -> str:
    if mode == "sha256":
        h = hashlib.sha256()
        for chunk in read_chunks:
            h.update(chunk)
        return "sha256:" + h.hexdigest()
    crc = 0
    for chunk in read_chunks:
        crc = zlib.crc32(chunk, crc)
    return "crc32:%08x" % (crc & 0xFFFFFFFF)


def _entry_chunks(entry: Entry, openers):
    kind, base = openers[entry.source]
    if kind == "zip":
        with zipfile.ZipFile(base) as zf:
            # Address the exact member by position: a zip may legally
            # hold several entries with the same name, and opening by
            # name silently returns the last one for all of them.
            target = (
                zf.infolist()[entry.index]
                if entry.index is not None else entry.path
            )
            with zf.open(target) as fh:
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    yield chunk
    elif kind == "tar":
        with tarfile.open(base, "r:*") as tf:
            # Same duplicate-name hazard as zip: getmember() returns
            # one match, so address the member by position instead.
            member = (
                tf.getmembers()[entry.index]
                if entry.index is not None else tf.getmember(entry.path)
            )
            fh = tf.extractfile(member)
            if fh is None:
                raise tarfile.TarError(
                    f"entry has no readable content: {entry.path}"
                )
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                yield chunk
    else:
        with open(Path(base) / entry.path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                yield chunk


def content_key(entry: Entry, openers, mode: str) -> str:
    """A content identity for dedupe: (size, content hash)."""
    if entry.digest is None:
        if mode == "crc32" and entry.kind == "zip" and entry.crc is not None:
            entry.digest = "crc32:%08x" % (entry.crc & 0xFFFFFFFF)
        else:
            entry.digest = _stream_hash(_entry_chunks(entry, openers), mode)
    return f"{entry.size}:{entry.digest}"


def count_mbox_messages(entry: Entry, openers) -> int:
    """Count messages in an mbox entry. In mbox format every message is
    introduced by a separator line beginning with ``From ``; body lines
    that would look like separators are escaped as ``>From `` by the
    writer, so a plain prefix count is the standard heuristic.

    The scan is a small state machine over the raw chunks — O(n) time
    and O(chunk) memory regardless of line lengths. Splitting the
    stream into lines first (the previous implementation) copies the
    remaining buffer for every line, which is quadratic: a plain
    30 MB mbox took over a minute, and a file with very long lines
    degraded just as badly.
    """
    separator = b"From "
    count = 0
    pending = b""      # partial separator candidate at a line start
    at_line_start = True
    for chunk in _entry_chunks(entry, openers):
        data = pending + chunk
        pending = b""
        pos = 0
        end = len(data)
        while pos < end:
            if at_line_start:
                if end - pos < len(separator):
                    # Too few bytes left to decide: carry them over.
                    pending = data[pos:]
                    pos = end
                    break
                if data.startswith(separator, pos):
                    count += 1
                at_line_start = False
            newline = data.find(b"\n", pos)
            if newline < 0:
                pos = end
            else:
                pos = newline + 1
                at_line_start = True
    return count


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

def _check_parts(paths: list[Path]) -> dict:
    groups: dict[str, set[int]] = {}
    for path in paths:
        if path.is_dir():
            continue
        m = PART_RE.match(path.name)
        if m:
            groups.setdefault(m.group("prefix"), set()).add(int(m.group("num")))
    missing: dict[str, list[int]] = {}
    for prefix, nums in groups.items():
        if len(nums) < 2 and max(nums) == 1:
            continue
        expected = set(range(1, max(nums) + 1))
        gone = sorted(expected - nums)
        if gone:
            missing[prefix] = gone
    return missing


def _nfc(text: str) -> str:
    """NFC-normalize a path for COMPARISON purposes. The same file name
    can be stored NFC in one part and NFD in another (typically when a
    set was zipped on macOS), and byte-wise comparison would then
    report a sidecar as both missing and orphaned."""
    return unicodedata.normalize("NFC", text)


def _sidecar_candidates(media_name: str) -> list[str]:
    full = media_name + SIDECAR_SUFFIX
    candidates = [full]
    if len(full) > SIDECAR_MAX_BASENAME:
        candidates.append(full[:SIDECAR_MAX_BASENAME])
    return candidates


def audit_sources(paths: list[Path], sha256: bool = False) -> Report:
    entries, archives, openers = load_sources(paths)
    report = Report(
        sources=[str(p) for p in paths],
        archives=archives,
        hash_mode="sha256" if sha256 else "crc32",
    )
    report.missing_parts = _check_parts(paths)

    for e in entries:
        report.total_files += 1
        report.total_bytes += e.size
        svc = report.services.setdefault(e.service, {"files": 0, "bytes": 0})
        svc["files"] += 1
        svc["bytes"] += e.size
        ext = PurePosixPath(e.path).suffix.lower() or "(none)"
        report.extensions[ext] = report.extensions.get(ext, 0) + 1

    report.empty = report.total_files == 0

    # Mail deep check: count messages in every .mbox in the set. The
    # content is streamed line by line; nothing is stored. An unreadable
    # (corrupt) mbox is reported as an archive problem, never a crash.
    for e in entries:
        if e.path.lower().endswith(".mbox"):
            try:
                count = count_mbox_messages(e, openers)
            except CONTENT_READ_ERRORS as exc:
                _mark_unreadable(report, e, exc)
                continue
            report.mail_mbox_files.append(
                {"source": e.source, "path": e.path, "messages": count}
            )
            report.mail_messages += count
    report.mail_mbox_files.sort(key=lambda m: -m["messages"])

    # Duplicates: group by size first; hash only collisions.
    by_size: dict[int, list[Entry]] = {}
    for e in entries:
        if e.size > 0:
            by_size.setdefault(e.size, []).append(e)
    mode = report.hash_mode
    for size, group in sorted(by_size.items()):
        if len(group) < 2:
            continue
        by_key: dict[str, list[Entry]] = {}
        for e in group:
            try:
                key = content_key(e, openers, mode)
            except CONTENT_READ_ERRORS as exc:
                _mark_unreadable(report, e, exc)
                continue
            by_key.setdefault(key, []).append(e)
        for _key, copies in by_key.items():
            if len(copies) < 2:
                continue
            report.duplicate_groups.append(
                {
                    "size": size,
                    "copies": len(copies),
                    "paths": [f"{c.source}:{c.path}" for c in copies],
                }
            )
            report.duplicate_files += len(copies) - 1
            report.wasted_bytes += size * (len(copies) - 1)
    report.duplicate_groups.sort(key=lambda g: -g["size"] * (g["copies"] - 1))

    # Photos sidecar pairing (across the whole set, same folder).
    # All comparisons run on NFC-normalized paths (reported paths stay
    # as stored): the same name may be NFC in one part and NFD in
    # another, which must not read as "sidecar missing AND orphaned".
    photos = [e for e in entries if e.service == "Google Photos"]
    sidecar_paths = {
        _nfc(e.path) for e in photos if e.name.lower().endswith(SIDECAR_SUFFIX)
    }
    media_paths = set()
    for e in photos:
        ext = PurePosixPath(e.path).suffix.lower()
        if ext in MEDIA_EXTENSIONS:
            report.photos_media += 1
            media_paths.add(e.path)
            folder = _nfc(str(PurePosixPath(e.path).parent))
            found = False
            for cand in _sidecar_candidates(e.name):
                cand = _nfc(cand)
                want = f"{folder}/{cand}" if folder != "." else cand
                if want in sidecar_paths:
                    found = True
                    break
            if not found:
                report.photos_missing_sidecar.append(f"{e.source}:{e.path}")
    # Index media names per folder once. Scanning every media path for
    # every sidecar is O(media x sidecars) path parsing — a 3,000-photo
    # set took minutes; real Takeout sets are far larger.
    media_by_folder: dict[str, set[str]] = {}
    trunc_prefix_by_folder: dict[str, set[str]] = {}
    trunc_stem_len = SIDECAR_MAX_BASENAME - len(SIDECAR_SUFFIX)
    for mp in media_paths:
        ppath = PurePosixPath(mp)
        mfolder = _nfc(str(ppath.parent))
        media_by_folder.setdefault(mfolder, set()).add(_nfc(ppath.name))
        trunc_prefix_by_folder.setdefault(mfolder, set()).add(
            _nfc(ppath.name + SIDECAR_SUFFIX)[:trunc_stem_len]
        )
    for e in photos:
        if not e.name.lower().endswith(SIDECAR_SUFFIX):
            continue
        folder = _nfc(str(PurePosixPath(e.path).parent))
        stem = _nfc(e.name[: -len(SIDECAR_SUFFIX)])
        paired = stem in media_by_folder.get(folder, set()) or (
            len(stem) == trunc_stem_len
            and stem in trunc_prefix_by_folder.get(folder, set())
        )
        if not paired:
            report.photos_orphan_sidecars.append(f"{e.source}:{e.path}")

    return report


# --------------------------------------------------------------------------
# Extract (unified, deduplicated)
# --------------------------------------------------------------------------

def extract_sources(paths: list[Path], output: Path, sha256: bool = False) -> dict:
    if output.exists() and not output.is_dir():
        raise ValueError(f"output path exists and is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    entries, archives, openers = load_sources(paths)
    mode = "sha256" if sha256 else "crc32"
    seen: dict[str, str] = {}
    written_dests: dict[str, str] = {}   # output-relative path -> entry path
    written, skipped = [], []
    read_problems: list[dict] = []
    for e in entries:
        rel = PurePosixPath(e.path)
        if rel.is_absolute() or ".." in rel.parts:
            skipped.append({"path": e.path, "reason": "unsafe path, skipped"})
            continue
        dest = None
        try:
            key = content_key(e, openers, mode)
            if key in seen:
                skipped.append({"path": e.path, "duplicate_of": seen[key]})
                continue
            parts = rel.parts
            if "Takeout" in parts:
                rel = PurePosixPath(*parts[parts.index("Takeout") + 1:])
                if not rel.parts:
                    skipped.append(
                        {"path": e.path,
                         "reason": "path maps to the library root, skipped"}
                    )
                    continue
            if rel.as_posix() in written_dests:
                # A different file already claimed this output path
                # (archives may repeat a name, and exports get merged).
                # Overwriting it would silently destroy the first copy
                # while the log claimed both were written.
                skipped.append(
                    {"path": e.path,
                     "reason": "output path already written from "
                               f"{written_dests[rel.as_posix()]}, skipped"}
                )
                continue
            dest = output.joinpath(*rel.parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as out:
                for chunk in _entry_chunks(e, openers):
                    out.write(chunk)
        except CONTENT_READ_ERRORS as exc:
            # Corrupt entry: never leave a partial file behind, and make
            # the skip visible in the log instead of crashing mid-extract.
            if dest is not None:
                dest.unlink(missing_ok=True)
            skipped.append(
                {"path": e.path, "reason": f"unreadable entry, skipped ({exc})"}
            )
            read_problems.append(
                {"source": e.source, "error": f"unreadable entry: {e.path} ({exc})"}
            )
            continue
        seen[key] = e.path
        written_dests[rel.as_posix()] = e.path
        written.append(e.path)
    log = {
        "version": __version__,
        "hash_mode": mode,
        "written": written,
        "skipped_duplicates": skipped,
        "archive_problems": [
            {"source": a.source, "error": a.error} for a in archives if not a.ok
        ] + read_problems,
    }
    (output / "_takeout-audit-log.json").write_text(
        json.dumps(log, indent=2), encoding="utf-8"
    )
    return log


# --------------------------------------------------------------------------
# Rendering / CLI
# --------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TB"


def render_text(report: Report, max_samples: int = 10) -> str:
    lines = []
    lines.append("Takeout Audit — offline report")
    lines.append("=" * 60)
    lines.append(f"Sources audited : {len(report.sources)}")
    for arc in report.archives:
        state = "OK" if arc.ok else f"PROBLEM — {arc.error}"
        lines.append(f"  - {arc.source} [{arc.kind}] {arc.entries} files — {state}")
    lines.append("")
    lines.append(
        f"Total: {report.total_files} files, {_fmt_bytes(report.total_bytes)}"
    )
    lines.append("")
    lines.append("By service:")
    for name, svc in sorted(
        report.services.items(), key=lambda kv: -kv[1]["bytes"]
    ):
        lines.append(
            f"  {name:<28} {svc['files']:>8} files  {_fmt_bytes(svc['bytes']):>10}"
        )
    lines.append("")
    if report.duplicate_groups:
        lines.append(
            f"Duplicates: {len(report.duplicate_groups)} group(s), "
            f"{report.duplicate_files} redundant file(s), "
            f"{_fmt_bytes(report.wasted_bytes)} wasted"
        )
        for g in report.duplicate_groups[:max_samples]:
            lines.append(f"  {_fmt_bytes(g['size'])} x{g['copies']}:")
            for p in g["paths"]:
                lines.append(f"    - {p}")
    else:
        lines.append("Duplicates: none found")
    lines.append("")
    lines.append(
        f"Google Photos: {report.photos_media} media file(s), "
        f"{len(report.photos_missing_sidecar)} missing sidecar, "
        f"{len(report.photos_orphan_sidecars)} orphan sidecar(s)"
    )
    for p in report.photos_missing_sidecar[:max_samples]:
        lines.append(f"  missing sidecar: {p}")
    for p in report.photos_orphan_sidecars[:max_samples]:
        lines.append(f"  orphan sidecar : {p}")
    lines.append("")
    if report.mail_mbox_files:
        lines.append(
            f"Mail: {report.mail_messages} message(s) in "
            f"{len(report.mail_mbox_files)} mbox file(s)"
        )
        for m in report.mail_mbox_files[:max_samples]:
            lines.append(
                f"  mbox: {m['source']}:{m['path']} — "
                f"{m['messages']} message(s)"
            )
        lines.append("")
    for prob in report.problems:
        lines.append(f"PROBLEM: {prob}")
    for warn in report.warnings:
        lines.append(f"Warning: {warn}")
    if not report.problems and not report.warnings:
        lines.append("Result: clean — no problems, no warnings.")
    elif not report.problems:
        lines.append("Result: no problems (warnings above).")
    else:
        lines.append("Result: PROBLEMS found (see above).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="takeout-audit",
        description="Offline auditor for Google Takeout archive sets.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="Audit archive sets (read-only).")
    p_audit.add_argument("paths", nargs="+", type=Path,
                         help=".zip/.tgz files or extracted directories")
    p_audit.add_argument("--json", type=Path, dest="json_path",
                         help="Also write the full report as JSON here.")
    p_audit.add_argument("--sha256", action="store_true",
                         help="Use SHA-256 content hashing for dedupe "
                              "(slower; default is CRC-32).")
    p_audit.add_argument("--strict", action="store_true",
                         help="Exit 1 on warnings too (duplicates, "
                              "sidecar issues).")
    p_audit.add_argument("--max-samples", type=int, default=10,
                         help="Max examples per section in text output.")

    p_ext = sub.add_parser(
        "extract", help="Extract one unified, deduplicated library.")
    p_ext.add_argument("paths", nargs="+", type=Path)
    p_ext.add_argument("-o", "--output", type=Path, required=True,
                       help="Target directory (must be empty or new).")
    p_ext.add_argument("--sha256", action="store_true")

    args = parser.parse_args(argv)
    paths = [Path(p) for p in args.paths]
    for p in paths:
        if not p.exists():
            print(f"error: no such file or directory: {p}", file=sys.stderr)
            return 2

    try:
        if args.command == "audit":
            report = audit_sources(paths, sha256=args.sha256)
            print(render_text(report, max_samples=args.max_samples))
            if args.json_path:
                args.json_path.write_text(
                    json.dumps(report.to_dict(), indent=2), encoding="utf-8"
                )
            if report.problems:
                return 1
            if args.strict and report.warnings:
                return 1
            return 0
        log = extract_sources(paths, args.output, sha256=args.sha256)
        print(
            f"Wrote {len(log['written'])} file(s), skipped "
            f"{len(log['skipped_duplicates'])} duplicate(s) -> {args.output}"
        )
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # Documented contract: I/O errors are exit 2, not a traceback
        # (e.g. --json pointing into a directory that does not exist).
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
