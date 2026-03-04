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
import concurrent.futures
import datetime as _dt
import gc
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
__last_modified__ = "2026-03-04"
__version__ = "1.1.0"


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
        epilog=(
            "Examples:\n"
            "  %(prog)s /path/to/show-tech                    # Basic analysis\n"
            "  %(prog)s -v /path/to/show-tech                 # Verbose mode\n"
            "  %(prog)s -d /path/to/show-tech                # Debug mode\n"
            "  %(prog)s -j -o report.json /path/to/show-tech  # JSON output\n"
            "  %(prog)s -l                                   # List all checks\n"
            "  %(prog)s -c memory_usage_top /path/to/show-tech  # Show specific check\n"
            "  %(prog)s -s memory_usage_top /path/to/show-tech  # Skip specific check\n"
            "  %(prog)s -S hardware /path/to/show-tech       # Skip entire category\n"
            "  %(prog)s -s cpu_usage_top -S hardware /path/to/show-tech  # Combine options\n"
            "  %(prog)s -t 4 *.zip                           # Process archives with 4 threads\n"
            "  %(prog)s -t 1 /path/to/show-tech              # Disable parallel processing\n"
            "\n"
            "Author  : %(author)s\n"
            "Company : %(company)s\n"
            "Version : %(version)s (Last modified: %(last)s)"
        ),
    )

    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help=(
            "One or more inputs: show-tech/show-tech-support-all file, "
            "unpacked support-bundle directory, or support-bundle archive "
            "(tar/tar.gz/tgz/zip). Type is detected automatically. "
            "Not required when using --list-checks."
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
        "-w",
        "--warn-only",
        action="store_true",
        help=(
            "Warn-only report mode: brief summary plus all WARN-severity checks."
        ),
    )
    output_group.add_argument(
        "-j",
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
    debug_group.add_argument(
        "-l",
        "--list-checks",
        action="store_true",
        help="List all supported health checks and exit.",
    )
    debug_group.add_argument(
        "-c",
        "--show-checks-in-brief",
        nargs="*",
        metavar="CHECK_NAME",
        help=(
            "Show specified checks in brief mode output. "
            "If no check names provided, shows all supported checks list. "
            "Use --list-checks to see available check names."
        ),
    )
    debug_group.add_argument(
        "-s",
        "--skip-checks",
        nargs="+",
        metavar="CHECK_NAME",
        help=(
            "Skip specified checks during execution. "
            "Can specify multiple check names. "
            "Use --list-checks to see available check names."
        ),
    )
    debug_group.add_argument(
        "-S",
        "--skip-categories",
        nargs="+",
        metavar="CATEGORY",
        help=(
            "Skip all checks in specified categories during execution. "
            "Can specify multiple categories (e.g., system, hardware, interface). "
            "Use --list-checks to see available categories."
        ),
    )
    debug_group.add_argument(
        "-t",
        "--threads",
        type=int,
        metavar="N",
        default=None,
        help=(
            "Number of worker threads for parallel processing. "
            "Default: number of CPU cores. Set to 1 to disable parallel processing."
        ),
    )
    debug_group.add_argument(
        "-m",
        "--low-memory",
        action="store_true",
        help=(
            "Enable low-memory mode: files are loaded on-demand instead of pre-loading all files. "
            "Reduces memory usage at the cost of slightly slower processing. "
            "Recommended for systems with limited RAM or when processing many large files."
        ),
    )

    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = build_arg_parser()
    meta = {
        "prog": parser.prog,
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

    # Resolve output mode:
    # - verbose overrides other modes
    # - warn-only shows brief summary plus all WARN-severity checks
    # - default is brief
    if args.verbose:
        args.mode = "verbose"
    elif getattr(args, "warn_only", False):
        args.mode = "warn"
    else:
        args.mode = "brief"

    # When -c (show-checks-in-brief) is used, automatically enable debug mode.
    if args.show_checks_in_brief is not None:
        args.debug = True

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
    command: Optional[str] = None  # Command name for debug output


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

    # Match lines like: ------------- show version ------------- or ------------- bash ls -ltr /var/core -------------
    CMD_HEADER_RE = re.compile(r"^[-\s]+(?:show|bash).*[-\s]+$", re.IGNORECASE)

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
                # Example: "------------- show version -------------" or "------------- bash ls -ltr /var/core -------------"
                cmd = raw.strip("- ").strip()
                # Try to match "show ..." or "bash ..." commands
                m = re.search(r"((?:show|bash).*)$", cmd, re.IGNORECASE)
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
        self.platform_series: str = "other"  # 78xx / 75xx / 7368 / 7289 / 7388 / other

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
    # Match exact filenames or files containing show-tech/show-tech-support-all
    # Examples: "show-tech", "show-tech-support-all", 
    #           "localhost-show-tech-support-all-2026_02_08-07_29_38.log", etc.
    results: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        # Exact match
        if name in ("show-tech", "show-tech-support-all"):
            results.append(path)
        # Contains show-tech-support-all (for files like localhost-show-tech-support-all-*.log)
        elif "show-tech-support-all" in name:
            results.append(path)
        # Starts with show-tech but not extended variants
        elif (name.startswith("show-tech") and 
              not name.startswith("show-tech-support-extended") and
              not name.startswith("show-tech-support-ribd")):
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
        # Accept exact match or files containing show-tech/show-tech-support-all
        # Examples: "show-tech", "show-tech-support-all", 
        #           "localhost-show-tech-support-all-2026_02_08-07_29_38.log",
        #           "tmp/support-bundle-cmds/show-tech", etc.
        return (base in ("show-tech", "show-tech-support-all") or
                "show-tech-support-all" in base or
                (base.startswith("show-tech") and not base.startswith("show-tech-support-extended") 
                 and not base.startswith("show-tech-support-ribd")))
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
    # Match patterns like "dcs-78xx", "7800", "780", "78xx" etc.
    if (
        "dcs-78" in model
        or "7800" in model
        or model.startswith("780")
        or re.search(r"\b78\d{2}", model)
    ):
        return "78xx"
    # Match patterns like "dcs-75xx", "7500", "7516", "75xx" etc.
    if (
        "dcs-75" in model
        or "7500" in model
        or re.search(r"\b75\d{2}", model)
    ):
        return "75xx"
    if "7368" in model:
        return "7368"
    if "7289" in model:
        return "7289"
    if "7388" in model:
        return "7388"
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
    """Legacy function - no longer adds debug output to details.
    Full raw output is now handled in format_human_report when debug=True."""
    pass


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
        
        # Find %CPU column index from header and header line index
        cpu_col_idx = None
        header_line_idx = None
        for idx, line in enumerate(lines):
            if "%CPU" in line:
                parts = line.split()
                try:
                    cpu_col_idx = parts.index("%CPU")
                    header_line_idx = idx
                    break
                except ValueError:
                    # Try case-insensitive search
                    parts_lower = [p.lower() for p in parts]
                    try:
                        cpu_col_idx = parts_lower.index("%cpu")
                        header_line_idx = idx
                        break
                    except ValueError:
                        continue
        
        if cpu_col_idx is None:
            # Fallback: assume %CPU is ninth column (index 8, 0-based)
            cpu_col_idx = 8
            header_line_idx = 0  # Assume first line is header
        
        # Parse data lines (only lines after the header)
        start_idx = (header_line_idx + 1) if header_line_idx is not None else 1
        for line in lines[start_idx:]:
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) <= cpu_col_idx:
                continue
            try:
                cpu_val = float(parts[cpu_col_idx])
            except (ValueError, IndexError):
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
        offenders_1g = []  # RES > 1GB
        offenders_2g = []  # RES > 2GB
        
        # Find the header line to determine RES column index
        res_col_idx = None
        header_line_idx = None
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            # Look for header line containing "RES"
            if "RES" in stripped and "PID" in stripped:
                # This is the header line
                parts = stripped.split()
                # Find RES column index
                for i, part in enumerate(parts):
                    if part == "RES":
                        res_col_idx = i
                        header_line_idx = idx
                        break
                if res_col_idx is not None:
                    break
        
        # If we found the header, process data rows
        if res_col_idx is not None and header_line_idx is not None:
            # Process lines after header
            for line in lines[header_line_idx + 1:]:
                stripped = line.strip()
                if not stripped:
                    continue
                # Skip separator lines or lines that look like headers
                if "RES" in stripped and "PID" in stripped:
                    continue
                
                parts = stripped.split()
                # Check if we have enough columns
                if len(parts) > res_col_idx:
                    # Extract RES value from the correct column
                    res_token = parts[res_col_idx]
                    res_bytes = _parse_numeric_with_unit(res_token)
                    if res_bytes is not None:
                        if res_bytes > 1024**3:  # >1g
                            offenders_1g.append((stripped, res_bytes))
                            if res_bytes > 2 * 1024**3:  # >2g
                                offenders_2g.append((stripped, res_bytes))
        else:
            # Fallback: if header not found, try old method (find first parseable value)
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
                    offenders_1g.append((line.strip(), res_bytes))
                    if res_bytes > 2 * 1024**3:  # >2g
                        offenders_2g.append((line.strip(), res_bytes))
        
        # Determine severity
        if offenders_2g:
            sev = Severity.WARN
        elif offenders_1g:
            sev = Severity.INFO
        else:
            sev = Severity.OK
        
        # Build summary with both counts (always show, even if zero)
        summary = f"RES > 1GB: {len(offenders_1g)} process(es), RES > 2GB: {len(offenders_2g)} process(es)."
        
        # Details: include all offenders > 1GB
        details = [f"{ln} (RES={v} bytes)" for ln, v in offenders_1g]
        
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
    supported_platforms = ("78xx", "75xx", "7368", "7289", "7388")

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show module")
        LOG.debug("module_uptime check: found %d block(s) for 'show module'", len(blocks))
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
        lines = blocks[0].lines
        
        # Find the header line with "Status" and "Uptime" columns
        status_col_start = None
        uptime_col_start = None
        uptime_col_end = None
        header_found = False
        header_line_idx = -1
        
        for i, line in enumerate(lines):
            if "Status" in line and "Uptime" in line:
                # Find column positions
                status_idx = line.find("Status")
                uptime_idx = line.find("Uptime")
                if status_idx != -1 and uptime_idx != -1:
                    status_col_start = status_idx
                    uptime_col_start = uptime_idx
                    # Find where Uptime column ends (look for "Power" or end of line)
                    power_idx = line.find("Power", uptime_idx)
                    if power_idx != -1:
                        uptime_col_end = power_idx
                    else:
                        # Fallback: assume Uptime column is about 20 characters wide
                        uptime_col_end = uptime_idx + 20
                    header_found = True
                    header_line_idx = i
                    break
        
        # Normal status values (Ok/OK and Active/Standby are both valid across EOS versions)
        normal_statuses = {"Ok", "OK", "Active", "Standby"}
        
        # Only parse data rows after the Status/Uptime header is found
        if header_found:
            # Start parsing from the line after the separator line (usually header_line_idx + 2)
            for i in range(header_line_idx + 2, len(lines)):
                line = lines[i]
                
                # Skip empty lines and separator lines
                if not line.strip() or "---" in line:
                    continue
                
                # Stop if we hit another section header (like "MAC addresses")
                if "MAC addresses" in line or ("Module" in line and "Ports" in line):
                    break
                
                # Use fixed-width parsing
                if len(line) > uptime_col_start:
                    # Extract Status column
                    status_str = line[status_col_start:uptime_col_start].strip()
                    # Extract Uptime column (strip trailing "N/A" if Power column bled into slice)
                    uptime_str = line[uptime_col_start:uptime_col_end].strip()
                    if uptime_str.endswith(" N/A"):
                        uptime_str = uptime_str[:-4].strip()
                    
                    # Check if status is abnormal (not in normal_statuses)
                    is_abnormal_status = status_str and status_str not in normal_statuses
                    
                    # Check if uptime is abnormal (N/A, 0 days, or 00:00)
                    # Interpret "0 days" strictly as zero, not any string containing "0 days"
                    days_is_zero = False
                    m = re.search(r"(\d+)\s+days", uptime_str)
                    if m:
                        try:
                            days_is_zero = int(m.group(1)) == 0
                        except ValueError:
                            days_is_zero = False

                    is_abnormal_uptime = (
                        uptime_str == "N/A"
                        or days_is_zero
                        or uptime_str.startswith("00:00")
                        or (uptime_str and not re.search(r"\d+", uptime_str))  # No numbers at all
                    )

                    if is_abnormal_status or is_abnormal_uptime:
                        anomalous.append(line.strip())
        if not anomalous:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.OK,
                    summary="No abnormal module uptime detected.",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.WARN,
                summary=f"{len(anomalous)} module(s) with abnormal uptime detected.",
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
        LOG.debug("platform_sand_health check: found %d block(s) for 'show platform sand health'", len(blocks))
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
            summary = "Detected linecard/fabric initialization issues in sand health."
        else:
            sev = Severity.OK
            summary = "All linecards and fabric cards initialized successfully."
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
        LOG.debug("fap_fabric_serdes check: found %d block(s) for 'show platform fap fabric detail'", len(blocks))
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show platform fap fabric detail output not found.",
                )
            ]
        lines = blocks[0].lines
        if ctx.platform_series == "78xx":
            # Pattern: U--- Ramon|---U Ramon|I--- Ramon|I---I Ramon|---I Ramon|\|--- Ramon|---\| Ramon
            # Note: I---I Ramon is also a valid pattern (I---I followed by Ramon without space)
            # In Python regex, \| matches literal |, so we use [|] or \| to match |
            pattern = r"(U--- Ramon|[|]---U Ramon|I---I? Ramon|[|]---I Ramon|[|]--- Ramon|---[|] Ramon)"
        else:
            # Pattern: U--- Fe|---U Fe|I--- Fe|I---I Fe|---I Fe|\|--- Fe|---\| Fe
            # Note: I---I Fe is also a valid pattern (I---I followed by Fe without space)
            pattern = r"(U--- Fe|[|]---U Fe|I---I? Fe|[|]---I Fe|[|]--- Fe|---[|] Fe)"
        
        # Find matching lines (output full lines like egrep)
        matched_lines = []
        for line in lines:
            if re.search(pattern, line):
                stripped = line.strip()
                if stripped:
                    matched_lines.append(stripped)
        
        if matched_lines:
            sev = Severity.WARN
            summary = f"Detected {len(matched_lines)} abnormal SerDes link entries in FAP fabric detail."
            # Output full lines (like egrep output)
            details = matched_lines[:10]  # Limit to first 10 for normal output
            if len(matched_lines) > 10:
                details.append(f"... and {len(matched_lines) - 10} more")
        else:
            sev = Severity.OK
            summary = "No abnormal SerDes link entries detected in FAP fabric detail."
            details = []
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
class RedundancyStatusCheck(BaseCheck):
    name = "redundancy_status"
    category = "system"
    supported_platforms = ("78xx", "75xx")

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show redundancy status")
        LOG.debug("redundancy_status check: found %d block(s) for 'show redundancy status'", len(blocks))
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
        my_state_active = False
        unit_id_1 = False
        op_proto = None
        cfg_proto = None
        for line in lines:
            # Check for "my state = ACTIVE" or similar patterns
            if "my state" in line.lower() and "ACTIVE" in line:
                my_state_active = True
            # Check for "Unit ID = 1" or similar patterns
            if "unit id" in line.lower():
                # Extract the unit ID value
                m = re.search(r"unit\s+id\s*[=:]\s*(\d+)", line, re.IGNORECASE)
                if m:
                    unit_id = int(m.group(1))
                    if unit_id == 1:
                        unit_id_1 = True
            # Also check for legacy format: "ACTIVE" and "unit 1" in same line
            if "ACTIVE" in line and "unit 1" in line.lower():
                active_unit1 = True
        
        # ACTIVE is on unit 1 if: (my state is ACTIVE AND unit ID is 1) OR legacy format matched
        if (my_state_active and unit_id_1) or active_unit1:
            active_unit1 = True
            # Match "Redundancy Protocol (Operational): <value>" or similar formats
            if "Redundancy Protocol (Operational)" in line:
                # Try multiple formats: "key: value", "key = value", etc.
                parts = re.split(r"[:=]", line, 1)
                if len(parts) > 1:
                    op_proto = parts[-1].strip()
                else:
                    # Fallback: extract text after the key
                    m = re.search(r"Redundancy Protocol \(Operational\)\s+(.+)", line, re.IGNORECASE)
                    if m:
                        op_proto = m.group(1).strip()
            # Match "Redundancy Protocol (Configured): <value>" or similar formats
            if "Redundancy Protocol (Configured)" in line:
                parts = re.split(r"[:=]", line, 1)
                if len(parts) > 1:
                    cfg_proto = parts[-1].strip()
                else:
                    # Fallback: extract text after the key
                    m = re.search(r"Redundancy Protocol \(Configured\)\s+(.+)", line, re.IGNORECASE)
                    if m:
                        cfg_proto = m.group(1).strip()
        results: List[CheckResult] = []
        if active_unit1:
            results.append(
                CheckResult(
                    name=f"{self.name}_active_unit",
                    category=self.category,
                    severity=Severity.OK,
                    summary="ACTIVE is on unit 1.",
                    command="show redundancy status",
                )
            )
        else:
            results.append(
                CheckResult(
                    name=f"{self.name}_active_unit",
                    category=self.category,
                    severity=Severity.WARN,
                    summary="ACTIVE is not on unit 1.",
                    command="show redundancy status",
                )
            )
        if op_proto and cfg_proto:
            # Normalize protocol values for comparison (case-insensitive, strip whitespace)
            op_proto_norm = op_proto.strip().lower()
            cfg_proto_norm = cfg_proto.strip().lower()
            if op_proto_norm == cfg_proto_norm:
                results.append(
                    CheckResult(
                        name=f"{self.name}_protocol",
                        category=self.category,
                        severity=Severity.OK,
                        summary=f"Redundancy Protocol Operational and Configured both '{op_proto.strip()}'.",
                        command="show redundancy status",
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
                            f"Operational='{op_proto.strip()}', Configured='{cfg_proto.strip()}'."
                        ),
                        command="show redundancy status",
                    )
                )
        elif op_proto or cfg_proto:
            # Only one protocol found
            results.append(
                CheckResult(
                    name=f"{self.name}_protocol",
                    category=self.category,
                    severity=Severity.INFO,
                    summary=(
                        f"Redundancy Protocol partially found: "
                        f"Operational='{op_proto or 'N/A'}', Configured='{cfg_proto or 'N/A'}'."
                    ),
                    command="show redundancy status",
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
        lines = blocks[0].lines
        offenders = []
        
        # Find column indices for FatalErr and SMBusERR from header
        fatal_col_idx = None
        smbus_col_idx = None
        header_line_idx = None
        
        for idx, line in enumerate(lines):
            if not line.strip():
                continue
            # Look for header line containing FatalErr and/or SMBusERR
            parts = line.split()
            parts_lower = [p.lower() for p in parts]
            if "fatalerr" in parts_lower or "smbuserr" in parts_lower:
                try:
                    if "fatalerr" in parts_lower:
                        fatal_col_idx = parts_lower.index("fatalerr")
                    if "smbuserr" in parts_lower:
                        smbus_col_idx = parts_lower.index("smbuserr")
                    header_line_idx = idx
                    break
                except ValueError:
                    continue
        
        # If columns found, parse data rows
        if fatal_col_idx is not None or smbus_col_idx is not None:
            # Parse data lines (after header)
            start_idx = (header_line_idx + 1) if header_line_idx is not None else 0
            for line in lines[start_idx:]:
                if not line.strip():
                    continue
                parts = line.split()
                
                # Check FatalErr column
                if fatal_col_idx is not None and len(parts) > fatal_col_idx:
                    try:
                        fatal_val = int(parts[fatal_col_idx])
                        if fatal_val != 0:
                            offenders.append(f"FatalErr={fatal_val}: {line.strip()}")
                    except (ValueError, IndexError):
                        pass
                
                # Check SMBusERR column
                if smbus_col_idx is not None and len(parts) > smbus_col_idx:
                    try:
                        smbus_val = int(parts[smbus_col_idx])
                        if smbus_val != 0:
                            offenders.append(f"SMBusERR={smbus_val}: {line.strip()}")
                    except (ValueError, IndexError):
                        pass
        
        if offenders:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"Detected non-zero FatalErr or SMBusERR in PCI output ({len(offenders)} entry/ies).",
                    details=offenders,
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No non-zero FatalErr or SMBusERR detected in PCI output.",
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


# Configurable regex patterns for logging threshold errors check
# Easy to add/modify/remove patterns without changing code logic
LOGGING_THRESHOLD_ERROR_PATTERNS = [
    # Memory / link error indicators
    r"\bECC\b",
    r"\bCRC\b",
    # High-severity syslog levels 0/1/2 in tags like %AGENT-0-FOO:, %AGENT-1-FOO:, %AGENT-2-FOO:
    # Example: %AGENT-6-INITIALIZED: ...  (here 6 is the log level; we only match 0/1/2)
    r"%[A-Z0-9_-]+-[0-2]-[A-Z0-9_-]+:",
    # Add more patterns here as needed, e.g.:
    # r"\bFATAL\b",
    # r"\bCRITICAL\b",
]


@register_check
class LoggingThresholdErrorsCheck(BaseCheck):
    name = "logging_threshold_errors"
    category = "hardware"
    supported_platforms = ("all",)
    
    # Use shared patterns list
    ERROR_PATTERNS = LOGGING_THRESHOLD_ERROR_PATTERNS

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
        lines = blocks[0].lines
        matching_lines = []
        
        # Check each line against all patterns
        for line in lines:
            for pattern in self.ERROR_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    matching_lines.append(line.strip())
                    break  # Only add line once even if multiple patterns match
        
        if matching_lines:
            # Store matching lines in details for debug output
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"Error patterns detected in logging threshold errors ({len(matching_lines)} matching line(s)).",
                    details=matching_lines,
                    command="show logging threshold errors",  # Store command for debug filtering
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No error patterns detected in logging threshold errors.",
            )
        ]


