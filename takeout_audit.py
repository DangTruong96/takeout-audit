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
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__version__ = "0.2.0"

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


def _iter_zip(path: Path, arc: ArchiveReport) -> list[Entry]:
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
        for info in zf.infolist():
            if info.is_dir():
                continue
            entries.append(
                Entry(
                    source=path.name,
                    path=info.filename.replace(os.sep, "/"),
                    size=info.file_size,
                    kind="zip",
                    crc=info.CRC,
                )
            )
    arc.entries = len(entries)
    return entries


def _iter_tar(path: Path, arc: ArchiveReport) -> list[Entry]:
    entries: list[Entry] = []
    try:
        tf = tarfile.open(path, "r:*")
    except tarfile.TarError as exc:
        arc.ok = False
        arc.error = f"not a readable tar archive ({exc})"
        return entries
    with tf:
        for member in tf:
            if not member.isfile():
                continue
            entries.append(
                Entry(
                    source=path.name,
                    path=member.name.replace(os.sep, "/"),
                    size=member.size,
                    kind="tar",
                )
            )
    arc.entries = len(entries)
    return entries


def _iter_dir(path: Path, arc: ArchiveReport) -> list[Entry]:
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
                Entry(source=path.name, path=rel, size=size, kind="dir")
            )
    arc.entries = len(entries)
    return entries


def load_sources(paths: list[Path]):
    """Return (entries, archive_reports, entry_opener)."""
    entries: list[Entry] = []
    archives: list[ArchiveReport] = []
    openers: dict[str, object] = {}

    for path in paths:
        kind = _classify(path)
        if kind is None:
            raise ValueError(
                f"unsupported source (expected .zip, .tgz/.tar.gz or a "
                f"directory): {path}"
            )
        arc = ArchiveReport(source=path.name, kind=kind)
        if kind == "zip":
            found = _iter_zip(path, arc)
            openers[path.name] = ("zip", path)
        elif kind == "tar":
            found = _iter_tar(path, arc)
            openers[path.name] = ("tar", path)
        else:
            found = _iter_dir(path, arc)
            openers[path.name] = ("dir", path)
        entries.extend(found)
        archives.append(arc)
    return entries, archives, openers


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
            with zf.open(entry.path) as fh:
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    yield chunk
    elif kind == "tar":
        with tarfile.open(base, "r:*") as tf:
            member = tf.getmember(entry.path)
            fh = tf.extractfile(member)
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


def _iter_lines(entry: Entry, openers):
    """Yield the entry's content as lines (bytes, newline stripped),
    streaming chunk by chunk so huge files are never held in memory."""
    buf = b""
    for chunk in _entry_chunks(entry, openers):
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.rstrip(b"\r")
    if buf:
        yield buf.rstrip(b"\r")


def count_mbox_messages(entry: Entry, openers) -> int:
    """Count messages in an mbox entry. In mbox format every message is
    introduced by a separator line beginning with ``From ``; body lines
    that would look like separators are escaped as ``>From `` by the
    writer, so a plain prefix count is the standard heuristic."""
    return sum(
        1 for line in _iter_lines(entry, openers)
        if line.startswith(b"From ")
    )


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

    # Mail deep check: count messages in every .mbox in the set. The
    # content is streamed line by line; nothing is stored.
    for e in entries:
        if e.path.lower().endswith(".mbox"):
            count = count_mbox_messages(e, openers)
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
            by_key.setdefault(content_key(e, openers, mode), []).append(e)
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
    photos = [e for e in entries if e.service == "Google Photos"]
    sidecar_paths = {
        e.path for e in photos if e.name.lower().endswith(SIDECAR_SUFFIX)
    }
    media_paths = set()
    for e in photos:
        ext = PurePosixPath(e.path).suffix.lower()
        if ext in MEDIA_EXTENSIONS:
            report.photos_media += 1
            media_paths.add(e.path)
            folder = str(PurePosixPath(e.path).parent)
            found = False
            for cand in _sidecar_candidates(e.name):
                want = f"{folder}/{cand}" if folder != "." else cand
                if want in sidecar_paths:
                    found = True
                    break
            if not found:
                report.photos_missing_sidecar.append(f"{e.source}:{e.path}")
    for e in photos:
        if not e.name.lower().endswith(SIDECAR_SUFFIX):
            continue
        folder = str(PurePosixPath(e.path).parent)
        stem = e.name[: -len(SIDECAR_SUFFIX)]
        paired = False
        for mp in media_paths:
            if str(PurePosixPath(mp).parent) != folder:
                continue
            mname = PurePosixPath(mp).name
            if stem == mname or (
                len(stem) == SIDECAR_MAX_BASENAME - len(SIDECAR_SUFFIX)
                and (mname + SIDECAR_SUFFIX).startswith(stem)
            ):
                paired = True
                break
        if not paired:
            report.photos_orphan_sidecars.append(f"{e.source}:{e.path}")

    return report


# --------------------------------------------------------------------------
# Extract (unified, deduplicated)
# --------------------------------------------------------------------------

def extract_sources(paths: list[Path], output: Path, sha256: bool = False) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    entries, archives, openers = load_sources(paths)
    mode = "sha256" if sha256 else "crc32"
    seen: dict[str, str] = {}
    written, skipped = [], []
    for e in entries:
        rel = PurePosixPath(e.path)
        if rel.is_absolute() or ".." in rel.parts:
            skipped.append({"path": e.path, "reason": "unsafe path, skipped"})
            continue
        key = content_key(e, openers, mode)
        if key in seen:
            skipped.append({"path": e.path, "duplicate_of": seen[key]})
            continue
        parts = rel.parts
        if "Takeout" in parts:
            rel = PurePosixPath(*parts[parts.index("Takeout") + 1:])
            if not rel.parts:
                continue
        dest = output.joinpath(*rel.parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            for chunk in _entry_chunks(e, openers):
                out.write(chunk)
        seen[key] = e.path
        written.append(e.path)
    log = {
        "version": __version__,
        "hash_mode": mode,
        "written": written,
        "skipped_duplicates": skipped,
        "archive_problems": [
            {"source": a.source, "error": a.error} for a in archives if not a.ok
        ],
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


if __name__ == "__main__":
    raise SystemExit(main())
