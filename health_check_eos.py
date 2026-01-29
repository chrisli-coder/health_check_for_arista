#!/usr/bin/env python3
"""
Arista EOS support-bundle / show-tech health check tool.

Author : chris.li@arista.com
Company: Arista Networks
Date   : 2026-01-29

This script analyses EOS show-tech / show-tech-support-all outputs
and related support-bundle archives/directories and generates a
health report in brief or verbose form.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
from pathlib import Path
import re
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__author__ = "chris.li@arista.com"
__company__ = "Arista Networks"
__last_modified__ = "2026-01-29"
__version__ = "1.0.0"


LOG = logging.getLogger("health_check_eos")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="health_check_eos",
        description=(
            "Arista EOS show-tech / support-bundle health check tool.\n"
            "Supports direct show-tech files, unpacked support-bundle "
            "directories and archive files containing one or more bundles."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    # Put author/company/version at the very end of help output via epilog.
    parser.epilog = (
        "Author  : %(author)s\n"
        "Company : %(company)s\n"
        "Version : %(version)s (Last modified: %(last)s)"
    )

    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help=(
            "One or more inputs: show-tech/show-tech-support-all file, "
            "unpacked support-bundle directory, or support-bundle archive "
            "(tar/tar.gz/tgz/zip). Type is detected automatically."
        ),
    )

    output_group = parser.add_argument_group("output")
    output_group.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="Write report to FILE instead of stdout.",
    )
    output_group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose report mode (includes all check details).",
    )
    output_group.add_argument(
        "-b",
        "--brief",
        action="store_true",
        help="Brief report mode (default).",
    )
    output_group.add_argument(
        "--json",
        action="store_true",
        help="Output report in JSON format.",
    )

    debug_group = parser.add_argument_group("debug / misc")
    debug_group.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )

    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = build_arg_parser()
    meta = {
        "author": __author__,
        "company": __company__,
        "version": __version__,
        "last": __last_modified__,
    }
    if parser.description:
        parser.description = parser.description % meta  # type: ignore[operator]
    if parser.epilog:
        parser.epilog = parser.epilog % meta  # type: ignore[operator]
    args = parser.parse_args(argv)

    # Resolve brief/verbose: verbose overrides brief, default is brief.
    if args.verbose:
        args.mode = "verbose"
    else:
        args.mode = "brief"

    return args


def configure_logging(debug: bool) -> None:
    # Only show detailed processing logs in debug mode; otherwise keep output clean.
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# Data structures and parsing
# ---------------------------------------------------------------------------


@dataclass
class CommandBlock:
    command: str
    lines: List[str]


class Severity(str, Enum):
    OK = "OK"
    WARN = "WARN"
    ERROR = "ERROR"
    INFO = "INFO"


@dataclass
class CheckResult:
    name: str
    category: str
    severity: Severity
    summary: str
    details: List[str] = field(default_factory=list)


@dataclass
class DeviceBrief:
    script_time: str
    hostname: Optional[str]
    eos_version: Optional[str]
    hw_model: Optional[str]
    system_time: Optional[str]
    health: Severity
    warn_count: int
    error_count: int


class TechSupportParser:
    """Parse a show-tech / show-tech-support-all text into command blocks."""

    # Match lines like: ------------- show version ------------- or similar
    CMD_HEADER_RE = re.compile(r"^[-\s]+show.*[-\s]+$", re.IGNORECASE)

    @classmethod
    def parse(cls, text: str) -> List[CommandBlock]:
        lines = text.splitlines()
        blocks: List[CommandBlock] = []
        current_cmd: Optional[str] = None
        current_lines: List[str] = []

        def flush_block() -> None:
            nonlocal current_cmd, current_lines
            if current_cmd is not None:
                blocks.append(CommandBlock(command=current_cmd, lines=current_lines))
            current_cmd = None
            current_lines = []

        for raw in lines:
            if cls.CMD_HEADER_RE.match(raw):
                # New command block header
                flush_block()
                # Normalise header to extract command name approx after the leading dashes.
                # Example: "------------- show version -------------"
                cmd = raw.strip("- ").strip()
                # Some headers might contain extra decorations; just keep from 'show'.
                m = re.search(r"(show.*)$", cmd, re.IGNORECASE)
                if m:
                    cmd = m.group(1).strip()
                current_cmd = cmd.lower()
            else:
                if current_cmd is not None:
                    current_lines.append(raw.rstrip("\n"))

        flush_block()
        return blocks


class TechSupportContext:
    """Holds parsed command outputs and basic device information."""

    def __init__(self, source_id: str, blocks: Sequence[CommandBlock]) -> None:
        self.source_id = source_id
        self._blocks_by_cmd: Dict[str, List[CommandBlock]] = {}
        for blk in blocks:
            self._blocks_by_cmd.setdefault(blk.command, []).append(blk)

        # Basic info populated by dedicated parser based on show version/clock etc.
        self.hostname: Optional[str] = None
        self.eos_version: Optional[str] = None
        self.hw_model: Optional[str] = None
        self.arch: Optional[str] = None
        self.uptime: Optional[str] = None
        self.total_mem: Optional[int] = None
        self.free_mem: Optional[int] = None
        self.system_time: Optional[str] = None
        self.platform_series: str = "other"  # 78xx / 75xx / 7368 / 7289 / other

    # Access helpers -----------------------------------------------------

    def get_blocks(self, command_prefix: str) -> List[CommandBlock]:
        """Return blocks whose command starts with given prefix (case-insensitive)."""
        prefix = command_prefix.lower()
        matched: List[CommandBlock] = []
        for cmd, blks in self._blocks_by_cmd.items():
            if cmd.startswith(prefix):
                matched.extend(blks)
        return matched

    def iter_all_blocks(self) -> Iterable[CommandBlock]:
        for blks in self._blocks_by_cmd.values():
            for blk in blks:
                yield blk


# ---------------------------------------------------------------------------
# Input discovery (show-tech files from various sources)
# ---------------------------------------------------------------------------


def discover_showtech_files_from_directory(root: Path) -> List[Path]:
    """Recursively find show-tech/show-tech-support-all files under a directory."""
    # Only match exact filenames (case-insensitive) for automatic discovery.
    valid_names = ("show-tech", "show-tech-support-all")
    results: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name in valid_names:
            results.append(path)
    return results


@dataclass
class ArchiveShowTechMember:
    """Represents a show-tech file inside an archive, possibly nested one level."""

    display_name: str  # for logging / source_id, e.g. "outer.zip!inner.tar!show-tech"
    outer_member: Optional[str]  # None if top-level show-tech in archive
    inner_member: str  # member path inside (nested) archive that holds the text


def discover_showtech_members_from_archive(archive_path: Path) -> List[ArchiveShowTechMember]:
    """
    Find show-tech files in an archive, supporting one level of nested archives
    (e.g. outer.zip containing support-bundle.zip which contains show-tech).
    """
    members: List[ArchiveShowTechMember] = []

    def is_showtech(name_: str) -> bool:
        base = os.path.basename(name_).lower()
        # Only accept exact show-tech or show-tech-support-all when auto-discovered.
        return base in ("show-tech", "show-tech-support-all")
    def is_nested_archive(name_: str) -> bool:
        lower = name_.lower()
        return lower.endswith((".zip", ".tar", ".tar.gz", ".tgz"))

    # Helper to scan an in-memory nested archive (bytes) for show-tech files.
    def scan_nested(outer_name: str, data: bytes) -> None:
        from io import BytesIO

        bio = BytesIO(data)
        if zipfile.is_zipfile(bio):
            bio.seek(0)
            with zipfile.ZipFile(bio, "r") as nz:
                for ninfo in nz.infolist():
                    if ninfo.is_dir():
                        continue
                    if is_showtech(ninfo.filename):
                        disp = f"{outer_name}!{ninfo.filename}"
                        members.append(
                            ArchiveShowTechMember(
                                display_name=disp,
                                outer_member=outer_name,
                                inner_member=ninfo.filename,
                            )
                        )
        else:
            bio.seek(0)
            try:
                with tarfile.open(fileobj=bio, mode="r:*") as ntar:
                    for nmem in ntar.getmembers():
                        if nmem.isfile() and is_showtech(nmem.name):
                            disp = f"{outer_name}!{nmem.name}"
                            members.append(
                                ArchiveShowTechMember(
                                    display_name=outer_name,
                                    outer_member=outer_name,
                                    inner_member=nmem.name,
                                )
                            )
            except tarfile.TarError:
                LOG.debug("Nested member is not a tar archive: %s", outer_name)

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if is_showtech(info.filename):
                    members.append(
                        ArchiveShowTechMember(
                            display_name=info.filename,
                            outer_member=None,
                            inner_member=info.filename,
                        )
                    )
                elif is_nested_archive(info.filename):
                    try:
                        with zf.open(info.filename, "r") as nf:
                            data = nf.read()
                        scan_nested(info.filename, data)
                    except OSError as exc:
                        LOG.warning("Failed to inspect nested archive %s: %s", info.filename, exc)
    else:
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    if is_showtech(member.name):
                        members.append(
                            ArchiveShowTechMember(
                                display_name=member.name,
                                outer_member=None,
                                inner_member=member.name,
                            )
                        )
                    elif is_nested_archive(member.name):
                        try:
                            f = tf.extractfile(member)
                            if f is None:
                                continue
                            data = f.read()
                            scan_nested(member.name, data)
                        except OSError as exc:
                            LOG.warning("Failed to inspect nested archive %s: %s", member.name, exc)
        except tarfile.TarError:
            LOG.error("Unsupported archive format or failed to open: %s", archive_path)

    return members


def read_text_from_archive_member(archive_path: Path, spec: ArchiveShowTechMember) -> str:
    """
    Read text for a show-tech file represented by ArchiveShowTechMember
    from the given archive, handling at most one level of nesting.
    """
    # Top-level member
    if spec.outer_member is None:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path, "r") as zf:
                with zf.open(spec.inner_member, "r") as f:
                    return f.read().decode("utf-8", errors="replace")
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                member = tf.getmember(spec.inner_member)
                f = tf.extractfile(member)
                if f is None:
                    return ""
                data = f.read()
                return data.decode("utf-8", errors="replace")

    # Nested archive case
    outer_name = spec.outer_member
    if outer_name is None:
        return ""

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as zf:
            from io import BytesIO

            with zf.open(outer_name, "r") as of:
                outer_bytes = of.read()
            bio = BytesIO(outer_bytes)
            if zipfile.is_zipfile(bio):
                bio.seek(0)
                with zipfile.ZipFile(bio, "r") as nz:
                    with nz.open(spec.inner_member, "r") as f:
                        return f.read().decode("utf-8", errors="replace")
            else:
                bio.seek(0)
                try:
                    with tarfile.open(fileobj=bio, mode="r:*") as ntar:
                        member = ntar.getmember(spec.inner_member)
                        f = ntar.extractfile(member)
                        if f is None:
                            return ""
                        data = f.read()
                        return data.decode("utf-8", errors="replace")
                except tarfile.TarError:
                    return ""
    else:
        with tarfile.open(archive_path, "r:*") as tf:
            from io import BytesIO

            outer_member = tf.getmember(outer_name)
            of = tf.extractfile(outer_member)
            if of is None:
                return ""
            outer_bytes = of.read()
            bio = BytesIO(outer_bytes)
            if zipfile.is_zipfile(bio):
                bio.seek(0)
                with zipfile.ZipFile(bio, "r") as nz:
                    with nz.open(spec.inner_member, "r") as f:
                        return f.read().decode("utf-8", errors="replace")
            else:
                bio.seek(0)
                try:
                    with tarfile.open(fileobj=bio, mode="r:*") as ntar:
                        member = ntar.getmember(spec.inner_member)
                        f = ntar.extractfile(member)
                        if f is None:
                            return ""
                        data = f.read()
                        return data.decode("utf-8", errors="replace")
                except tarfile.TarError:
                    return ""


# ---------------------------------------------------------------------------
# Basic parsers for show version / clock
# ---------------------------------------------------------------------------


SHOW_VERSION_VERSION_RE = re.compile(r"^\s*Software image version:\s*(\S+)", re.IGNORECASE)
SHOW_VERSION_ARCH_RE = re.compile(r"^\s*Architecture\s*:\s*(\S+)", re.IGNORECASE)
SHOW_VERSION_UPTIME_RE = re.compile(r"^\s*Uptime\s*:\s*(.+)$", re.IGNORECASE)
SHOW_VERSION_MEM_RE = re.compile(
    r"^\s*(Total|Free)\s+memory\s*:\s*([0-9]+)\s*(\w+)?", re.IGNORECASE
)

SHOW_CLOCK_RE = re.compile(r"^(\S.+)$")


def parse_show_version(ctx: TechSupportContext) -> List[CheckResult]:
    blocks = ctx.get_blocks("show version")
    if not blocks:
        return [
            CheckResult(
                name="show_version_present",
                category="system",
                severity=Severity.WARN,
                summary="show version output not found",
            )
        ]

    # Only use first block
    lines = blocks[0].lines
    total_mem = None
    free_mem = None

    # Model: always use first non-empty line as full model string
    for line in lines:
        stripped = line.strip()
        if stripped:
            ctx.hw_model = stripped
            break

    for line in lines:
        if m := SHOW_VERSION_VERSION_RE.search(line):
            ctx.eos_version = m.group(1)
        elif m := SHOW_VERSION_ARCH_RE.search(line):
            ctx.arch = m.group(1)
        elif m := SHOW_VERSION_UPTIME_RE.search(line):
            ctx.uptime = m.group(1).strip()
        elif m := SHOW_VERSION_MEM_RE.search(line):
            kind = m.group(1).lower()
            value = int(m.group(2))
            unit = (m.group(3) or "").lower()
            # Assume KB/MB/GB if provided; default to MB if no unit.
            if unit.startswith("g"):
                value_bytes = value * 1024 * 1024 * 1024
            elif unit.startswith("m") or unit == "":
                value_bytes = value * 1024 * 1024
            elif unit.startswith("k"):
                value_bytes = value * 1024
            else:
                value_bytes = value
            if "total" in kind:
                total_mem = value_bytes
            elif "free" in kind:
                free_mem = value_bytes
        # Stop processing after Free memory line as requested
        if "Free memory" in line:
            break

    # Fallback for model: if still unknown, use first non-empty line as full model string.
    if ctx.hw_model is None:
        for line in lines:
            stripped = line.strip()
            if stripped:
                ctx.hw_model = stripped
                break

    ctx.total_mem = total_mem
    ctx.free_mem = free_mem

    results: List[CheckResult] = []

    # Architecture check
    if ctx.arch:
        if ctx.arch.lower() != "x86_64":
            results.append(
                CheckResult(
                    name="architecture",
                    category="system",
                    severity=Severity.WARN,
                    summary=f"Architecture is {ctx.arch}, recommend using 64-bit EOS (x86_64).",
                )
            )
        else:
            results.append(
                CheckResult(
                    name="architecture",
                    category="system",
                    severity=Severity.OK,
                    summary=f"Architecture is x86_64.",
                )
            )

    # Memory check
    if total_mem is not None and free_mem is not None:
        ratio = free_mem / float(total_mem) if total_mem else 0.0
        if ratio < 0.10:
            sev = Severity.WARN
            summary = (
                f"Free memory below 10%% of total "
                f"({free_mem} bytes free / {total_mem} bytes total)."
            )
        else:
            sev = Severity.OK
            summary = (
                f"Free memory sufficient "
                f"({free_mem} bytes free / {total_mem} bytes total)."
            )
        results.append(
            CheckResult(
                name="memory_free",
                category="system",
                severity=sev,
                summary=summary,
            )
        )

    return results


def parse_show_clock(ctx: TechSupportContext) -> List[CheckResult]:
    blocks = ctx.get_blocks("show clock")
    if not blocks:
        return [
            CheckResult(
                name="show_clock_present",
                category="system",
                severity=Severity.WARN,
                summary="show clock output not found",
            )
        ]

    # First non-empty line as system time
    system_time = None
    for line in blocks[0].lines:
        line = line.strip()
        if not line:
            continue
        if m := SHOW_CLOCK_RE.match(line):
            system_time = m.group(1).strip()
            break

    ctx.system_time = system_time

    return [
        CheckResult(
            name="system_time",
            category="system",
            severity=Severity.INFO,
            summary=f"System time: {system_time}" if system_time else "System time not parsed.",
        )
    ]


def populate_hostname_from_running_config(ctx: TechSupportContext) -> None:
    """Fallback: extract hostname from 'show running-config sanitized' if missing."""
    if ctx.hostname:
        return
    blocks = ctx.get_blocks("show running-config sanitized")
    if not blocks:
        return
    for line in blocks[0].lines:
        line = line.strip()
        if not line or line.startswith("!"):
            continue
        m = re.match(r"^hostname\s+(\S+)", line)
        if m:
            ctx.hostname = m.group(1)
            break


def infer_platform_series(hw_model: Optional[str]) -> str:
    if not hw_model:
        return "other"
    model = hw_model.lower()
    if model.startswith("dcs-78") or "7800" in model or model.startswith("780"):
        return "78xx"
    if model.startswith("dcs-75") or "7500" in model or model.startswith("750"):
        return "75xx"
    if "7368" in model:
        return "7368"
    if "7289" in model:
        return "7289"
    return "other"


# ---------------------------------------------------------------------------
# Full check framework
# ---------------------------------------------------------------------------


class BaseCheck:
    name: str = "base"
    category: str = "generic"
    supported_platforms: Sequence[str] = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        raise NotImplementedError


REGISTERED_CHECKS: List[BaseCheck] = []


def register_check(cls):
    """Class decorator to register a check."""
    instance = cls()
    REGISTERED_CHECKS.append(instance)
    return cls


def platform_supported(check: BaseCheck, platform: str) -> bool:
    if "all" in check.supported_platforms:
        return True
    return platform in check.supported_platforms


# -------------------------- Generic checks ---------------------------------


@register_check
class CoolingStatusCheck(BaseCheck):
    name = "cooling_status"
    category = "environment"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show system env cooling")
        if not blocks:
            # Command not present – silently skip this check
            return []
        lines = blocks[0].lines
        text = "\n".join(lines)
        # Allow optional colon and capture rest of line as status.
        m = re.search(
            r"System\s+cooling\s+status\s+is\s*:?\s*(\S.*)$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if not m:
            # If format is unfamiliar, skip instead of emitting noisy INFO.
            return []
        status = m.group(1)
        details: List[str] = []
        _maybe_add_debug_raw(details, "show system env cooling", lines)
        if status.lower() != "ok":
            sev = Severity.WARN
            summary = f"System cooling status is {status}."
        else:
            sev = Severity.OK
            summary = "System cooling status is Ok."
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
                details=details,
            )
        ]


@register_check
class TemperatureStatusCheck(BaseCheck):
    name = "temperature_status"
    category = "environment"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show system env temperature")
        if not blocks:
            # Command not present – silently skip this check
            return []
        lines = blocks[0].lines
        text = "\n".join(lines)
        m = re.search(
            r"System\s+temperature\s+status\s+is\s*:?\s*(\S.*)$",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if not m:
            # If format is unfamiliar, skip instead of emitting noisy INFO.
            return []
        status = m.group(1)
        details: List[str] = []
        _maybe_add_debug_raw(details, "show system env temperature", lines)
        if status.lower() != "ok":
            sev = Severity.WARN
            summary = f"System temperature status is {status}."
        else:
            sev = Severity.OK
            summary = "System temperature status is Ok."
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
                details=details,
            )
        ]


@register_check
class CoreDumpCheck(BaseCheck):
    name = "core_dump_files"
    category = "system"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("bash ls -ltr /var/core")
        if not blocks:
            # Command not present – silently skip this check
            return []
        lines = blocks[0].lines
        count = 0
        for line in lines:
            if not line.strip():
                continue
            if "minidump" in line:
                continue
            if "No such file" in line or "cannot access" in line:
                continue
            # treat any remaining file listing as core
            parts = line.split()
            if len(parts) >= 9:
                count += 1
        details: List[str] = []
        _maybe_add_debug_raw(details, "bash ls -ltr /var/core", lines)

        if count > 0:
            sev = Severity.WARN
            summary = f"Found {count} core dump file(s) under /var/core (excluding minidump)."
        else:
            sev = Severity.OK
            summary = "No core dump files under /var/core (excluding minidump)."
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
                details=details,
            )
        ]


@register_check
class FlashUsageCheck(BaseCheck):
    name = "flash_usage"
    category = "storage"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("bash df -h")
        if not blocks:
            # Command not present – silently skip this check
            return []

        over = []
        lines = blocks[0].lines
        for line in lines:
            if "/mnt/flash" not in line:
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            # df typical: Filesystem Size Used Avail Use% Mounted on
            # handle possible shift if filesystem name has spaces by scanning for % value
            use_field = None
            for p in parts:
                if p.endswith("%") and p[:-1].isdigit():
                    use_field = p
                    break
            if not use_field:
                continue
            try:
                pct = int(use_field.rstrip("%"))
            except ValueError:
                continue
            if pct > 90:
                over.append((line.strip(), pct))

        if over:
            sev = Severity.WARN
            summary = f"/mnt/flash usage exceeds 90%% on {len(over)} entry(ies)."
            details = [f"{ln} (Use%={pct})" for ln, pct in over]
        else:
            sev = Severity.OK
            summary = "/mnt/flash usage is below or equal to 90%."
            details = []
        _maybe_add_debug_raw(details, "bash df -h", lines)
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
                details=details,
            )
        ]


@register_check
class ExtensionsDetailCheck(BaseCheck):
    name = "extensions_detail"
    category = "software"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show extensions detail")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show extensions detail output not found.",
                )
            ]

        entries = []
        cur = {"Name": None, "Presence": None, "Status": None, "Boot": None}

        def flush():
            if any(cur.values()):
                entries.append(cur.copy())

        for line in blocks[0].lines:
            if not line.strip():
                flush()
                cur = {"Name": None, "Presence": None, "Status": None, "Boot": None}
                continue
            for key in list(cur.keys()):
                m = re.search(rf"^{key}\s*:\s*(.+)$", line.strip())
                if m:
                    cur[key] = m.group(1).strip()
        flush()

        details = [
            f"Name={e['Name']}, Presence={e['Presence']}, Status={e['Status']}, Boot={e['Boot']}"
            for e in entries
        ]
        summary = f"Found {len(entries)} extension patch entries."
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.INFO,
                summary=summary,
                details=details,
            )
        ]


def _parse_numeric_with_unit(token: str) -> Optional[int]:
    """Parse value with units like 100m, 2g, returning bytes."""
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmMgG]?)$", token)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "g":
        value *= 1024**3
    elif unit == "m":
        value *= 1024**2
    elif unit == "k":
        value *= 1024
    return int(value)


def _maybe_add_debug_raw(details: List[str], cmd: str, lines: Sequence[str]) -> None:
    """Append raw command output to details when debug logging is enabled."""
    if not LOG.isEnabledFor(logging.DEBUG):
        return
    if not lines:
        return
    max_lines = 50
    snippet_lines = list(lines[:max_lines])
    if len(lines) > max_lines:
        snippet_lines.append(f"... ({len(lines) - max_lines} more line(s) truncated)")
    snippet = "\n".join(snippet_lines)
    details.append(f"[DEBUG raw {cmd}]\n{snippet}")


@register_check
class CpuUsageCheck(BaseCheck):
    name = "cpu_usage_top"
    category = "process"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show processes top once")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show processes top once output not found.",
                )
            ]
        lines = blocks[0].lines
        offenders = []
        for line in lines:
            if not line.strip() or "%CPU" in line:
                continue
            parts = line.split()
            # Look for a numeric token representing %CPU
            cpu_val = None
            for tok in parts:
                if tok.replace(".", "", 1).isdigit():
                    try:
                        v = float(tok)
                    except ValueError:
                        continue
                    if v >= 0 and v <= 800:  # crude sanity
                        cpu_val = v
                        break
            if cpu_val is None:
                continue
            if cpu_val > 99:
                offenders.append((line.strip(), cpu_val))
        if not offenders:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.OK,
                    summary="No processes with CPU usage greater than 99%.",
                )
            ]
        sev = Severity.WARN if any(v > 100 for _, v in offenders) else Severity.INFO
        summary = f"{len(offenders)} process(es) with CPU usage > 99%."
        details = [f"{ln} (CPU={v})" for ln, v in offenders]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
                details=details,
            )
        ]


@register_check
class MemoryUsageCheck(BaseCheck):
    name = "memory_usage_top"
    category = "process"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show processes top memory once")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show processes top memory once output not found.",
                )
            ]
        lines = blocks[0].lines
        offenders = []
        for line in lines:
            if not line.strip() or "RES" in line:
                continue
            parts = line.split()
            # Try to find a token that looks like RES (e.g. 500m, 1g)
            res_bytes = None
            for tok in parts:
                val = _parse_numeric_with_unit(tok)
                if val is not None:
                    res_bytes = val
                    break
            if res_bytes is None:
                continue
            if res_bytes > 1024**3:  # >1g
                offenders.append((line.strip(), res_bytes))
        if not offenders:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.OK,
                    summary="No processes with RES > 1g.",
                )
            ]
        sev = Severity.WARN if any(v > 2 * 1024**3 for _, v in offenders) else Severity.INFO
        summary = f"{len(offenders)} process(es) with RES > 1g."
        details = [f"{ln} (RES={v} bytes)" for ln, v in offenders]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
                details=details,
            )
        ]


@register_check
class ModuleUptimeCheck(BaseCheck):
    name = "module_uptime"
    category = "hardware"
    supported_platforms = ("78xx", "75xx", "7368", "7289")

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show module")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show module output not found.",
                )
            ]
        anomalous = []
        for line in blocks[0].lines:
            if not line.strip() or "Uptime" in line or "Module" in line:
                continue
            if "N/A" in line or "0 days" in line or "00:00" in line:
                anomalous.append(line.strip())
        if not anomalous:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="No obviously abnormal module uptime detected.",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.INFO,
                summary=f"{len(anomalous)} module(s) with potentially abnormal uptime.",
                details=anomalous,
            )
        ]


@register_check
class SandHealthCheck(BaseCheck):
    name = "platform_sand_health"
    category = "hardware"
    supported_platforms = ("78xx", "75xx")

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show platform sand health")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show platform sand health output not found.",
                )
            ]
        text = "\n".join(blocks[0].lines)
        if re.search(r"fail|error|not\s+initial", text, re.IGNORECASE):
            sev = Severity.WARN
            summary = "Detected possible linecard/fabric initialization issues in sand health."
        else:
            sev = Severity.OK
            summary = "No obvious initialization failures in sand health."
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
            )
        ]


@register_check
class FapFabricSerdesCheck(BaseCheck):
    name = "fap_fabric_serdes"
    category = "hardware"
    supported_platforms = ("78xx", "75xx")

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show platform fap fabric detail")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show platform fap fabric detail output not found.",
                )
            ]
        text = "\n".join(blocks[0].lines)
        if ctx.platform_series == "78xx":
            pattern = r"(U--- Ramon|---U Ramon|I--- Ramon|---I Ramon|\|--- Ramon|---\| Ramon)"
        else:
            pattern = r"(U--- Fe|---U Fe|I--- Fe|---I Fe|\|--- Fe|---\| Fe)"
        matches = re.findall(pattern, text)
        if matches:
            sev = Severity.WARN
            summary = f"Detected {len(matches)} abnormal SerDes link entries in FAP fabric detail."
        else:
            sev = Severity.OK
            summary = "No abnormal SerDes link entries detected in FAP fabric detail."
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
            )
        ]


@register_check
class RedundancyStatusCheck(BaseCheck):
    name = "redundancy_status"
    category = "system"
    supported_platforms = ("78xx", "75xx")

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show redundancy status")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show redundancy status output not found.",
                )
            ]
        lines = blocks[0].lines
        active_unit1 = False
        op_proto = None
        cfg_proto = None
        for line in lines:
            if "ACTIVE" in line and "unit 1" in line:
                active_unit1 = True
            if "Redundancy Protocol (Operational)" in line:
                op_proto = line.split(":", 1)[-1].strip()
            if "Redundancy Protocol (Configured)" in line:
                cfg_proto = line.split(":", 1)[-1].strip()
        results: List[CheckResult] = []
        if active_unit1:
            results.append(
                CheckResult(
                    name=f"{self.name}_active_unit",
                    category=self.category,
                    severity=Severity.OK,
                    summary="ACTIVE is on unit 1.",
                )
            )
        else:
            results.append(
                CheckResult(
                    name=f"{self.name}_active_unit",
                    category=self.category,
                    severity=Severity.WARN,
                    summary="ACTIVE is not on unit 1.",
                )
            )
        if op_proto and cfg_proto:
            if op_proto == cfg_proto:
                results.append(
                    CheckResult(
                        name=f"{self.name}_protocol",
                        category=self.category,
                        severity=Severity.OK,
                        summary=f"Redundancy Protocol Operational and Configured both '{op_proto}'.",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name=f"{self.name}_protocol",
                        category=self.category,
                        severity=Severity.WARN,
                        summary=(
                            "Redundancy Protocol mismatch: "
                            f"Operational='{op_proto}', Configured='{cfg_proto}'."
                        ),
                    )
                )
        return results


@register_check
class PciErrorCheck(BaseCheck):
    name = "pci_errors"
    category = "hardware"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show pci")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show pci output not found.",
                )
            ]
        text = "\n".join(blocks[0].lines)
        if re.search(r"FatalErr", text, re.IGNORECASE) or re.search(
            r"SMBusERR", text, re.IGNORECASE
        ):
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="Detected FatalErr or SMBusERR in PCI output.",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No FatalErr or SMBusERR found in PCI output.",
            )
        ]


@register_check
class AgentCrashLogCheck(BaseCheck):
    name = "agent_logs_crash"
    category = "software"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show agent logs crash")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show agent logs crash output not found.",
                )
            ]
        lines = [l for l in blocks[0].lines if l.strip()]
        if not lines:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.OK,
                    summary="No agent crash logs.",
                )
            ]
        # treat explicit 'No crash' message as OK
        joined = "\n".join(lines)
        if re.search(r"no\s+crash", joined, re.IGNORECASE):
            sev = Severity.OK
            summary = "No agent crash logs (explicit)."
        else:
            sev = Severity.WARN
            summary = f"Agent crash logs present ({len(lines)} line(s))."
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
            )
        ]


@register_check
class PowerInputCheck(BaseCheck):
    name = "power_input_voltage"
    category = "environment"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show system environment power detail")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show system environment power detail output not found.",
                )
            ]
        lines = blocks[0].lines
        zero_entries = []
        for line in lines:
            if "Input Voltage" in line:
                m = re.search(r"Input Voltage\s*:\s*([0-9]+)", line)
                if m and int(m.group(1)) == 0:
                    zero_entries.append(line.strip())
        if zero_entries:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="Detected PSU(s) with input voltage 0.",
                    details=zero_entries,
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No PSU with input voltage 0.",
            )
        ]


@register_check
class LoggingThresholdErrorsCheck(BaseCheck):
    name = "logging_threshold_errors"
    category = "hardware"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show logging threshold errors")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show logging threshold errors output not found.",
                )
            ]
        text = "\n".join(blocks[0].lines)
        if re.search(r"\bECC\b", text) or re.search(r"\bCRC\b", text):
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="ECC or CRC error logs detected in logging threshold errors.",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No ECC/CRC error logs detected in logging threshold errors.",
            )
        ]


@register_check
class InterfaceQueueDropsCheck(BaseCheck):
    name = "interfaces_queue_drops"
    category = "interface"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show interfaces counters queue drops")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show interfaces counters queue drops output not found.",
                )
            ]
        lines = [l for l in blocks[0].lines if l.strip()]
        if len(lines) > 0:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="Queue drops present in interface counters.",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No queue drops in interface counters.",
            )
        ]


@register_check
class CpuQueueDropsCheck(BaseCheck):
    name = "cpu_queue_drops"
    category = "system"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show cpu counters queue")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show cpu counters queue output not found.",
                )
            ]
        lines = [l for l in blocks[0].lines if l.strip()]
        offenders = []
        for line in lines:
            if "|" in line:
                parts = [p.strip() for p in line.split("|") if p.strip()]
            else:
                parts = line.split()
            for tok in reversed(parts):
                tok_clean = tok.replace(",", "").strip()
                if tok_clean.isdigit():
                    val = int(tok_clean)
                    if val > 1_000_000:
                        offenders.append((line.strip(), val))
                    break
        if offenders:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"CPU queue drops exceed 1 million on {len(offenders)} entry(ies).",
                    details=[f"{ln} (drops={v})" for ln, v in offenders],
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No CPU queue drops above 1 million.",
            )
        ]


@register_check
class InterfaceDiscardsCheck(BaseCheck):
    name = "interfaces_discards"
    category = "interface"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show interfaces counters discards")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show interfaces counters discards output not found.",
                )
            ]
        lines = [l for l in blocks[0].lines if l.strip()]
        if lines:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="Interface discards present.",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No interface discards present.",
            )
        ]


@register_check
class InterfaceErrorsCheck(BaseCheck):
    name = "interfaces_errors"
    category = "interface"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show interfaces counters errors")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show interfaces counters errors output not found.",
                )
            ]
        lines = [l for l in blocks[0].lines if l.strip()]
        if lines:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="Interface error counters present.",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No interface error counters present.",
            )
        ]


@register_check
class HardwareCounterDropCheck(BaseCheck):
    name = "hardware_counter_drop"
    category = "hardware"
    supported_platforms = ("78xx", "75xx", "7289")

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show hardware counter drop")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show hardware counter drop output not found.",
                )
            ]
        text = "\n".join(blocks[0].lines)
        if not ctx.system_time:
            # cannot compare date, just check presence of A/C drops
            if re.search(r"Adverse\s*\(A\)\s*Drops", text) or re.search(
                r"Congestion\s*\(C\)\s*Drops", text
            ):
                sev = Severity.WARN
                summary = "Adverse or Congestion drops detected (system time unavailable)."
            else:
                sev = Severity.OK
                summary = "No Adverse or Congestion drops detected."
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=sev,
                    summary=summary,
                )
            ]

        # Try to parse date from show clock (first tokenized date)
        clock = ctx.system_time
        drop_same_day = False
        try:
            # Example: Thu Jan 29 23:10:00 2026
            dt_clock = _dt.datetime.strptime(clock, "%a %b %d %H:%M:%S %Y")
            date_clock = dt_clock.date()
            for line in blocks[0].lines:
                if "Last Occurrence" in line:
                    # naive approach: use last 4 tokens as date/time/year
                    parts = line.split()
                    tail = " ".join(parts[-4:])
                    try:
                        dt_last = _dt.datetime.strptime(tail, "%b %d %H:%M:%S %Y")
                        if dt_last.date() == date_clock:
                            drop_same_day = True
                            break
                    except Exception:
                        continue
        except Exception:
            LOG.debug("Failed to parse show clock time for hardware counter drop comparison.")

        if drop_same_day and (
            re.search(r"Adverse\s*\(A\)\s*Drops", text)
            or re.search(r"Congestion\s*\(C\)\s*Drops", text)
        ):
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="Adverse (A) or Congestion (C) drops occurred on the same day as show clock.",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No Adverse/Congestion drops with last occurrence on current day.",
            )
        ]


@register_check
class HardwareCapacityCheck(BaseCheck):
    name = "hardware_capacity"
    category = "hardware"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show hardware capacity")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show hardware capacity output not found.",
                )
            ]
        lines = blocks[0].lines
        over = []
        for line in lines:
            if "%" not in line:
                continue
            m = re.search(r"(\d+)%\s*Used", line)
            if not m:
                continue
            val = int(m.group(1))
            if val > 90:
                over.append((line.strip(), val))
        if over:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"Hardware capacity Used exceeds 90% on {len(over)} resource(s).",
                    details=[f"{ln} (Used={v}%)" for ln, v in over],
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="Hardware capacity Used is below or equal to 90% for all resources.",
            )
        ]


@register_check
class SystemHealthStorageCheck(BaseCheck):
    name = "system_health_storage"
    category = "storage"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show system health storage")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show system health storage output not found.",
                )
            ]
        lines = blocks[0].lines
        bad_status = []
        low_lifetime = []
        for line in lines:
            if "Status" in line:
                m = re.search(r"Status\s*:\s*(\S+)", line)
                if m and m.group(1).lower() != "ok":
                    bad_status.append(line.strip())
            if "Lifetime remaining" in line:
                m = re.search(r"([0-9]+)\s*%", line)
                if m:
                    val = int(m.group(1))
                    if val < 10:
                        low_lifetime.append(line.strip())
        sev = Severity.OK
        details: List[str] = []
        if bad_status:
            sev = Severity.WARN
            details.extend(f"Bad status: {l}" for l in bad_status)
        if low_lifetime:
            sev = Severity.WARN
            details.extend(f"Low lifetime: {l}" for l in low_lifetime)
        if sev == Severity.OK:
            summary = "All storage status Ok and lifetime remaining >= 10%."
        else:
            summary = "Storage issues detected (status not Ok or lifetime < 10%)."
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=sev,
                summary=summary,
                details=details,
            )
        ]


@register_check
class HardwareFpgaErrorCheck(BaseCheck):
    name = "hardware_fpga_error"
    category = "hardware"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show hardware fpga error")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show hardware fpga error output not found.",
                )
            ]
        lines = blocks[0].lines
        offenders = []
        for line in lines:
            if "error" in line.lower():
                m = re.search(r"([0-9]+)", line)
                if m and int(m.group(1)) != 0:
                    offenders.append(line.strip())
        if offenders:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="FPGA error count non-zero detected.",
                    details=offenders,
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No non-zero FPGA error counts detected.",
            )
        ]


@register_check
class ScdSatelliteRetryErrCheck(BaseCheck):
    name = "scd_satellite_retry_error"
    category = "hardware"
    supported_platforms = ("7368", "7289")

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show platform scd satellite debug")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show platform scd satellite debug output not found.",
                )
            ]
        lines = blocks[0].lines
        offenders = []
        for line in lines:
            if "RetryErr" in line:
                m = re.search(r"RetryErr\s*=\s*(0x[0-9a-fA-F]+)", line)
                if m and m.group(1).lower() != "0x0":
                    offenders.append(line.strip())
        if offenders:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="RetryErr not 0x0 in satellite debug.",
                    details=offenders,
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="RetryErr is 0x0 for all entries in satellite debug.",
            )
        ]


@register_check
class RunningConfigCheck(BaseCheck):
    name = "running_config_78xx_special"
    category = "config"
    supported_platforms = ("78xx",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show running-config sanitized")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show running-config sanitized output not found.",
                )
            ]
        pattern = "ip hardware fib next-hop arp dedicated"
        found = any(pattern in line for line in blocks[0].lines)
        if found:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="Found 'ip hardware fib next-hop arp dedicated' in running-config on 78xx.",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No 'ip hardware fib next-hop arp dedicated' config on 78xx.",
            )
        ]


def run_all_checks(ctx: TechSupportContext) -> List[CheckResult]:
    results: List[CheckResult] = []
    # Core info parsers (populate context)
    results.extend(parse_show_version(ctx))
    results.extend(parse_show_clock(ctx))
    # Fallback hostname from running-config if not present in show version
    populate_hostname_from_running_config(ctx)
    # Platform series based on parsed model for later checks
    ctx.platform_series = infer_platform_series(ctx.hw_model)
    # Run registered checks based on platform
    for check in REGISTERED_CHECKS:
        if not platform_supported(check, ctx.platform_series):
            LOG.debug(
                "Skipping check %s for platform %s", check.name, ctx.platform_series
            )
            continue
        try:
            check_results = check.run(ctx) or []
            results.extend(check_results)
        except Exception as exc:  # defensive
            LOG.exception("Check %s failed: %s", check.name, exc)
            results.append(
                CheckResult(
                    name=f"{check.name}_internal_error",
                    category=check.category,
                    severity=Severity.WARN,
                    summary=f"Internal error while running check {check.name}: {exc}",
                )
            )
    return results


def aggregate_health(results: Sequence[CheckResult]) -> Tuple[Severity, int, int]:
    warn = sum(1 for r in results if r.severity == Severity.WARN)
    err = sum(1 for r in results if r.severity == Severity.ERROR)
    if err:
        health = Severity.ERROR
    elif warn:
        health = Severity.WARN
    else:
        health = Severity.OK
    return health, warn, err


def make_device_brief(ctx: TechSupportContext, results: Sequence[CheckResult]) -> DeviceBrief:
    script_time = _dt.datetime.now().isoformat(timespec="seconds")
    health, warn, err = aggregate_health(results)
    return DeviceBrief(
        script_time=script_time,
        hostname=ctx.hostname,
        eos_version=ctx.eos_version,
        hw_model=ctx.hw_model,
        system_time=ctx.system_time,
        health=health,
        warn_count=warn,
        error_count=err,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _compute_col_widths(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> List[int]:
    cols = len(headers)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(cols):
            cell = str(row[i]) if i < len(row) else ""
            if len(cell) > widths[i]:
                widths[i] = len(cell)
    return widths


def _ascii_table_with_widths(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    widths: Sequence[int],
) -> List[str]:
    """Render a simple ASCII table given headers, rows and precomputed widths."""
    cols = len(headers)

    def sep_line() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt_row(row_vals: Sequence[str]) -> str:
        cells = []
        for i in range(cols):
            cell = str(row_vals[i]) if i < len(row_vals) else ""
            cells.append(" " + cell.ljust(widths[i]) + " ")
        return "|" + "|".join(cells) + "|"

    lines: List[str] = []
    lines.append(sep_line())
    lines.append(fmt_row(headers))
    lines.append(sep_line())
    for row in rows:
        lines.append(fmt_row(row))
    lines.append(sep_line())
    return lines


def _ascii_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> List[str]:
    """Render an ASCII table computing widths from the given rows."""
    widths = _compute_col_widths(headers, rows)
    return _ascii_table_with_widths(headers, rows, widths)


def format_human_report(
    ctx: TechSupportContext,
    brief: DeviceBrief,
    results: Sequence[CheckResult],
    mode: str,
) -> str:
    lines: List[str] = []
    lines.append(f"Source: {ctx.source_id}")
    lines.append("")

    headers = ["Type", "Name", "Value", "Extra"]
    brief_rows: List[List[str]] = [
        ["BRIEF", "Script time", brief.script_time, ""],
        ["BRIEF", "Hostname", brief.hostname or "N/A", ""],
        ["BRIEF", "EOS version", brief.eos_version or "N/A", ""],
        ["BRIEF", "Model", brief.hw_model or "N/A", ""],
        ["BRIEF", "System time", brief.system_time or "N/A", ""],
        [
            "BRIEF",
            "Health",
            f"{brief.health.value} (WARN={brief.warn_count}, ERROR={brief.error_count})",
            "",
        ],
    ]

    if mode == "brief":
        # Only brief table; widths based on brief rows.
        widths = _compute_col_widths(headers, brief_rows)
        lines.extend(_ascii_table_with_widths(headers, brief_rows, widths))
        return "\n".join(lines)

    # verbose: build detail rows and ensure both tables share same widths
    detail_rows: List[List[str]] = []
    for r in results:
        detail_rows.append(
            [
                r.severity.value,
                f"{r.category}/{r.name}",
                r.summary,
                "",
            ]
        )
        for d in r.details:
            detail_rows.append(["", "", "", d])

    all_rows = brief_rows + detail_rows
    widths = _compute_col_widths(headers, all_rows)

    # Brief table
    lines.extend(_ascii_table_with_widths(headers, brief_rows, widths))
    lines.append("")
    lines.append("Detailed checks:")
    # Detailed table with identical column widths
    if detail_rows:
        lines.extend(_ascii_table_with_widths(headers, detail_rows, widths))

    return "\n".join(lines)


def format_json_report(
    ctx: TechSupportContext,
    brief: DeviceBrief,
    results: Sequence[CheckResult],
    mode: str,
) -> str:
    data = {
        "source": ctx.source_id,
        "brief": {
            "script_time": brief.script_time,
            "hostname": brief.hostname,
            "eos_version": brief.eos_version,
            "hw_model": brief.hw_model,
            "system_time": brief.system_time,
            "health": brief.health.value,
            "warn_count": brief.warn_count,
            "error_count": brief.error_count,
        },
    }
    if mode == "verbose":
        data["checks"] = [
            {
                "name": r.name,
                "category": r.category,
                "severity": r.severity.value,
                "summary": r.summary,
                "details": r.details,
            }
            for r in results
        ]
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def process_showtech_text(source_id: str, text: str, mode: str, as_json: bool) -> str:
    # Load into memory, parse, then drop raw text reference
    parser = TechSupportParser()
    blocks = parser.parse(text)
    text = ""  # release

    ctx = TechSupportContext(source_id, blocks)
    results = run_all_checks(ctx)
    brief = make_device_brief(ctx, results)

    if as_json:
        return format_json_report(ctx, brief, results, mode)
    else:
        return format_human_report(ctx, brief, results, mode)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    configure_logging(args.debug)

    outputs: List[str] = []

    for path_str in args.paths:
        path = Path(path_str)
        if path.is_dir():
            # Unpacked support-bundle directory
            root = path
            LOG.info("Processing directory: %s", root)
            files = discover_showtech_files_from_directory(root)
            if not files:
                LOG.warning("No show-tech files found under directory: %s", root)
                continue
            for f in files:
                LOG.info("Processing show-tech file from directory: %s", f)
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    LOG.error("Failed to read %s: %s", f, exc)
                    continue
                report = process_showtech_text(str(f), text, args.mode, args.json)
                outputs.append(report)
        elif path.is_file():
            # Decide if archive or plain show-tech file
            if zipfile.is_zipfile(path) or tarfile.is_tarfile(path):
                arch = path
                LOG.info("Processing archive: %s", arch)
                members = discover_showtech_members_from_archive(arch)
                if not members:
                    LOG.warning("No show-tech files found in archive: %s", arch)
                    continue
                for spec in members:
                    LOG.info(
                        "Processing show-tech member from archive %s: %s",
                        arch,
                        spec.display_name,
                    )
                    try:
                        text = read_text_from_archive_member(arch, spec)
                    except OSError as exc:
                        LOG.error(
                            "Failed to read member %s from archive %s: %s",
                            spec.display_name,
                            arch,
                            exc,
                        )
                        continue
                    report = process_showtech_text(
                        f"{arch}!{spec.display_name}", text, args.mode, args.json
                    )
                    outputs.append(report)
            else:
                LOG.info("Processing show-tech file: %s", path)
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    LOG.error("Failed to read %s: %s", path, exc)
                    continue
                report = process_showtech_text(str(path), text, args.mode, args.json)
                outputs.append(report)
        else:
            LOG.error("Path does not exist or is not accessible: %s", path)

    final_output = "\n\n".join(outputs)

    if args.output:
        out_path = Path(args.output)
        try:
            out_path.write_text(final_output, encoding="utf-8")
        except OSError as exc:
            LOG.error("Failed to write output to %s: %s", out_path, exc)
            print(final_output)
    else:
        print(final_output)


if __name__ == "__main__":
    main()