def _parse_queue_drops_output(lines: List[str]) -> Tuple[Optional[str], List[str]]:
    """
    Parse 'show interfaces counters queue drops' output.
    Only matches:
    - Header line: contains DropPkts or DropOctets
    - Port lines: 2-3 columns (e.g., "Et12/1/1            TC0"), recorded as context
    - VOQ lines: start with "VOQ", recorded only if DropPkts or DropOctets is non-zero
    - Egress queue lines: precisely match lines containing "Egress queue" string,
      recorded only if DropPkts or DropOctets is non-zero
    
    Returns:
        (header_line, matched_lines) tuple
    """
    header_line = None
    drop_pkts_col_idx = None
    drop_octets_col_idx = None
    header_line_idx = None
    matched_lines = []
    
    # Find header line and column indices for DropPkts and DropOctets
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        parts_lower = [p.lower() for p in parts]
        
        # Look for header containing DropPkts and/or DropOctets
        if "droppkts" in parts_lower or "dropoctets" in parts_lower:
            try:
                if "droppkts" in parts_lower:
                    drop_pkts_col_idx = parts_lower.index("droppkts")
                if "dropoctets" in parts_lower:
                    drop_octets_col_idx = parts_lower.index("dropoctets")
                header_line = stripped
                header_line_idx = idx
                break
            except ValueError:
                continue
    
    # Parse data rows (after header)
    # Only match: header line, port lines, VOQ lines, and Egress queue lines
    if header_line_idx is not None:
        start_idx = header_line_idx + 1
        # Determine the maximum column index we need to check
        max_col_idx = max(
            drop_pkts_col_idx if drop_pkts_col_idx is not None else -1,
            drop_octets_col_idx if drop_octets_col_idx is not None else -1
        )
        
        for line in lines[start_idx:]:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip separator lines (lines with only dashes)
            if stripped.replace("-", "").strip() == "":
                continue
            
            parts = stripped.split()
            
            # Match VOQ lines (start with "VOQ") - check first to avoid being misidentified as port lines
            is_voq_line = stripped.startswith("VOQ")
            
            # Match Egress queue lines (precise match: must contain "Egress queue" string
            # and have enough columns to contain DropPkts/DropOctets)
            is_egress_queue_line = False
            if "egress queue" in stripped.lower() and len(parts) > max_col_idx:
                is_egress_queue_line = True
            
            # Match port lines (e.g., "Et12/1/1            TC0")
            # Port lines typically have 2-3 columns: port name and TC class
            # They don't have enough columns for DropPkts/DropOctets
            # Only match if not VOQ or Egress queue line
            if not (is_voq_line or is_egress_queue_line):
                if len(parts) <= max_col_idx:
                    # This could be a port line - check if it looks like one
                    # Port lines usually start with interface names (Et, Ma, etc.) and have TC class
                    if len(parts) >= 2 and len(parts) <= 3:
                        # Record port lines (they provide context)
                        matched_lines.append(stripped)
                continue
            
            # Only process VOQ lines and Egress queue lines
            if not (is_voq_line or is_egress_queue_line):
                continue
            
            # Adjust column indices based on line type
            # VOQ lines don't have Port and Class columns, so indices need to be adjusted
            if is_voq_line:
                # VOQ lines: DropPkts and DropOctets indices are 1 less than header
                # (Header: Port, Class, EnqPkts, EnqOctets, DropPkts, DropOctets)
                # (VOQ: VOQ, EnqPkts, EnqOctets, DropPkts, DropOctets)
                actual_drop_pkts_idx = drop_pkts_col_idx - 1 if drop_pkts_col_idx is not None else None
                actual_drop_octets_idx = drop_octets_col_idx - 1 if drop_octets_col_idx is not None else None
            else:
                # Egress queue lines: use original indices
                actual_drop_pkts_idx = drop_pkts_col_idx
                actual_drop_octets_idx = drop_octets_col_idx
            
            # Check DropPkts column
            drop_pkts_non_zero = False
            if actual_drop_pkts_idx is not None and len(parts) > actual_drop_pkts_idx:
                try:
                    drop_pkts_val = int(parts[actual_drop_pkts_idx].replace(",", ""))
                    if drop_pkts_val != 0:
                        drop_pkts_non_zero = True
                except (ValueError, IndexError):
                    # Cannot parse - skip this line
                    continue
            
            # Check DropOctets column
            drop_octets_non_zero = False
            if actual_drop_octets_idx is not None and len(parts) > actual_drop_octets_idx:
                try:
                    drop_octets_val = int(parts[actual_drop_octets_idx].replace(",", ""))
                    if drop_octets_val != 0:
                        drop_octets_non_zero = True
                except (ValueError, IndexError):
                    # Cannot parse - skip this line
                    continue
            
            # Record VOQ/Egress queue lines only if either column has non-zero value
            if drop_pkts_non_zero or drop_octets_non_zero:
                matched_lines.append(stripped)
    
    return header_line, matched_lines


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
        lines = blocks[0].lines
        header_line, matched_lines = _parse_queue_drops_output(lines)
        
        # Only report WARN if there are actual non-zero drop entries
        if matched_lines:
            # Store header and matched lines for debug output
            debug_info = []
            if header_line:
                debug_info.append(header_line)
            debug_info.extend(matched_lines)
            
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"Queue drops present in interface counters ({len(matched_lines)} non-zero entry/ies).",
                    details=debug_info,
                    command="show interfaces counters queue drops",
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
        lines = blocks[0].lines
        drop_pkts_col_idx = None
        drop_octets_col_idx = None
        header_line_idx = None
        
        # Find header line and DropPkts column index
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            # Handle both pipe-separated and space-separated headers
            if "|" in stripped:
                parts = [p.strip() for p in stripped.split("|") if p.strip()]
            else:
                parts = stripped.split()
            parts_lower = [p.lower() for p in parts]
            
            # Look for header containing DropPkts and DropOctets (exact match)
            # Check for exact "droppkts" and "dropoctets" to ensure we get the right column
            temp_drop_pkts_idx = None
            temp_drop_octets_idx = None
            
            for col_idx, col_name in enumerate(parts_lower):
                # Exact match for "droppkts" (case-insensitive)
                if col_name == "droppkts":
                    temp_drop_pkts_idx = col_idx
                # Also find DropOctets for validation
                if col_name == "dropoctets":
                    temp_drop_octets_idx = col_idx
            
            # Check if header has "CoPP" and "Class" as separate columns
            # If so, data rows will have one less column (CoPP Class merged)
            header_cols_adjustment = 0
            if "copp" in parts_lower and "class" in parts_lower:
                copp_idx = parts_lower.index("copp")
                class_idx = parts_lower.index("class")
                # If CoPP and Class are adjacent, data rows will merge them
                if class_idx == copp_idx + 1:
                    header_cols_adjustment = 1
            
            # Validate: DropPkts should come before DropOctets in standard format
            # If both found and DropPkts comes after DropOctets, the header might be reversed
            if temp_drop_pkts_idx is not None:
                # Adjust column index for data rows (subtract adjustment if CoPP/Class are merged)
                drop_pkts_col_idx = temp_drop_pkts_idx - header_cols_adjustment
                drop_octets_col_idx = temp_drop_octets_idx - header_cols_adjustment if temp_drop_octets_idx is not None else None
                header_line_idx = idx
                break
        
        if drop_pkts_col_idx is None:
            # Fallback: if no header found, return OK
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.OK,
                    summary="No DropPkts column found in output.",
                )
            ]
        
        # Parse data rows (after header)
        offenders = []
        start_idx = header_line_idx + 1 if header_line_idx is not None else 0
        
        for line in lines[start_idx:]:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip separator lines
            if stripped.replace("-", "").replace("|", "").strip() == "":
                continue
            
            # Parse line (handle both pipe-separated and space-separated)
            if "|" in stripped:
                parts = [p.strip() for p in stripped.split("|") if p.strip()]
            else:
                parts = stripped.split()
            
            # Check DropPkts column
            # Ensure we have enough columns and the DropPkts column exists
            if len(parts) > drop_pkts_col_idx:
                try:
                    drop_pkts_val_str = parts[drop_pkts_col_idx].replace(",", "").strip()
                    drop_pkts_val = int(drop_pkts_val_str)
                    # Only match if DropPkts > 1000000 (1 million)
                    # Note: We check DropPkts column specifically, not DropOctets
                    if drop_pkts_val > 1_000_000:
                        offenders.append((stripped, drop_pkts_val))
                except (ValueError, IndexError):
                    # Cannot parse - skip this line
                    continue
        
        if offenders:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"CPU queue drops exceed 1 million on {len(offenders)} entry(ies).",
                    details=[f"{ln} (DropPkts={v})" for ln, v in offenders],
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
        lines = blocks[0].lines
        
        # Find header line to determine column positions
        in_discards_col_idx = None
        out_discards_col_idx = None
        header_line_idx = None
        
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split()
            parts_lower = [p.lower() for p in parts]
            
            # Look for header containing InDiscards and/or OutDiscards
            if "indiscards" in parts_lower or "outdiscards" in parts_lower:
                try:
                    if "indiscards" in parts_lower:
                        in_discards_col_idx = parts_lower.index("indiscards")
                    if "outdiscards" in parts_lower:
                        out_discards_col_idx = parts_lower.index("outdiscards")
                    header_line_idx = idx
                    break
                except ValueError:
                    continue
        
        # Check for non-zero discards in data rows
        has_discards = False
        discard_lines = []
        
        if header_line_idx is not None:
            max_col_idx = max(
                in_discards_col_idx if in_discards_col_idx is not None else -1,
                out_discards_col_idx if out_discards_col_idx is not None else -1
            )
            
            for line in lines[header_line_idx + 1:]:
                stripped = line.strip()
                if not stripped:
                    continue
                # Skip separator lines (lines with only dashes or similar)
                if stripped.replace("-", "").replace(" ", "").strip() == "":
                    continue
                
                parts = stripped.split()
                if len(parts) <= max_col_idx:
                    continue
                
                # Check InDiscards column
                if in_discards_col_idx is not None and len(parts) > in_discards_col_idx:
                    try:
                        in_discards_val = int(parts[in_discards_col_idx].replace(",", ""))
                        if in_discards_val > 0:
                            has_discards = True
                            discard_lines.append(stripped)
                            continue
                    except (ValueError, IndexError):
                        pass
                
                # Check OutDiscards column
                if out_discards_col_idx is not None and len(parts) > out_discards_col_idx:
                    try:
                        out_discards_val = int(parts[out_discards_col_idx].replace(",", ""))
                        if out_discards_val > 0:
                            has_discards = True
                            discard_lines.append(stripped)
                    except (ValueError, IndexError):
                        pass
        
        if has_discards:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"Interface discards present ({len(discard_lines)} interface(s) with non-zero discards).",
                    details=discard_lines,
                    command="show interfaces counters discards",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No interface discards present.",
                command="show interfaces counters discards",
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
        lines = blocks[0].lines
        
        # Find header line to determine column positions
        # Error counter columns: FCS, Align, Symbol, Rx, Runts, Giants, Tx
        error_column_names = ["fcs", "align", "symbol", "rx", "runts", "giants", "tx"]
        error_col_indices = {}
        header_line_idx = None
        
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split()
            parts_lower = [p.lower() for p in parts]
            
            # Look for header containing error counter column names
            found_columns = []
            for col_name in error_column_names:
                if col_name in parts_lower:
                    found_columns.append(col_name)
                    error_col_indices[col_name] = parts_lower.index(col_name)
            
            # If we found at least one error counter column, consider this the header
            if found_columns:
                header_line_idx = idx
                break
        
        # Check for non-zero errors in data rows
        has_errors = False
        error_lines = []
        
        if header_line_idx is not None:
            max_col_idx = max(error_col_indices.values()) if error_col_indices else -1
            
            for line in lines[header_line_idx + 1:]:
                stripped = line.strip()
                if not stripped:
                    continue
                # Skip separator lines (lines with only dashes or similar)
                if stripped.replace("-", "").replace(" ", "").strip() == "":
                    continue
                # Skip the "(No non-zero error counters found)" message line
                if "no non-zero" in stripped.lower() or "no error" in stripped.lower():
                    continue
                
                parts = stripped.split()
                if len(parts) <= max_col_idx:
                    continue
                
                # Check all error counter columns
                line_has_error = False
                for col_name, col_idx in error_col_indices.items():
                    if len(parts) > col_idx:
                        try:
                            error_val = int(parts[col_idx].replace(",", ""))
                            if error_val > 0:
                                line_has_error = True
                                break
                        except (ValueError, IndexError):
                            pass
                
                if line_has_error:
                    has_errors = True
                    error_lines.append(stripped)
        
        if has_errors:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"Interface error counters present ({len(error_lines)} interface(s) with non-zero errors).",
                    details=error_lines,
                    command="show interfaces counters errors",
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No interface error counters present.",
                command="show interfaces counters errors",
            )
        ]


@register_check
class HardwareCounterDropCheck(BaseCheck):
    name = "hardware_counter_drop"
    category = "hardware"
    supported_platforms = ("78xx", "75xx", "7289", "7388")

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

        # Try to parse date from show clock
        clock = ctx.system_time
        drop_same_day = False
        has_adverse_drops = False
        has_congestion_drops = False
        adverse_row_count = 0
        congestion_row_count = 0
        
        try:
            # Example: Thu Jan 29 23:10:00 2026
            dt_clock = _dt.datetime.strptime(clock, "%a %b %d %H:%M:%S %Y")
            date_clock = dt_clock.date()
            
            # Check Summary section for total counts
            summary_match_a = re.search(r"Total\s+Adverse\s*\(A\)\s*Drops:\s*(\d+)", text, re.IGNORECASE)
            summary_match_c = re.search(r"Total\s+Congestion\s*\(C\)\s*Drops:\s*(\d+)", text, re.IGNORECASE)
            
            if summary_match_a:
                try:
                    adverse_count = int(summary_match_a.group(1))
                    has_adverse_drops = adverse_count > 0
                except (ValueError, IndexError):
                    pass
            
            if summary_match_c:
                try:
                    congestion_count = int(summary_match_c.group(1))
                    has_congestion_drops = congestion_count > 0
                except (ValueError, IndexError):
                    pass
            
            # Check data rows for A or C type drops with Last Occurrence on same day
            # Data row format: Type  Chip         CounterName  :  Count : First Occurrence : Last Occurrence
            # Example: A     Jericho4/2   DeqDeletePktCnt :  28 : 2026-01-07 15:53:42 : 2026-01-07 15:53:56
            for line in blocks[0].lines:
                stripped = line.strip()
                if not stripped:
                    continue
                # Skip header and separator lines
                if "Last Occurrence" in stripped or stripped.replace("-", "").replace("|", "").strip() == "":
                    continue
                
                # Check if line starts with A or C (Adverse or Congestion type)
                if stripped.startswith("A "):
                    adverse_row_count += 1
                    # Parse the line to extract Last Occurrence date
                    # Format: A     Chip   CounterName : Count : FirstOccurrence : LastOccurrence
                    # Last Occurrence is typically the last date/time field
                    parts = stripped.split()
                    if len(parts) >= 6:
                        # Try to find date pattern in the line (YYYY-MM-DD HH:MM:SS)
                        # Look for pattern matching date format
                        date_pattern = r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
                        matches = re.findall(date_pattern, stripped)
                        if matches:
                            # Last match should be Last Occurrence
                            last_occurrence_str = matches[-1]
                            try:
                                dt_last = _dt.datetime.strptime(last_occurrence_str, "%Y-%m-%d %H:%M:%S")
                                if dt_last.date() == date_clock:
                                    drop_same_day = True
                                    # Don't break, continue counting all rows
                            except (ValueError, Exception):
                                continue
                elif stripped.startswith("C "):
                    congestion_row_count += 1
                    # Parse the line to extract Last Occurrence date
                    parts = stripped.split()
                    if len(parts) >= 6:
                        date_pattern = r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
                        matches = re.findall(date_pattern, stripped)
                        if matches:
                            last_occurrence_str = matches[-1]
                            try:
                                dt_last = _dt.datetime.strptime(last_occurrence_str, "%Y-%m-%d %H:%M:%S")
                                if dt_last.date() == date_clock:
                                    drop_same_day = True
                                    # Don't break, continue counting all rows
                            except (ValueError, Exception):
                                continue
        except Exception as e:
            LOG.debug(f"Failed to parse show clock time for hardware counter drop comparison: {e}")

        # Alert if we have A or C drops AND at least one has Last Occurrence on same day
        if drop_same_day and (has_adverse_drops or has_congestion_drops):
            summary_parts = []
            if has_adverse_drops:
                summary_parts.append(f"A drops: {adverse_row_count} row(s)")
            if has_congestion_drops:
                summary_parts.append(f"C drops: {congestion_row_count} row(s)")
            summary_suffix = f" ({', '.join(summary_parts)})" if summary_parts else ""
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"Adverse (A) or Congestion (C) drops occurred on the same day as show clock.{summary_suffix}",
                )
            ]
        
        # Build OK summary with row counts
        summary_parts = []
        if adverse_row_count > 0:
            summary_parts.append(f"A drops: {adverse_row_count} row(s)")
        if congestion_row_count > 0:
            summary_parts.append(f"C drops: {congestion_row_count} row(s)")
        summary_suffix = f" ({', '.join(summary_parts)})" if summary_parts else ""
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary=f"No Adverse/Congestion drops with last occurrence on current day.{summary_suffix}",
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
        
        # Expected format: table with "Device Type", "Health Metric", "Value" columns
        # Example:
        # Device Type  Health Metric  Value
        # ------ ----- ------------- ------
        # flash: SMART Health status FAILED
        
        # Find header line to identify where data starts
        header_line_idx = None
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            stripped_lower = stripped.lower()
            # Look for header line containing "device type", "health metric", and "value"
            if "device" in stripped_lower and "type" in stripped_lower and \
               "health" in stripped_lower and "metric" in stripped_lower and \
               "value" in stripped_lower:
                header_line_idx = idx
                break
        
        # Parse data rows after header
        if header_line_idx is not None:
            start_idx = header_line_idx + 1
            while start_idx < len(lines):
                line = lines[start_idx]
                stripped = line.strip()
                if not stripped:
                    start_idx += 1
                    continue
                # Skip separator lines (lines with only dashes)
                if stripped.replace("-", "").replace("|", "").strip() == "":
                    start_idx += 1
                    continue
                # Skip lines that look like headers
                if "device" in stripped.lower() and "type" in stripped.lower() and \
                   "health" in stripped.lower() and "metric" in stripped.lower():
                    start_idx += 1
                    continue
                
                # Parse data row
                # Check the entire line for status and lifetime information
                line_lower = stripped.lower()
                
                # Check for Status: look for "status" keyword and check if value is not "ok"
                if "status" in line_lower:
                    # Extract the status value
                    # Pattern: "... status VALUE" or "... status: VALUE"
                    # Try regex first to handle "status:" pattern
                    status_match = re.search(r"status\s*:?\s*(\S+)", line_lower)
                    if status_match:
                        status_value = status_match.group(1).strip()
                        if status_value.lower() != "ok":
                            bad_status.append(stripped)
                    else:
                        # Fallback: use last token as value
                        parts = stripped.split()
                        if parts:
                            value_str = parts[-1].strip()
                            if value_str.lower() != "ok":
                                bad_status.append(stripped)
                
                # Check for Lifetime remaining: look for "lifetime" and "remaining" keywords
                if "lifetime" in line_lower and "remaining" in line_lower:
                    # Extract percentage value from the line
                    lifetime_match = re.search(r"(\d+)\s*%", stripped)
                    if lifetime_match:
                        try:
                            lifetime_val = int(lifetime_match.group(1))
                            if lifetime_val < 10:
                                low_lifetime.append(stripped)
                        except (ValueError, IndexError):
                            pass
                
                start_idx += 1
        else:
            # Fallback: parse non-table format
            # Look for "Status:" and "Lifetime remaining:" patterns
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue
                
                # Check for Status (case-insensitive, flexible format)
                status_match = re.search(r"Status\s*:\s*(\S+)", stripped, re.IGNORECASE)
                if status_match:
                    status_value = status_match.group(1).strip()
                    if status_value.lower() != "ok":
                        bad_status.append(stripped)
                
                # Check for Lifetime remaining (flexible format)
                if "lifetime" in stripped.lower() and "remaining" in stripped.lower():
                    lifetime_match = re.search(r"(\d+)\s*%", stripped)
                    if lifetime_match:
                        try:
                            lifetime_val = int(lifetime_match.group(1))
                            if lifetime_val < 10:
                                low_lifetime.append(stripped)
                        except (ValueError, IndexError):
                            continue
        
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
        
        _maybe_add_debug_raw(details, "show system health storage", lines)
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
        
        # Find header line with "FPGA" and "Errors" to determine column position
        errors_col_start = None
        errors_col_end = None
        
        for i, line in enumerate(lines):
            # Look for header line containing "FPGA" and "Errors"
            if "FPGA" in line and "Errors" in line:
                # Find the position of "Errors" word
                errors_idx = line.find("Errors")
                if errors_idx != -1:
                    # The Errors column starts at the beginning of "Errors" word
                    errors_col_start = errors_idx
                    # Find the end position by looking for the next column header
                    # "First Occurrence" comes after "Errors"
                    first_occurrence_idx = line.find("First Occurrence", errors_idx)
                    if first_occurrence_idx != -1:
                        errors_col_end = first_occurrence_idx
                    else:
                        # Fallback: assume Errors column ends at "Last Occurrence"
                        last_occurrence_idx = line.find("Last Occurrence", errors_idx)
                        if last_occurrence_idx != -1:
                            errors_col_end = last_occurrence_idx
                        else:
                            # Last resort: assume Errors column is about 12 characters wide
                            errors_col_end = errors_idx + 12
                    break
        
        # If we found the header, parse data rows
        if errors_col_start is not None and errors_col_end is not None:
            for line in lines:
                # Skip header lines, separator lines, section headers, and empty lines
                if (
                    "FPGA" in line and "Errors" in line  # Header line
                    or "---" in line  # Separator line
                    or "Action:" in line  # Action line
                    or "Uncorrected" in line or "Corrected" in line or "Software-repaired" in line  # Section headers
                    or not line.strip()  # Empty line
                ):
                    continue
                
                # Extract the Errors column value using fixed-width parsing
                if len(line) > errors_col_start:
                    # Get the Errors column substring (from Errors start to First Occurrence start)
                    errors_col_str = line[errors_col_start:errors_col_end].strip()
                    
                    # Extract the first number from this column (handles right-aligned numbers)
                    # The number might be right-aligned, so we need to find it
                    number_match = re.search(r'\d+', errors_col_str)
                    if number_match:
                        try:
                            error_count = int(number_match.group(0))
                            if error_count > 0:
                                offenders.append(line.strip())
                        except ValueError:
                            # Not a valid number, skip this line
                            continue
        
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
    supported_platforms = ("7368", "7289", "7388")

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


# Platform-specific configurable patterns for running-config checks
# To add/modify/delete patterns, simply edit the corresponding platform list without changing the core logic
# Format: {platform_series: [list of patterns to check]}
RUNNING_CONFIG_PATTERNS_BY_PLATFORM = {
    "78xx": [
        "ip hardware fib next-hop arp dedicated",
        # Add more patterns for 78xx here as needed
        # Example: "another pattern to check for 78xx",
    ],
    "75xx": [
        # Add patterns for 75xx here as needed
        # Example: "pattern for 75xx",
    ],
    "7368": [
        # Add patterns for 7368 here as needed
        # Example: "pattern for 7368",
    ],
    "7289": [
        # Add patterns for 7289 here as needed
        # Example: "pattern for 7289",
    ],
    "7388": [
        # Add patterns for 7388 here as needed
        # Example: "pattern for 7388",
    ],
    # Add more platforms as needed
    # "other": [
    #     "pattern for other platforms",
    # ],
}


@register_check
class RunningConfigCheck(BaseCheck):
    name = "running_config_check"
    category = "config"
    supported_platforms = ("all",)  # Support all platforms, but check patterns based on detected platform

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
        
        # Get patterns for the detected platform
        platform_series = ctx.platform_series
        patterns = RUNNING_CONFIG_PATTERNS_BY_PLATFORM.get(platform_series, [])
        
        # If no patterns configured for this platform, skip the check
        if not patterns:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary=f"No configuration patterns configured for platform {platform_series}.",
                )
            ]
        
        lines = blocks[0].lines
        matched_lines = []
        matched_patterns = []
        
        # Check each pattern against all lines
        for pattern in patterns:
            for line in lines:
                if pattern in line:
                    matched_lines.append(line.strip())
                    if pattern not in matched_patterns:
                        matched_patterns.append(pattern)
                    break  # Only record first match per pattern
        
        if matched_patterns:
            # Store matched lines in details for debug output
            details = matched_lines.copy()
            pattern_list = ", ".join(f"'{p}'" for p in matched_patterns)
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"Found matching configuration pattern(s) on {platform_series}: {pattern_list}.",
                    details=details,
                )
            ]
        
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary=f"No matching configuration patterns found on {platform_series}.",
            )
        ]


@register_check
class InventoryCheck(BaseCheck):
    name = "inventory"
    category = "hardware"
    supported_platforms = ("all",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show inventory")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show inventory output not found.",
                )
            ]
        
        # Record all inventory information
        lines = blocks[0].lines
        # Filter out empty lines and command delimiters
        inventory_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("---"):
                inventory_lines.append(stripped)
        
        if inventory_lines:
            summary = f"Found inventory information ({len(inventory_lines)} line(s))."
            # Include all inventory lines in details
            details = inventory_lines
        else:
            summary = "show inventory output is empty."
            details = []
        
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.INFO,
                summary=summary,
                details=details,
            )
        ]


def run_all_checks(ctx: TechSupportContext, skip_checks: Optional[List[str]] = None, skip_categories: Optional[List[str]] = None) -> List[CheckResult]:
    results: List[CheckResult] = []
    skip_set = set(skip_checks) if skip_checks else set()
    skip_categories_set = set(skip_categories) if skip_categories else set()
    # Core info parsers (populate context)
    results.extend(parse_show_version(ctx))
    results.extend(parse_show_clock(ctx))
    # Fallback hostname from running-config if not present in show version
    populate_hostname_from_running_config(ctx)
    # Platform series based on parsed model for later checks
    ctx.platform_series = infer_platform_series(ctx.hw_model)
    LOG.debug("Detected platform series: %s (from model: %s)", ctx.platform_series, ctx.hw_model)
    # Run registered checks based on platform
    for check in REGISTERED_CHECKS:
        # Skip if category is excluded
        if check.category in skip_categories_set:
            LOG.debug("Skipping check %s (category %s is excluded)", check.name, check.category)
            continue
        # Skip if explicitly requested
        if check.name in skip_set:
            LOG.debug("Skipping check %s (explicitly excluded)", check.name)
            continue
        if not platform_supported(check, ctx.platform_series):
            LOG.debug(
                "Skipping check %s for platform %s", check.name, ctx.platform_series
            )
            continue
        try:
            check_results = check.run(ctx) or []
            LOG.debug("Check %s returned %d result(s)", check.name, len(check_results))
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


def format_checks_list() -> str:
    """Format a list of all registered checks."""
    lines: List[str] = []
    lines.append("Supported Health Checks:")
    lines.append("=" * 80)
    
    # Group checks by category
    checks_by_category: Dict[str, List[BaseCheck]] = {}
    for check in REGISTERED_CHECKS:
        category = check.category
        if category not in checks_by_category:
            checks_by_category[category] = []
        checks_by_category[category].append(check)
    
    # Sort categories
    sorted_categories = sorted(checks_by_category.keys())
    
    for category in sorted_categories:
        lines.append("")
        lines.append(f"Category: {category}")
        lines.append("-" * 80)
        
        checks = checks_by_category[category]
        # Sort checks by name
        checks.sort(key=lambda c: c.name)
        
        headers = ["Check Name", "Command", "Supported Platforms"]
        rows: List[List[str]] = []
        
        for check in checks:
            cmd = _infer_command_from_check(
                CheckResult(name=check.name, category=check.category, severity=Severity.OK, summary="")
            ) or "N/A"
            platforms = ", ".join(check.supported_platforms) if check.supported_platforms else "all"
            rows.append([check.name, cmd, platforms])
        
        # Compute column widths
        widths = _compute_col_widths(headers, rows)
        lines.extend(_ascii_table_with_widths(headers, rows, widths))
    
    lines.append("")
    lines.append("=" * 80)
    return "\n".join(lines)


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


def _infer_command_from_check(check: CheckResult) -> Optional[str]:
    """Infer command name from check name/category for debug output."""
    # Map check names to their corresponding commands
    name_to_cmd = {
        "cooling_status": "show system env cooling",
        "temperature_status": "show system env temperature",
        "core_dump_files": "bash ls -ltr /var/core",
        "flash_usage": "bash df -h",
        "extensions_detail": "show extensions detail",
        "cpu_usage_top": "show processes top once",
        "memory_usage_top": "show processes top memory once",
        "module_uptime": "show module",
        "platform_sand_health": "show platform sand health",
        "fap_fabric_serdes": "show platform fap fabric detail",
        "redundancy_status": "show redundancy status",
        "pci_errors": "show pci",
        "agent_crash_logs": "show agent logs crash",
        "power_input_voltage": "show system environment power detail",
        "logging_threshold_errors": "show logging threshold errors",
        "interfaces_queue_drops": "show interfaces counters queue drops",
        "cpu_queue_drops": "show cpu counters queue",
        "interfaces_discards": "show interfaces counters discards",
        "interfaces_errors": "show interfaces counters errors",
        "hardware_counter_drop": "show hardware counter drop",
        "hardware_capacity": "show hardware capacity",
        "system_health_storage": "show system health storage",
        "hardware_fpga_error": "show hardware fpga error",
        "scd_satellite_retry_error": "show platform scd satellite debug",
        "running_config_check": "show running-config sanitized",
        "inventory": "show inventory",
    }
    return name_to_cmd.get(check.name)


def format_human_report(
    ctx: TechSupportContext,
    brief: DeviceBrief,
    results: Sequence[CheckResult],
    mode: str,
    debug: bool = False,
    show_checks_in_brief: Optional[List[str]] = None,
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
            f"{brief.health.value} (WARN={brief.warn_count})",
            "",
        ],
    ]

    # Brief table is always the same regardless of mode
    widths = _compute_col_widths(headers, brief_rows)
    lines.extend(_ascii_table_with_widths(headers, brief_rows, widths))
    
    # Add checks information in brief mode if requested
    if mode == "brief" and show_checks_in_brief is not None:
        lines.append("")
        if len(show_checks_in_brief) == 0:
            # No check names specified: show all supported checks list
            lines.append(format_checks_list())
        else:
            # Show specified checks details
            lines.append("Selected Checks Details:")
            lines.append("=" * 80)
            for check_name in show_checks_in_brief:
                # Find all matching check results (exact match or prefix match)
                # This handles cases where a check returns multiple results with suffixes
                # (e.g., redundancy_status -> redundancy_status_active_unit, redundancy_status_protocol)
                matching_results = []
                for r in results:
                    if r.name == check_name or r.name.startswith(check_name + "_"):
                        matching_results.append(r)
                
                if matching_results:
                    for check_result in matching_results:
                        lines.append("")
                        lines.append(f"Check: {check_result.category}/{check_result.name}")
                        lines.append(f"Status: {check_result.severity.value}")
                        lines.append(f"Summary: {check_result.summary}")
                        if check_result.details:
                            lines.append("Details:")
                            for detail in check_result.details[:5]:  # Limit to first 5 details
                                if not detail.startswith("[DEBUG"):
                                    lines.append(f"  {detail}")
                            if len(check_result.details) > 5:
                                lines.append(f"  ... and {len(check_result.details) - 5} more item(s)")
                        lines.append("-" * 80)
                else:
                    lines.append("")
                    lines.append(f"Check: {check_name} (not found or not executed)")
                    lines.append("-" * 80)
    
    # In brief mode without debug, return early
    # In brief mode with debug, continue to show debug output for selected checks
    if mode == "brief" and not debug:
        return "\n".join(lines)
    
    # If brief mode with debug, filter results to only selected checks
    # Then continue with normal debug output logic below
    if mode == "brief" and debug and show_checks_in_brief is not None and len(show_checks_in_brief) > 0:
        # Filter results to only selected checks (exact match or prefix match)
        # This handles cases where a check returns multiple results with suffixes
        selected_results = []
        for r in results:
            for check_name in show_checks_in_brief:
                if r.name == check_name or r.name.startswith(check_name + "_"):
                    selected_results.append(r)
                    break
        results = selected_results
        # Add separator before debug output (skip "Detailed checks:" header)
        lines.append("")
        lines.append("Debug output for selected checks:")
        lines.append("-" * 80)
    else:
        # verbose/debug/warn-only: detailed checks with horizontal separators
        lines.append("")
        if mode == "warn":
            lines.append("WARN checks:")
        else:
            lines.append("Detailed checks:")
        lines.append("-" * 80)

    # In warn-only mode, only keep WARN-severity results
    if mode == "warn":
        results = [r for r in results if r.severity == Severity.WARN]
    
    for r in results:
        # In brief mode with debug and selected checks, skip summary/details (already shown above)
        # Only show debug raw output
        if not (mode == "brief" and debug and show_checks_in_brief is not None and len(show_checks_in_brief) > 0):
            lines.append(f"[{r.severity.value}] {r.category}/{r.name}: {r.summary}")
            
            # In verbose mode, limit details to avoid excessive output
            # Show only summary and important lines (max 10 details)
            # Exception: inventory check should not show details in verbose mode
            if mode in ("verbose", "warn") and not debug:
                if r.name == "inventory":
                    # Skip details for inventory check in verbose mode
                    pass
                else:
                    max_details = 10
                    filtered_details = [d for d in r.details if not d.startswith("[DEBUG raw")]
                    if len(filtered_details) > max_details:
                        for d in filtered_details[:max_details]:
                            lines.append(f"  {d}")
                        lines.append(f"  ... and {len(filtered_details) - max_details} more item(s)")
                    else:
                        for d in filtered_details:
                            lines.append(f"  {d}")
            elif debug:
                # In debug mode, show all details (except legacy debug raw)
                for d in r.details:
                    if not d.startswith("[DEBUG raw"):
                        lines.append(f"  {d}")
        
        # In debug mode, output full raw command output (not truncated)
        # Exception: fap_fabric_serdes outputs filtered lines matching regex pattern
        if debug:
            cmd = r.command or _infer_command_from_check(r)
            if cmd:
                blocks = ctx.get_blocks(cmd)
                if blocks:
                    raw_lines = blocks[0].lines
                    lines.append("")
                    if r.name == "fap_fabric_serdes":
                        # Special case: output only lines matching the regex pattern
                        text = "\n".join(raw_lines)
                        if ctx.platform_series == "78xx":
                            # Pattern: U--- Ramon|---U Ramon|I--- Ramon|I---I Ramon|---I Ramon|\|--- Ramon|---\| Ramon
                            # Note: I---I Ramon is also a valid pattern (I---I followed by Ramon without space)
                            pattern = r"(U--- Ramon|[|]---U Ramon|I---I? Ramon|[|]---I Ramon|[|]--- Ramon|---[|] Ramon)"
                        else:
                            # Pattern: U--- Fe|---U Fe|I--- Fe|I---I Fe|---I Fe|\|--- Fe|---\| Fe
                            # Note: I---I Fe is also a valid pattern (I---I followed by Fe without space)
                            pattern = r"(U--- Fe|[|]---U Fe|I---I? Fe|[|]---I Fe|[|]--- Fe|---[|] Fe)"
                        
                        # Find matching lines (all lines in debug mode, no limit)
                        matching_lines = []
                        for line in raw_lines:
                            if re.search(pattern, line):
                                matching_lines.append(line)
                        
                        lines.append(f"[DEBUG filtered {cmd}]")
                        lines.append("-" * 80)
                        if matching_lines:
                            for line in matching_lines:
                                lines.append(line)
                        else:
                            lines.append("(No lines matched the pattern)")
                        lines.append("-" * 80)
                    elif r.name == "logging_threshold_errors":
                        # Special case: output only lines matching configured regex patterns
                        # Use shared patterns list (same as in LoggingThresholdErrorsCheck)
                        patterns = LOGGING_THRESHOLD_ERROR_PATTERNS
                        matching_lines = []
                        for line in raw_lines:
                            for pattern in patterns:
                                if re.search(pattern, line, re.IGNORECASE):
                                    matching_lines.append(line)
                                    break  # Only add line once
                        lines.append(f"[DEBUG filtered {cmd}]")
                        lines.append("-" * 80)
                        if matching_lines:
                            for line in matching_lines:
                                lines.append(line)
                        else:
                            lines.append("(No lines matched the patterns)")
                        lines.append("-" * 80)
                    elif r.name == "interfaces_queue_drops":
                        # Special case: output only header and non-zero drop lines
                        # Details already contain header + non-zero lines from check
                        if r.details:
                            lines.append(f"[DEBUG filtered {cmd}]")
                            lines.append("-" * 80)
                            for detail_line in r.details:
                                lines.append(detail_line)
                            lines.append("-" * 80)
                        else:
                            # Fallback: use shared parsing function
                            header_line, matched_lines = _parse_queue_drops_output(raw_lines)
                            lines.append(f"[DEBUG filtered {cmd}]")
                            lines.append("-" * 80)
                            if header_line:
                                lines.append(header_line)
                            if matched_lines:
                                for line in matched_lines:
                                    lines.append(line)
                            else:
                                lines.append("(No matched lines found)")
                            lines.append("-" * 80)
                    elif r.name == "interfaces_errors":
                        # Special case: output only header and lines with non-zero error counters
                        header_line = None
                        header_line_idx = None
                        non_zero_lines = []
                        
                        # Find header line
                        for idx, line in enumerate(raw_lines):
                            stripped = line.strip()
                            if not stripped:
                                continue
                            # Skip separator lines
                            if stripped.replace("-", "").replace("|", "").strip() == "":
                                continue
                            
                            # Check if this looks like a header (contains common error counter names)
                            parts = stripped.split()
                            parts_lower = [p.lower() for p in parts]
                            # Common error counter column names
                            error_keywords = ["error", "crc", "alignment", "fcs", "frame", "overrun", "underrun", "collision"]
                            if any(keyword in " ".join(parts_lower) for keyword in error_keywords):
                                header_line = stripped
                                header_line_idx = idx
                                break
                        
                        # Parse data rows (after header)
                        if header_line_idx is not None:
                            start_idx = header_line_idx + 1
                            for line in raw_lines[start_idx:]:
                                stripped = line.strip()
                                if not stripped:
                                    continue
                                # Skip separator lines
                                if stripped.replace("-", "").replace("|", "").strip() == "":
                                    continue
                                
                                parts = stripped.split()
                                # Check if any numeric column (after interface name) is non-zero
                                # Typically first column is interface name, rest are counters
                                has_non_zero = False
                                for i in range(1, len(parts)):  # Skip first column (interface name)
                                    try:
                                        val = int(parts[i].replace(",", "").strip())
                                        if val != 0:
                                            has_non_zero = True
                                            break
                                    except (ValueError, IndexError):
                                        continue
                                
                                if has_non_zero:
                                    non_zero_lines.append(stripped)
                        
                        lines.append(f"[DEBUG filtered {cmd}]")
                        lines.append("-" * 80)
                        if header_line:
                            lines.append(header_line)
                        if non_zero_lines:
                            for line in non_zero_lines:
                                lines.append(line)
                        else:
                            lines.append("(No non-zero error counters found)")
                        lines.append("-" * 80)
                    elif r.name == "hardware_counter_drop":
                        # Special case: output only A (Adverse) and C (Congestion) type drop lines
                        header_line = None
                        header_line_idx = None
                        filtered_lines = []
                        
                        # Find header line
                        for idx, line in enumerate(raw_lines):
                            stripped = line.strip()
                            if not stripped:
                                continue
                            # Skip separator lines
                            if stripped.replace("-", "").replace("|", "").strip() == "":
                                continue
                            
                            # Check if this looks like a header (contains "Last Occurrence")
                            if "Last Occurrence" in stripped:
                                header_line = stripped
                                header_line_idx = idx
                                break
                        
                        # Also include Summary section if present
                        summary_lines = []
                        for line in raw_lines:
                            stripped = line.strip()
                            if not stripped:
                                continue
                            # Include Summary section
                            if stripped.startswith("Summary:") or "Total Adverse" in stripped or "Total Congestion" in stripped:
                                summary_lines.append(stripped)
                        
                        # Parse data rows (after header)
                        if header_line_idx is not None:
                            start_idx = header_line_idx + 1
                            for line in raw_lines[start_idx:]:
                                stripped = line.strip()
                                if not stripped:
                                    continue
                                # Skip separator lines
                                if stripped.replace("-", "").replace("|", "").strip() == "":
                                    continue
                                
                                # Only include lines starting with A or C (Adverse or Congestion type)
                                if stripped.startswith("A ") or stripped.startswith("C "):
                                    filtered_lines.append(stripped)
                        
                        lines.append(f"[DEBUG filtered {cmd}]")
                        lines.append("-" * 80)
                        # Include Summary section if present
                        if summary_lines:
                            for line in summary_lines:
                                lines.append(line)
                            lines.append("")  # Empty line separator
                        if header_line:
                            lines.append(header_line)
                        if filtered_lines:
                            for line in filtered_lines:
                                lines.append(line)
                        else:
                            lines.append("(No A or C type drops found)")
                        lines.append("-" * 80)
                    elif r.name == "hardware_capacity":
                        # Special case: output only lines where "used entries" column is non-zero
                        # Handle multi-line fixed-width headers where "Used" and "Entries" are on different lines
                        header_lines = []
                        used_entries_char_pos = None
                        header_end_idx = None
                        filtered_lines = []
                        
                        # Find header lines and align "Used" from first line with "Entries" from second line
                        first_used_pos = None
                        first_used_line_idx = None
                        entries_line_idx = None
                        
                        # First pass: find the first line with "Used" and get its position
                        for idx, line in enumerate(raw_lines):
                            stripped = line.strip()
                            if not stripped:
                                continue
                            
                            parts_lower = [p.lower() for p in stripped.split()]
                            # Look for line with "Used" (first header line)
                            # Check for "Table" to identify the header line
                            if "used" in parts_lower and "table" in parts_lower:
                                # Find the first "Used" in the original line (not stripped)
                                # This is the "Used Entries" column (first "Used", not second)
                                used_pos = line.find("Used")
                                if used_pos >= 0:
                                    first_used_pos = used_pos
                                    first_used_line_idx = idx
                                    if stripped not in header_lines:
                                        header_lines.append(stripped)
                                    break
                        
                        # Second pass: find the line with "Entries" that aligns with first "Used"
                        # If we found first_used_pos, try to align; otherwise just use first "Entries"
                        for idx, line in enumerate(raw_lines):
                            stripped = line.strip()
                            if not stripped:
                                continue
                            
                            # Look for line with "Entries" (second header line)
                            # Use original line to find character positions
                            if "Entries" in line:
                                # Find all "Entries" positions in the original line
                                entries_positions = []
                                start = 0
                                while True:
                                    pos = line.find("Entries", start)
                                    if pos < 0:
                                        break
                                    entries_positions.append(pos)
                                    start = pos + 1
                                
                                if entries_positions:
                                    # If we have first_used_pos, try to align
                                    if first_used_pos is not None:
                                        # Find the "Entries" closest to first_used_pos (within reasonable range)
                                        # The first "Entries" should align with the first "Used"
                                        best_pos = None
                                        min_diff = float('inf')
                                        for pos in entries_positions:
                                            diff = abs(pos - first_used_pos)
                                            if diff < min_diff and diff <= 10:
                                                min_diff = diff
                                                best_pos = pos
                                        
                                        # Set the position (use best aligned or first as fallback)
                                        if best_pos is not None:
                                            used_entries_char_pos = best_pos
                                        else:
                                            # If no good alignment, use first "Entries" as fallback
                                            used_entries_char_pos = entries_positions[0]
                                    else:
                                        # If we didn't find first_used_pos, just use first "Entries"
                                        used_entries_char_pos = entries_positions[0]
                                    
                                    entries_line_idx = idx
                                    if stripped not in header_lines:
                                        header_lines.append(stripped)
                                    header_end_idx = idx
                                    break
                        
                        # Parse data rows (after header)
                        if header_end_idx is not None and used_entries_char_pos is not None:
                            # Find the separator line after header
                            start_idx = header_end_idx + 1
                            # Skip separator lines (lines with only dashes or empty)
                            while start_idx < len(raw_lines):
                                line = raw_lines[start_idx]
                                stripped = line.strip()
                                if not stripped:
                                    start_idx += 1
                                    continue
                                # Check if it's a separator line (mostly dashes)
                                if stripped.replace("-", "").replace("|", "").strip() == "":
                                    start_idx += 1
                                    continue
                                # Check if it looks like a data row (has alphanumeric content)
                                if re.search(r'[A-Za-z0-9]', stripped):
                                    break
                                start_idx += 1
                            
                            for line in raw_lines[start_idx:]:
                                # Use original line (not stripped) for fixed-width parsing
                                stripped = line.strip()
                                if not stripped:
                                    continue
                                # Skip separator lines
                                if stripped.replace("-", "").replace("|", "").strip() == "":
                                    continue
                                # Skip lines that look like headers (contain "Table", "Entries", etc.)
                                if any(keyword in stripped for keyword in ["Table", "Entries", "Feature", "Chip"]):
                                    continue
                                
                                # Extract value at the "Entries" column position
                                # Use fixed-width parsing: find the number at or near used_entries_char_pos
                                # Use original line to maintain character positions
                                if len(line) > used_entries_char_pos:
                                    # Extract a substring around the column position (allow some flexibility)
                                    start_pos = max(0, used_entries_char_pos - 5)
                                    end_pos = min(len(line), used_entries_char_pos + 20)
                                    col_substring = line[start_pos:end_pos].strip()
                                    
                                    # Try to find a number in this substring
                                    # Look for the first number in this region
                                    number_match = re.search(r'\b(\d+)\b', col_substring)
                                    if number_match:
                                        try:
                                            used_val = int(number_match.group(1))
                                            if used_val != 0:
                                                # Store stripped version for output
                                                filtered_lines.append(stripped)
                                        except (ValueError, IndexError):
                                            continue
                        
                        lines.append(f"[DEBUG filtered {cmd}]")
                        lines.append("-" * 80)
                        if header_lines:
                            for header_line in header_lines:
                                lines.append(header_line)
                        if filtered_lines:
                            for line in filtered_lines:
                                lines.append(line)
                        else:
                            lines.append("(No lines with non-zero used entries found)")
                        lines.append("-" * 80)
                    elif r.name == "running_config_check":
                        # Special case: output only lines matching configured patterns
                        # Use platform-specific patterns list (same as in RunningConfigCheck)
                        platform_series = ctx.platform_series
                        patterns = RUNNING_CONFIG_PATTERNS_BY_PLATFORM.get(platform_series, [])
                        matching_lines = []
                        for line in raw_lines:
                            for pattern in patterns:
                                if pattern in line:
                                    matching_lines.append(line)
                                    break  # Only add line once
                        lines.append(f"[DEBUG filtered {cmd}]")
                        lines.append("-" * 80)
                        if matching_lines:
                            for line in matching_lines:
                                lines.append(line)
                        else:
                            lines.append("(No lines matched the patterns)")
                        lines.append("-" * 80)
                    else:
                        # Normal case: output full raw
                        lines.append(f"[DEBUG raw {cmd}]")
                        lines.append("-" * 80)
                        for raw_line in raw_lines:
                            lines.append(raw_line)
                        lines.append("-" * 80)
        
        lines.append("-" * 80)

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


@dataclass
class ProcessingTask:
    """Represents a single file processing task."""
    source_id: str
    text: Optional[str] = None  # None in lazy-load mode
    mode: str = "brief"
    as_json: bool = False
    debug: bool = False
    show_checks_in_brief: Optional[List[str]] = None
    skip_checks: Optional[List[str]] = None
    skip_categories: Optional[List[str]] = None
    # Lazy-load fields (used when text is None)
    lazy_path: Optional[Path] = None  # For plain files or directories
    lazy_archive_path: Optional[Path] = None  # For archive members
    lazy_archive_spec: Optional[ArchiveShowTechMember] = None  # For archive members


def process_single_task(task: ProcessingTask) -> Tuple[str, str]:
    """
    Process a single file task and return (source_id, report).
    This function is designed to be called in parallel.
    Supports both pre-loaded text and lazy-loading modes.
    """
    text = task.text
    try:
        # Lazy-load text if needed
        if text is None:
            if task.lazy_path is not None:
                # Load from plain file
                text = task.lazy_path.read_text(encoding="utf-8", errors="replace")
            elif task.lazy_archive_path is not None and task.lazy_archive_spec is not None:
                # Load from archive member
                text = read_text_from_archive_member(task.lazy_archive_path, task.lazy_archive_spec)
            else:
                raise ValueError(f"Cannot lazy-load text for task {task.source_id}: missing lazy-load fields")
        
        report = process_showtech_text(
            task.source_id,
            text,
            task.mode,
            task.as_json,
            task.debug,
            task.show_checks_in_brief,
            task.skip_checks,
            task.skip_categories,
        )
        return (task.source_id, report)
    except Exception as exc:
        LOG.error("Error processing %s: %s", task.source_id, exc, exc_info=task.debug)
        error_msg = f"Error processing {task.source_id}: {exc}"
        return (task.source_id, error_msg)
    finally:
        # Release text reference immediately after processing (if lazy-loaded)
        if text is not None and task.text is None:
            del text
            gc.collect()


def collect_processing_tasks(
    paths: List[str],
    mode: str,
    as_json: bool,
    debug: bool,
    show_checks_in_brief: Optional[List[str]],
    skip_checks: Optional[List[str]],
    skip_categories: Optional[List[str]],
    low_memory: bool = False,
) -> List[ProcessingTask]:
    """
    Collect all file processing tasks from the given paths.
    Returns a list of ProcessingTask objects ready for parallel execution.
    
    Args:
        low_memory: If True, use lazy-loading mode (files loaded on-demand).
                    If False, pre-load all file contents into memory.
    """
    tasks: List[ProcessingTask] = []
    
    for path_str in paths:
        path = Path(path_str)
        if path.is_dir():
            # Unpacked support-bundle directory
            root = path
            LOG.info("Discovering show-tech files in directory: %s", root)
            files = discover_showtech_files_from_directory(root)
            if not files:
                LOG.warning("No show-tech files found under directory: %s", root)
                continue
            for f in files:
                LOG.info("Found show-tech file: %s", f)
                if low_memory:
                    # Lazy-load mode: store path, load on-demand
                    tasks.append(ProcessingTask(
                        source_id=str(f),
                        text=None,  # Will be loaded on-demand
                        mode=mode,
                        as_json=as_json,
                        debug=debug,
                        show_checks_in_brief=show_checks_in_brief,
                        skip_checks=skip_checks,
                        skip_categories=skip_categories,
                        lazy_path=f,
                    ))
                else:
                    # Pre-load mode: load now
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")
                        tasks.append(ProcessingTask(
                            source_id=str(f),
                            text=text,
                            mode=mode,
                            as_json=as_json,
                            debug=debug,
                            show_checks_in_brief=show_checks_in_brief,
                            skip_checks=skip_checks,
                            skip_categories=skip_categories,
                        ))
                    except OSError as exc:
                        LOG.error("Failed to read %s: %s", f, exc)
        elif path.is_file():
            # Decide if archive or plain show-tech file
            if zipfile.is_zipfile(path) or tarfile.is_tarfile(path):
                arch = path
                LOG.info("Discovering show-tech files in archive: %s", arch)
                members = discover_showtech_members_from_archive(arch)
                if not members:
                    LOG.warning("No show-tech files found in archive: %s", arch)
                    continue
                for spec in members:
                    LOG.info("Found show-tech member: %s!%s", arch, spec.display_name)
                    if low_memory:
                        # Lazy-load mode: store archive info, load on-demand
                        tasks.append(ProcessingTask(
                            source_id=f"{arch}!{spec.display_name}",
                            text=None,  # Will be loaded on-demand
                            mode=mode,
                            as_json=as_json,
                            debug=debug,
                            show_checks_in_brief=show_checks_in_brief,
                            skip_checks=skip_checks,
                            skip_categories=skip_categories,
                            lazy_archive_path=arch,
                            lazy_archive_spec=spec,
                        ))
                    else:
                        # Pre-load mode: load now
                        try:
                            text = read_text_from_archive_member(arch, spec)
                            tasks.append(ProcessingTask(
                                source_id=f"{arch}!{spec.display_name}",
                                text=text,
                                mode=mode,
                                as_json=as_json,
                                debug=debug,
                                show_checks_in_brief=show_checks_in_brief,
                                skip_checks=skip_checks,
                                skip_categories=skip_categories,
                            ))
                        except OSError as exc:
                            LOG.error(
                                "Failed to read member %s from archive %s: %s",
                                spec.display_name,
                                arch,
                                exc,
                            )
            else:
                # Plain show-tech file
                LOG.info("Found show-tech file: %s", path)
                if low_memory:
                    # Lazy-load mode: store path, load on-demand
                    tasks.append(ProcessingTask(
                        source_id=str(path),
                        text=None,  # Will be loaded on-demand
                        mode=mode,
                        as_json=as_json,
                        debug=debug,
                        show_checks_in_brief=show_checks_in_brief,
                        skip_checks=skip_checks,
                        skip_categories=skip_categories,
                        lazy_path=path,
                    ))
                else:
                    # Pre-load mode: load now
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        tasks.append(ProcessingTask(
                            source_id=str(path),
                            text=text,
                            mode=mode,
                            as_json=as_json,
                            debug=debug,
                            show_checks_in_brief=show_checks_in_brief,
                            skip_checks=skip_checks,
                            skip_categories=skip_categories,
                        ))
                    except OSError as exc:
                        LOG.error("Failed to read %s: %s", path, exc)
        else:
            LOG.error("Path does not exist or is not accessible: %s", path)
    
    return tasks


def process_showtech_text(source_id: str, text: str, mode: str, as_json: bool, debug: bool = False, show_checks_in_brief: Optional[List[str]] = None, skip_checks: Optional[List[str]] = None, skip_categories: Optional[List[str]] = None) -> str:
    # Load into memory, parse, then drop raw text reference
    parser = TechSupportParser()
    blocks = parser.parse(text)
    # Release parser and text references immediately after parsing
    del parser
    text = ""  # release raw text reference

    ctx = TechSupportContext(source_id, blocks)
    results = run_all_checks(ctx, skip_checks, skip_categories)
    brief = make_device_brief(ctx, results)

    # Generate report
    if as_json:
        report = format_json_report(ctx, brief, results, mode)
    else:
        report = format_human_report(ctx, brief, results, mode, debug, show_checks_in_brief)
    
    # Explicitly release large objects to help garbage collection
    # Note: Python's garbage collector will handle this, but explicit cleanup
    # helps ensure memory is freed promptly, especially when processing multiple files
    del blocks
    del ctx
    del results
    del brief
    
    # Force garbage collection for large objects
    gc.collect()
    
    return report


def main(argv: Optional[Sequence[str]] = None) -> None:
    import os as _os
    
    args = parse_args(argv)
    configure_logging(args.debug)

    # Handle --list-checks option
    if args.list_checks:
        print(format_checks_list())
        return

    # Validate that paths are provided when not using --list-checks
    if not args.paths:
        import sys
        print("error: the following arguments are required: PATH (unless using --list-checks)", file=sys.stderr)
        sys.exit(2)

    # Collect all processing tasks
    LOG.info("Collecting processing tasks from %d path(s)...", len(args.paths))
    low_memory = getattr(args, 'low_memory', False)
    tasks = collect_processing_tasks(
        args.paths,
        args.mode,
        args.json,
        args.debug,
        args.show_checks_in_brief,
        args.skip_checks,
        args.skip_categories,
        low_memory=low_memory,
    )
    
    if not tasks:
        LOG.warning("No show-tech files found to process.")
        return
    
    LOG.info("Found %d file(s) to process", len(tasks))
    if low_memory:
        LOG.info("Low-memory mode enabled: files will be loaded on-demand")
    
    # Determine number of threads
    num_threads = args.threads
    if num_threads is None:
        if low_memory:
            # In low-memory mode, use fewer threads to reduce memory pressure
            # Default to 2 threads or CPU count (whichever is smaller), capped at 4
            num_threads = min(_os.cpu_count() or 1, 4)
            if num_threads > 2:
                num_threads = 2
        else:
            # Default to number of CPU cores, but cap at 8 for memory efficiency
            num_threads = min(_os.cpu_count() or 1, 8)
    elif num_threads < 1:
        LOG.warning("Invalid thread count %d, using 1", num_threads)
        num_threads = 1
    
    # Process tasks in parallel or sequentially
    outputs: List[str] = []
    
    if num_threads == 1 or len(tasks) == 1:
        # Sequential processing (single thread or single task)
        LOG.info("Processing %d file(s) sequentially...", len(tasks))
        for task in tasks:
            LOG.info("Processing: %s", task.source_id)
            source_id, report = process_single_task(task)
            outputs.append(report)
            # Release task text reference immediately after processing
            del task.text
            gc.collect()
        
        # Clean up tasks list after sequential processing
        del tasks
        gc.collect()
    else:
        # Parallel processing with thread pool
        if low_memory and len(tasks) > num_threads * 2:
            # In low-memory mode with many tasks, process in batches
            # to avoid loading too many files simultaneously
            batch_size = num_threads * 2  # Process 2x thread count at a time
            LOG.info("Processing %d file(s) using %d thread(s) in batches of %d...", 
                     len(tasks), num_threads, batch_size)
            
            for batch_start in range(0, len(tasks), batch_size):
                batch_end = min(batch_start + batch_size, len(tasks))
                batch_tasks = tasks[batch_start:batch_end]
                LOG.info("Processing batch %d-%d of %d...", batch_start + 1, batch_end, len(tasks))
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                    # Submit batch tasks
                    future_to_index = {}
                    for idx, task in enumerate(batch_tasks):
                        future = executor.submit(process_single_task, task)
                        future_to_index[future] = idx
                    
                    # Collect results in submission order
                    batch_results: List[Optional[str]] = [None] * len(batch_tasks)
                    for future in concurrent.futures.as_completed(future_to_index.keys()):
                        idx = future_to_index[future]
                        task = batch_tasks[idx]
                        try:
                            source_id, report = future.result()
                            batch_results[idx] = report
                            LOG.info("Completed: %s", source_id)
                        except Exception as exc:
                            LOG.error("Task %s raised an exception: %s", task.source_id, exc, exc_info=args.debug)
                            batch_results[idx] = f"Error processing {task.source_id}: {exc}"
                        finally:
                            # Release task text reference immediately after processing
                            if task.text is not None:
                                del task.text
                    
                    # Add batch results to outputs
                    outputs.extend(r for r in batch_results if r is not None)
                    
                    # Clean up batch
                    del batch_results
                    del future_to_index
                    gc.collect()
            
            # Clean up tasks list after all batches complete
            del tasks
            gc.collect()
        else:
            # Standard parallel processing (all tasks at once)
            LOG.info("Processing %d file(s) using %d thread(s)...", len(tasks), num_threads)
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                # Submit all tasks and maintain order
                future_to_index = {}
                for idx, task in enumerate(tasks):
                    future = executor.submit(process_single_task, task)
                    future_to_index[future] = idx
                
                # Collect results in submission order
                results: List[Optional[str]] = [None] * len(tasks)
                completed_futures = []
                for future in concurrent.futures.as_completed(future_to_index.keys()):
                    idx = future_to_index[future]
                    task = tasks[idx]
                    try:
                        source_id, report = future.result()
                        results[idx] = report
                        LOG.info("Completed: %s", source_id)
                    except Exception as exc:
                        LOG.error("Task %s raised an exception: %s", task.source_id, exc, exc_info=args.debug)
                        results[idx] = f"Error processing {task.source_id}: {exc}"
                    finally:
                        # Release task text reference immediately after processing
                        if task.text is not None:
                            del task.text
                        completed_futures.append(future)
                
                # Add results in order
                outputs.extend(r for r in results if r is not None)
                
                # Clean up: release completed futures, results, and future_to_index
                del completed_futures
                del results
                del future_to_index
                # Note: tasks list will be cleaned up after the with block
            
            # Clean up tasks list after thread pool closes
            del tasks
            # Force garbage collection after all tasks complete
            gc.collect()

    final_output = "\n\n".join(outputs)
    
    # Release outputs list after creating final_output
    del outputs
    gc.collect()

    if args.output:
        out_path = Path(args.output)
        try:
            out_path.write_text(final_output, encoding="utf-8")
            LOG.info("Report written to: %s", out_path)
            # Release final_output after writing to file
            del final_output
            gc.collect()
        except OSError as exc:
            LOG.error("Failed to write output to %s: %s", out_path, exc)
            print(final_output)
            # final_output will be released when function exits
    else:
        print(final_output)
        # final_output will be released when function exits


if __name__ == "__main__":
    main()


