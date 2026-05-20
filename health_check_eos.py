#!/usr/bin/env python3
"""
Arista EOS support-bundle / show-tech health check tool.

Author : chris.li@arista.com
Date   : 2026-05-20

This script analyses EOS show-tech / show-tech-support-all outputs
and related support-bundle archives/directories and generates a
health report in brief or verbose form. Also supports `--live` to
collect commands directly from a device over eAPI or SSH.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import gc
import gzip
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
import re
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__author__ = "chris.li@arista.com"
__last_modified__ = "2026-05-20"
__version__ = "1.4.0"


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
            "  %(prog)s -V /path/to/show-tech                 # Verbose mode\n"
            "  %(prog)s -d /path/to/show-tech                # Debug mode\n"
            "  %(prog)s -j -o report.json /path/to/show-tech  # JSON output\n"
            "  %(prog)s -l                                   # List all checks\n"
            "  %(prog)s -c memory_usage_top /path/to/show-tech  # Show specific check\n"
            "  %(prog)s -s memory_usage_top /path/to/show-tech  # Skip specific check\n"
            "  %(prog)s -S hardware /path/to/show-tech       # Skip entire category\n"
            "  %(prog)s -s cpu_usage_top -S hardware /path/to/show-tech  # Combine options\n"
            "  %(prog)s -t 4 *.zip                           # Process archives with 4 threads\n"
            "  %(prog)s -t 1 /path/to/show-tech              # Disable parallel processing\n"
            "  %(prog)s -L /path/to/show-tech                # List command sections in show-tech\n"
            "  %(prog)s -r \"show version\" /path/to/show-tech # Dump raw output of one section\n"
            "  %(prog)s --cli /path/to/show-tech             # Interactive CLI over show-tech sections\n"
            "  %(prog)s --live 10.0.0.1 -u admin --insecure  # Live: connect via eAPI, run checks\n"
            "  %(prog)s --live --inventory hosts.yaml -t 8   # Live: batch from inventory file\n"
            "  %(prog)s --live 10.0.0.1 -u admin --save out/ # Live: also save show-tech-style file\n"
            "\n"
            "Author  : %(author)s\n"
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
            "Not required only when using --list-checks (-L / -r / --cli require PATH)."
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
        "-V",
        "--verbose",
        action="store_true",
        help="Verbose report mode (includes all check details).",
    )
    output_group.add_argument(
        "-v",
        "--summary",
        action="store_true",
        help="Summary report mode: one-line output for all checks (no details).",
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
    showtech_extract = debug_group.add_mutually_exclusive_group()
    showtech_extract.add_argument(
        "-L",
        "--list-showtech-commands",
        action="store_true",
        help=(
            "List command section headers from the show-tech input(s) in file order and exit. "
            "Requires PATH. Does not run health checks."
        ),
    )
    showtech_extract.add_argument(
        "-r",
        "--raw",
        dest="raw_command",
        metavar="COMMAND",
        default=None,
        help=(
            "Dump the raw body of COMMAND from the show-tech input(s) and exit. "
            "Matching is case-insensitive: exact command first, else prefix. "
            "Quote multi-word commands. Requires PATH. Does not run health checks."
        ),
    )
    showtech_extract.add_argument(
        "--cli",
        action="store_true",
        help=(
            "Interactive CLI over the first show-tech found under PATH: EOS-style abbreviated "
            "commands, '?' for next-level keywords, paged output, '| grep' / '| include', "
            "command history with arrow Up/Down on POSIX TTY; ASCII '?' ends input. "
            "Prompt uses hostname from config (running-config) when available. "
            "Does not run health checks."
        ),
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

    live_group = parser.add_argument_group("live device")
    live_group.add_argument(
        "--live",
        action="store_true",
        help=(
            "Live mode: treat PATH arguments as device hostnames/IPs and collect "
            "the commands each check needs directly from the device via eAPI "
            "(HTTPS/JSON-RPC) or SSH instead of reading show-tech files."
        ),
    )
    live_group.add_argument(
        "--inventory",
        metavar="FILE",
        help=(
            "Inventory file (JSON or YAML) listing devices for batch live mode. "
            "Each entry: {host, user?, password?, port?, transport?}. "
            "Entries combine with any hostnames given as PATH."
        ),
    )
    live_group.add_argument(
        "-u",
        "--user",
        metavar="USER",
        help="Default username for live device login (overridden per inventory entry).",
    )
    live_group.add_argument(
        "--password",
        metavar="PASS",
        help=(
            "Default password for live device login. Precedence: CLI > env EOS_PASSWORD "
            "> inventory > interactive prompt. Avoid passing on the command line in shared shells."
        ),
    )
    live_group.add_argument(
        "--port",
        type=int,
        metavar="N",
        help="eAPI port (defaults: 443 for HTTPS, 80 for --http). SSH always uses 22.",
    )
    live_group.add_argument(
        "--http",
        action="store_true",
        help="Use unencrypted HTTP for eAPI instead of HTTPS.",
    )
    live_group.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification for eAPI (common with self-signed certs).",
    )
    live_group.add_argument(
        "--transport",
        choices=("auto", "eapi", "ssh"),
        default="auto",
        help=(
            "Transport for live collection. Default 'auto' tries eAPI first then falls "
            "back to SSH. 'eapi' or 'ssh' force a single transport."
        ),
    )
    live_group.add_argument(
        "-T",
        "--use-tech-support",
        action="store_true",
        help=(
            "In live mode, run 'show tech-support all' on the device instead of the "
            "per-check command set. Slower but tolerant of unknown command variants."
        ),
    )
    live_group.add_argument(
        "--save",
        metavar="DIR",
        help=(
            "After collecting commands from a live device, write the assembled "
            "show-tech-style text to DIR/<host>-show-tech-<timestamp>.txt for "
            "offline re-analysis."
        ),
    )

    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = build_arg_parser()
    meta = {
        "prog": parser.prog,
        "author": __author__,
        "version": __version__,
        "last": __last_modified__,
    }
    if parser.description:
        parser.description = parser.description % meta  # type: ignore[operator]
    if parser.epilog:
        parser.epilog = parser.epilog % meta  # type: ignore[operator]
    args = parser.parse_args(argv)

    # Resolve output mode:
    # - summary overrides other modes
    # - verbose overrides other modes
    # - warn-only shows brief summary plus all WARN-severity checks
    # - default is brief
    if getattr(args, "summary", False):
        args.mode = "summary"
    elif args.verbose:
        args.mode = "verbose"
    elif getattr(args, "warn_only", False):
        args.mode = "warn"
    else:
        args.mode = "brief"

    if getattr(args, "live", False) or getattr(args, "inventory", None):
        if not args.live and args.inventory:
            args.live = True
        if not args.paths and not args.inventory:
            parser.error(
                "--live requires one or more device hostnames as PATH, or --inventory FILE"
            )
        if args.use_tech_support and args.inventory is None and not args.live:
            parser.error("-T/--use-tech-support requires --live")

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
    serial_number: Optional[str]
    system_time: Optional[str]
    health: Severity
    warn_count: int
    error_count: int


class TechSupportParser:
    """Parse a show-tech / show-tech-support-all text into command blocks."""

    # Match section headers: ------------- command text ------------- (EOS show-tech / support-bundle)
    _CMD_HEADER_RE = re.compile(r"^[-]{3,}\s*(.+?)\s*[-]{3,}\s*$")

    @classmethod
    def _header_command(cls, line: str) -> Optional[str]:
        m = cls._CMD_HEADER_RE.match(line.rstrip("\r\n"))
        if not m:
            return None
        cmd = m.group(1).strip()
        if not cmd:
            return None
        # Reject pure dash/table-decoration lines (regex can treat a lone "-" as the "command").
        if not any(c.isalnum() for c in cmd):
            return None
        low = cmd.lower()
        # Real show-tech section titles are CLI lines: "show ..." or "bash ...". Dashed
        # in-output headings (e.g. "------------- BSR Border Information -------------")
        # are not bundle section boundaries.
        if not (low.startswith("show") or low.startswith("bash")):
            return None
        return low

    @classmethod
    def parse_lines(cls, lines: Iterable[str]) -> List[CommandBlock]:
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
            hdr_cmd = cls._header_command(raw)
            if hdr_cmd is not None:
                flush_block()
                current_cmd = hdr_cmd
            else:
                if current_cmd is not None:
                    current_lines.append(raw.rstrip("\r\n"))

        flush_block()
        return blocks

    @classmethod
    def parse(cls, text: str) -> List[CommandBlock]:
        """Backward-compatible parser that accepts a single text string."""
        return cls.parse_lines(text.splitlines())


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
        self.serial_number: Optional[str] = None
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


def _path_under_tech_support_directory(member_or_path: str) -> bool:
    """
    True if the path has a directory component named 'tech-support' (EOS rotated log dir).

    Those bundles are skipped; we only pick top-level / support-bundle style show-tech files.
    """
    norm = member_or_path.replace("\\", "/").strip()
    parts = [p for p in norm.split("/") if p and p not in (".",)]
    return any(p.lower() == "tech-support" for p in parts)


def is_showtech_filename(basename: str) -> bool:
    """
    Return True if the last path segment looks like a show-tech / tech-support bundle file.

    Matches EOS support-bundle names with or without a "show" prefix (e.g. tech_support_*,
    *-tech-support-all-*) and excludes extended / ribd variants where applicable.
    """
    base = basename.lower()
    if base in ("show-tech", "show-tech-support-all"):
        return True
    if "tech-support-all" in base:
        if "tech-support-extended" in base or "tech-support-ribd" in base:
            return False
        return True
    if base.startswith("show-tech") and not base.startswith(
        "show-tech-support-extended"
    ) and not base.startswith("show-tech-support-ribd"):
        return True
    if "tech_support" in base:
        if "tech_support_extended" in base or "tech_support_ribd" in base:
            return False
        return True
    return False


def discover_showtech_files_from_directory(root: Path) -> List[Path]:
    """Recursively find show-tech/show-tech-support-all files under a directory."""
    results: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _path_under_tech_support_directory(str(path)):
            continue
        if is_showtech_filename(path.name):
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
        if _path_under_tech_support_directory(name_):
            return False
        return is_showtech_filename(Path(name_).name)
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


def _bytes_to_text_maybe_gunzip(member_name: str, data: bytes) -> str:
    """Decode archive member bytes; gunzip single-file .gz members (not .tar.gz)."""
    lower = member_name.lower()
    if lower.endswith(".gz") and not lower.endswith(".tar.gz"):
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
    return data.decode("utf-8", errors="replace")


def _read_path_maybe_gunzip(path: Path) -> str:
    """Read a file from disk; gunzip single-file .gz (not .tar.gz), same rules as bundles."""
    return _bytes_to_text_maybe_gunzip(path.name, path.read_bytes())


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
                    return _bytes_to_text_maybe_gunzip(spec.inner_member, f.read())
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                member = tf.getmember(spec.inner_member)
                f = tf.extractfile(member)
                if f is None:
                    return ""
                data = f.read()
                return _bytes_to_text_maybe_gunzip(spec.inner_member, data)

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
                        return _bytes_to_text_maybe_gunzip(spec.inner_member, f.read())
            else:
                bio.seek(0)
                try:
                    with tarfile.open(fileobj=bio, mode="r:*") as ntar:
                        member = ntar.getmember(spec.inner_member)
                        f = ntar.extractfile(member)
                        if f is None:
                            return ""
                        data = f.read()
                        return _bytes_to_text_maybe_gunzip(spec.inner_member, data)
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
                        return _bytes_to_text_maybe_gunzip(spec.inner_member, f.read())
            else:
                bio.seek(0)
                try:
                    with tarfile.open(fileobj=bio, mode="r:*") as ntar:
                        member = ntar.getmember(spec.inner_member)
                        f = ntar.extractfile(member)
                        if f is None:
                            return ""
                        data = f.read()
                        return _bytes_to_text_maybe_gunzip(spec.inner_member, data)
                except tarfile.TarError:
                    return ""


# ---------------------------------------------------------------------------
# Live device collection (eAPI + SSH)
# ---------------------------------------------------------------------------


class LiveCollectionError(RuntimeError):
    """Raised when collecting commands from a live device fails."""


class LiveProgress:
    """Thread-safe stderr progress reporter for live device collection.

    Auto-disables when stderr is not a TTY or when debug logging is on (debug
    logs would interleave badly with a redrawing status line).
    """

    def __init__(self, total_devices: int, debug: bool = False):
        import threading as _threading
        self.total = total_devices
        self.enabled = (
            total_devices > 0
            and sys.stderr.isatty()
            and not debug
        )
        self.done = 0
        self.failed = 0
        self._active: "Dict[str, str]" = {}
        self._lock = _threading.Lock()
        self._last_len = 0

    # --- callbacks -------------------------------------------------------

    def device_started(self, host: str) -> None:
        self._update(host, "connecting")

    def device_stage(self, host: str, stage: str) -> None:
        self._update(host, stage)

    def device_cmd(self, host: str, done: int, total: int, cmd: str) -> None:
        # Trim long commands so the line stays readable.
        short = cmd if len(cmd) <= 40 else cmd[:37] + "..."
        self._update(host, f"[{done}/{total}] {short}")

    def device_finished(self, host: str, ok: bool = True) -> None:
        with self._lock:
            self._active.pop(host, None)
            if ok:
                self.done += 1
            else:
                self.failed += 1
            self._render_locked()

    def close(self) -> None:
        """Erase the status line and emit a final summary on its own line."""
        with self._lock:
            if not self.enabled:
                return
            self._clear_locked()
            summary = f"Live collection: {self.done}/{self.total} ok"
            if self.failed:
                summary += f", {self.failed} failed"
            sys.stderr.write(summary + "\n")
            sys.stderr.flush()

    # --- internals -------------------------------------------------------

    def _update(self, host: str, stage: str) -> None:
        with self._lock:
            self._active[host] = stage
            self._render_locked()

    def _render_locked(self) -> None:
        if not self.enabled:
            return
        if self.total > 1:
            if self._active:
                items = list(self._active.items())
                shown = items[:2]
                tail = "; ".join(f"{h}: {s}" for h, s in shown)
                if len(items) > 2:
                    tail += f" (+{len(items) - 2} more)"
            else:
                tail = "-"
            counter = f"{self.done}/{self.total}"
            if self.failed:
                counter += f" ({self.failed} failed)"
            text = f"[{counter}] {tail}"
        else:
            if self._active:
                host, stage = next(iter(self._active.items()))
                text = f"{host}: {stage}"
            else:
                text = f"done ({self.done}/{self.total})"
        pad = max(0, self._last_len - len(text))
        sys.stderr.write("\r" + text + (" " * pad))
        sys.stderr.flush()
        self._last_len = len(text)

    def _clear_locked(self) -> None:
        if self._last_len:
            sys.stderr.write("\r" + " " * self._last_len + "\r")
            sys.stderr.flush()
            self._last_len = 0


@dataclass
class DeviceCredentials:
    host: str
    user: str
    password: str
    port: Optional[int] = None       # eAPI port; None = derive from use_https
    transport: str = "auto"          # "auto" | "eapi" | "ssh"
    use_https: bool = True
    verify_tls: bool = False         # eAPI TLS verification (--insecure flips this)


def _format_showtech_section(cmd: str, body: str) -> str:
    """Format one command's output the way TechSupportParser expects.

    Header must begin with "show" or "bash" (per TechSupportParser._header_command)
    so downstream parsing is identical to an offline show-tech.
    """
    body = body.rstrip("\r\n")
    return f"------------ {cmd} ------------\n{body}\n"


def _assemble_showtech_text(outputs: Dict[str, str], command_order: Sequence[str]) -> str:
    parts: List[str] = []
    for cmd in command_order:
        if cmd not in outputs:
            continue
        parts.append(_format_showtech_section(cmd, outputs[cmd]))
    return "\n".join(parts)


def fetch_via_eapi(
    creds: DeviceCredentials,
    commands: Sequence[str],
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, str]:
    """Single JSON-RPC runCmds call (format=text). stdlib only.

    Returns dict mapping each command to its raw text output.

    progress_cb, if given, is called once before the request as
    (0, len(commands), "eapi: collecting N commands") so callers can
    surface a coarse stage indicator. eAPI returns all commands in one
    response, so per-command progress isn't available.
    """
    if progress_cb is not None:
        try:
            progress_cb(0, len(commands), f"eapi: collecting {len(commands)} cmds")
        except Exception:
            pass
    import base64
    import json as _json
    import ssl
    import urllib.error
    import urllib.request

    scheme = "https" if creds.use_https else "http"
    port = creds.port or (443 if creds.use_https else 80)
    url = f"{scheme}://{creds.host}:{port}/command-api"

    payload = {
        "jsonrpc": "2.0",
        "method": "runCmds",
        "params": {
            "version": 1,
            "cmds": list(commands),
            "format": "text",
        },
        "id": "health_check_eos",
    }
    body = _json.dumps(payload).encode("utf-8")
    auth = base64.b64encode(f"{creds.user}:{creds.password}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
        method="POST",
    )

    ssl_ctx: Optional[ssl.SSLContext] = None
    if creds.use_https and not creds.verify_tls:
        ssl_ctx = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl_ctx) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", "replace")
        except Exception:
            err_body = ""
        raise LiveCollectionError(
            f"eAPI HTTP {exc.code} from {creds.host}: {exc.reason} {err_body[:200]}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise LiveCollectionError(f"eAPI connection to {creds.host} failed: {exc}") from exc

    try:
        data = _json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise LiveCollectionError(f"eAPI from {creds.host} returned non-JSON: {exc}") from exc

    if "error" in data and data["error"]:
        # Partial results may be in error.data; still surface text outputs collected so far.
        err = data["error"]
        msg = err.get("message", "unknown eAPI error")
        partial = err.get("data") or []
        results = [_extract_eapi_text(entry) for entry in partial]
        if not any(results):
            raise LiveCollectionError(f"eAPI error from {creds.host}: {msg}")
        LOG.warning("eAPI partial error from %s: %s", creds.host, msg)
    else:
        results = [_extract_eapi_text(entry) for entry in (data.get("result") or [])]

    out: Dict[str, str] = {}
    for cmd, text in zip(commands, results):
        if text is not None:
            out[cmd] = text
    return out


def _extract_eapi_text(entry: object) -> Optional[str]:
    # eAPI text format returns {"output": "..."} per command.
    if isinstance(entry, dict):
        if "output" in entry and isinstance(entry["output"], str):
            return entry["output"]
        # JSON format response shouldn't happen here but guard anyway.
        return _json_dumps_safe(entry)
    if isinstance(entry, str):
        return entry
    return None


def _json_dumps_safe(obj: object) -> str:
    import json as _json
    try:
        return _json.dumps(obj, indent=2, sort_keys=True)
    except Exception:
        return str(obj)


def fetch_via_ssh(
    creds: DeviceCredentials,
    commands: Sequence[str],
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, str]:
    """SSH fallback using paramiko (optional dependency).

    Opens one interactive shell, disables paging, then sends each command and
    splits responses on the device prompt.

    progress_cb, if given, is invoked as (i, total, cmd) before each command
    is sent so callers can render per-command progress.
    """
    try:
        import paramiko  # type: ignore
    except ImportError as exc:
        raise LiveCollectionError(
            "SSH transport requires paramiko (pip install paramiko)"
        ) from exc

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=creds.host,
            port=22,
            username=creds.user,
            password=creds.password,
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
        )
    except Exception as exc:
        raise LiveCollectionError(f"SSH connection to {creds.host} failed: {exc}") from exc

    out: Dict[str, str] = {}
    try:
        chan = client.invoke_shell(width=240, height=10000)
        chan.settimeout(60)
        _ssh_drain(chan, settle=1.0)
        # Disable EOS paging and the welcome prompt; ignore output.
        chan.send("terminal length 0\n")
        _ssh_drain(chan, settle=0.5)
        chan.send("terminal width 32767\n")
        _ssh_drain(chan, settle=0.3)
        total = len(commands)
        for i, cmd in enumerate(commands, start=1):
            if progress_cb is not None:
                try:
                    progress_cb(i, total, cmd)
                except Exception:
                    pass
            chan.send(cmd + "\n")
            text = _ssh_drain(chan, settle=2.0)
            out[cmd] = _ssh_strip_prompt(text, cmd)
    finally:
        try:
            client.close()
        except Exception:
            pass
    return out


def _ssh_drain(chan, settle: float = 1.0) -> str:
    """Read until no new bytes arrive for `settle` seconds."""
    import time as _time
    buf: List[bytes] = []
    deadline = _time.monotonic() + 90  # hard cap per command
    last_recv = _time.monotonic()
    while _time.monotonic() < deadline:
        if chan.recv_ready():
            chunk = chan.recv(65536)
            if not chunk:
                break
            buf.append(chunk)
            last_recv = _time.monotonic()
        else:
            if _time.monotonic() - last_recv >= settle:
                break
            _time.sleep(0.05)
    return b"".join(buf).decode("utf-8", "replace")


_SSH_PROMPT_RE = re.compile(r"(?m)^[\w.\-]+[>#]\s*$")


def _ssh_strip_prompt(text: str, cmd: str) -> str:
    """Remove the echoed command line and the trailing device prompt."""
    lines = text.splitlines()
    # Drop the first line if it echoes the command we sent.
    if lines and cmd.strip() and cmd.strip() in lines[0]:
        lines = lines[1:]
    # Drop a trailing prompt line.
    while lines and _SSH_PROMPT_RE.match(lines[-1].rstrip()):
        lines.pop()
    return "\n".join(lines)


def fetch_device_output(
    creds: DeviceCredentials,
    commands: Sequence[str],
    use_tech_support: bool = False,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> str:
    """Collect commands from a device and return show-tech-style text.

    progress_cb (if given) receives (current, total, stage_or_cmd) updates
    suitable for a live progress display. For SSH it fires per-command; for
    eAPI only a single coarse stage event is reported.
    """
    if use_tech_support:
        cmds = ["show tech-support all"]
    else:
        cmds = list(commands)

    transports: List[str]
    if creds.transport == "eapi":
        transports = ["eapi"]
    elif creds.transport == "ssh":
        transports = ["ssh"]
    else:
        transports = ["eapi", "ssh"]

    last_err: Optional[Exception] = None
    outputs: Dict[str, str] = {}
    for t in transports:
        try:
            if t == "eapi":
                outputs = fetch_via_eapi(creds, cmds, progress_cb=progress_cb)
            else:
                outputs = fetch_via_ssh(creds, cmds, progress_cb=progress_cb)
            if outputs:
                LOG.debug("Collected %d/%d commands from %s via %s",
                          len(outputs), len(cmds), creds.host, t)
                break
        except LiveCollectionError as exc:
            last_err = exc
            LOG.info("Transport %s failed for %s: %s", t, creds.host, exc)
            continue

    if not outputs:
        raise LiveCollectionError(
            f"Failed to collect from {creds.host} via {transports}: {last_err}"
        )

    if use_tech_support:
        # `show tech-support all` already contains the section dividers; return verbatim
        # so TechSupportParser picks the same sections an offline file would.
        return outputs.get("show tech-support all", "")
    return _assemble_showtech_text(outputs, cmds)


# ---------------------------------------------------------------------------
# Basic parsers for show version / clock
# ---------------------------------------------------------------------------


SHOW_VERSION_VERSION_RE = re.compile(r"^\s*Software image version:\s*(\S+)", re.IGNORECASE)
SHOW_VERSION_ARCH_RE = re.compile(r"^\s*Architecture\s*:\s*(\S+)", re.IGNORECASE)
SHOW_VERSION_UPTIME_RE = re.compile(r"^\s*Uptime\s*:\s*(.+)$", re.IGNORECASE)
SHOW_VERSION_MEM_RE = re.compile(
    r"^\s*(Total|Free)\s+memory\s*:\s*([0-9]+)\s*(\w+)?", re.IGNORECASE
)
SHOW_VERSION_SERIAL_RE = re.compile(r"^\s*Serial number:\s*(.+)$", re.IGNORECASE)

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

    # Serial number: may appear in "show version" or "show version detail" block
    for block in blocks:
        for line in block.lines:
            if m := SHOW_VERSION_SERIAL_RE.search(line):
                ctx.serial_number = m.group(1).strip()
                break
        if ctx.serial_number is not None:
            break

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
    # CLI commands this check needs from the device, used by --live collection
    # to know what to query. Each entry must match the prefix passed to
    # ctx.get_blocks() so live and offline runs produce identical results.
    required_commands: Sequence[str] = ()

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        raise NotImplementedError


REGISTERED_CHECKS: List[BaseCheck] = []

# Commands consumed by the metadata parsers / hostname helper / report formatter,
# independent of any specific check.
_METADATA_COMMANDS: Tuple[str, ...] = (
    "show version",
    "show clock",
    "show running-config sanitized",
)


def register_check(cls):
    """Class decorator to register a check."""
    instance = cls()
    REGISTERED_CHECKS.append(instance)
    return cls


def platform_supported(check: BaseCheck, platform: str) -> bool:
    if "all" in check.supported_platforms:
        return True
    return platform in check.supported_platforms


def collect_required_commands() -> List[str]:
    """Union of every registered check's required_commands plus metadata commands."""
    cmds: set = set(_METADATA_COMMANDS)
    for chk in REGISTERED_CHECKS:
        cmds.update(getattr(chk, "required_commands", ()) or ())
    return sorted(cmds)


# -------------------------- Generic checks ---------------------------------


@register_check
class CoolingStatusCheck(BaseCheck):
    name = "cooling_status"
    category = "environment"
    supported_platforms = ("all",)
    required_commands = ("show system env cooling",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show system env cooling")
        if not blocks:
            # Command not present – silently skip this check
            return []
        lines = blocks[0].lines
        # Allow optional colon and capture rest of line as status.
        status: Optional[str] = None
        for line in lines:
            m = re.search(
                r"System\s+cooling\s+status\s+is\s*:?\s*(\S.*)$",
                line,
                re.IGNORECASE,
            )
            if m:
                status = m.group(1)
                break
        if status is None:
            # If format is unfamiliar, skip instead of emitting noisy INFO.
            return []
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
    required_commands = ("show system env temperature",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show system env temperature")
        if not blocks:
            # Command not present – silently skip this check
            return []
        lines = blocks[0].lines
        status: Optional[str] = None
        for line in lines:
            m = re.search(
                r"System\s+temperature\s+status\s+is\s*:?\s*(\S.*)$",
                line,
                re.IGNORECASE,
            )
            if m:
                status = m.group(1)
                break
        if status is None:
            # If format is unfamiliar, skip instead of emitting noisy INFO.
            return []
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
    required_commands = ("bash ls -ltr /var/core",)

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
    required_commands = ("bash df -h",)

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
    required_commands = ("show extensions detail",)

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
    required_commands = ("show processes top once",)

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
        # Treat any process with CPU usage >= 100 as WARN; values between 99 and 100 remain INFO.
        sev = Severity.WARN if any(v >= 100 for _, v in offenders) else Severity.INFO
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
    required_commands = ("show processes top memory once",)

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
    required_commands = ("show module",)

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
                    
                    # Check if uptime is abnormal: only when uptime < 1 hour (i.e. hours field is 0)
                    # N/A or no numbers -> abnormal; otherwise parse "N days H:MM:SS" and require days==0 and H==0
                    is_abnormal_uptime = False
                    if uptime_str == "N/A" or (uptime_str and not re.search(r"\d+", uptime_str)):
                        is_abnormal_uptime = True
                    else:
                        # Parse "X day(s) H:MM:SS" -> uptime < 1 hour when days==0 and hours==0
                        # EOS commonly prints: "127 days, 0:53:56" (note the comma)
                        md = re.search(r"(\d+)\s+day(?:s)?[,]?\s+(\d+):(\d+):(\d+)", uptime_str)
                        if md:
                            days_val = int(md.group(1))
                            hours_val = int(md.group(2))
                            if days_val == 0 and hours_val == 0:
                                is_abnormal_uptime = True
                        else:
                            # Fallback: only "H:MM:SS" (e.g. "0:30:00") -> hour 0 means < 1 hour
                            mt = re.search(r"(\d+):(\d+):(\d+)", uptime_str)
                            if mt and int(mt.group(1)) == 0:
                                is_abnormal_uptime = True

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
    required_commands = ("show platform sand health",)

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
        has_issue = False
        for line in blocks[0].lines:
            if re.search(r"fail|error|not\s+initial", line, re.IGNORECASE):
                has_issue = True
                break
        if has_issue:
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
    required_commands = ("show platform fap fabric detail",)

    PATTERN_78XX = re.compile(
        r"(U--- Ramon|[|]---U Ramon|I---I? Ramon|[|]---I Ramon|[|]--- Ramon|---[|] Ramon)"
    )
    PATTERN_OTHER = re.compile(
        r"(U--- Fe|[|]---U Fe|I---I? Fe|[|]---I Fe|[|]--- Fe|---[|] Fe)"
    )

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
        pattern = (
            self.PATTERN_78XX
            if ctx.platform_series == "78xx"
            else self.PATTERN_OTHER
        )
        # Find matching lines (output full lines like egrep)
        matched_lines = []
        for line in lines:
            if pattern.search(line):
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
class PlatformFapCountersNzCheck(BaseCheck):
    name = "platform_fap_counters_nz"
    category = "hardware"
    supported_platforms = ("78xx", "75xx")
    required_commands = ("show platform fap counters",)

    CMD_PREFIX = "show platform fap counters"
    CNTR_75_RE = re.compile(
        r"Cgm\s+Unicast\s+Data\s+Buffer\s+Drop\s+Reassembly\s+Cnt",
        re.IGNORECASE,
    )
    CNTR_78_RE = re.compile(r"Voq\s+Latency\s+Rjct", re.IGNORECASE)
    DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
    _CHIP_SECTION_RE = re.compile(r"^(\S+/\d+)\s+Counters\b")
    _CGM_BRACKET_RE = re.compile(r"^(\[[^\]]+\])\s*$")

    @staticmethod
    def _is_separator_dash_line(raw: str) -> bool:
        t = raw.strip()
        return len(t) >= 3 and set(t) == {"-"}

    @classmethod
    def _is_counter_table_header_line(cls, stripped: str) -> bool:
        lower = stripped.lower()
        if "counter name" in lower and "value" in lower:
            return True
        return "value" in lower and "last update" in lower

    @classmethod
    def _context_prefix_line_indices(cls, lines: Sequence[str], i: int) -> List[int]:
        """
        Lines above a counter row that recreate the CLI layout: chip title, rule line,
        column header, [BlockName] — all verbatim so the following counter line stays
        column-aligned with the header.
        """
        n = len(lines)
        if i < 0 or i >= n:
            return []
        block_idx: Optional[int] = None
        j = i - 1
        while j >= 0:
            if cls._CGM_BRACKET_RE.match(lines[j].strip()):
                block_idx = j
                break
            j -= 1

        header_idx: Optional[int] = None
        start_scan = block_idx - 1 if block_idx is not None else i - 1
        j = start_scan
        while j >= 0:
            st = lines[j].strip()
            if cls._is_counter_table_header_line(st):
                header_idx = j
                break
            if block_idx is not None and cls._CHIP_SECTION_RE.match(st):
                break
            j -= 1

        dash_idx: Optional[int] = None
        chip_idx: Optional[int] = None
        if header_idx is not None:
            j = header_idx - 1
            while j >= 0:
                st = lines[j].strip()
                if cls._is_separator_dash_line(lines[j]):
                    dash_idx = j
                    break
                if cls._CHIP_SECTION_RE.match(st):
                    chip_idx = j
                    break
                j -= 1
        if chip_idx is None and dash_idx is not None:
            j = dash_idx - 1
            while j >= 0:
                if cls._CHIP_SECTION_RE.match(lines[j].strip()):
                    chip_idx = j
                    break
                j -= 1

        idxs = [x for x in (chip_idx, dash_idx, header_idx, block_idx) if x is not None]
        idxs.sort()
        return idxs

    @classmethod
    def _enriched_counter_rows(
        cls, lines: Sequence[str], row_indices: Sequence[int]
    ) -> List[str]:
        """
        Repeat chip section headings as in the CLI (chip line, rule, column header, block),
        then the counter line unchanged (preserves leading spaces vs. table columns).
        """
        out: List[str] = []
        last_prefix_key: Optional[Tuple[int, ...]] = None
        for i in row_indices:
            if i < 0 or i >= len(lines):
                continue
            prefix = tuple(cls._context_prefix_line_indices(lines, i))
            if prefix != last_prefix_key:
                for j in prefix:
                    if 0 <= j < len(lines):
                        out.append(lines[j].rstrip("\r\n"))
                last_prefix_key = prefix
            out.append(lines[i].rstrip("\r\n"))
        return out

    @classmethod
    def _last_update_ts_75xx_line(cls, line: str) -> Optional[str]:
        """
        Column order in 'show platform fap counters': Value, First update, Last update,
        optional Last discontinuity. When First is blank there is only one timestamp (Last update).
        Use index 1 when two or more timestamps exist so we never pick Last discontinuity.
        """
        dates = cls.DATE_RE.findall(line)
        if not dates:
            return None
        if len(dates) == 1:
            return dates[0]
        return dates[1]

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks(self.CMD_PREFIX)
        LOG.debug(
            "platform_fap_counters_nz check: found %d block(s) for %r",
            len(blocks),
            self.CMD_PREFIX,
        )
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show platform fap counters | nz output not found.",
                    command=self.CMD_PREFIX,
                )
            ]
        lines = blocks[0].lines

        if ctx.platform_series == "75xx":
            matched_idx = [
                (i, ln)
                for i, ln in enumerate(lines)
                if self.CNTR_75_RE.search(ln)
            ]
            if not matched_idx:
                return [
                    CheckResult(
                        name=self.name,
                        category=self.category,
                        severity=Severity.OK,
                        summary=(
                            "Cgm Unicast Data Buffer Drop Reassembly Cnt not present in "
                            "non-zero FAP counters."
                        ),
                        command=self.CMD_PREFIX,
                    )
                ]
            sample_idx = [i for i, _ in matched_idx[:10]]
            if not ctx.system_time:
                return [
                    CheckResult(
                        name=self.name,
                        category=self.category,
                        severity=Severity.INFO,
                        summary=(
                            "Cgm Unicast Data Buffer Drop Reassembly Cnt row present but "
                            "system time unavailable; cannot compare last update date."
                        ),
                        details=self._enriched_counter_rows(lines, sample_idx),
                        command=self.CMD_PREFIX,
                    )
                ]
            try:
                dt_clock = _dt.datetime.strptime(
                    ctx.system_time.strip(), "%a %b %d %H:%M:%S %Y"
                )
                date_clock = dt_clock.date()
            except (ValueError, TypeError) as exc:
                LOG.debug("platform_fap_counters_nz: show clock parse failed: %s", exc)
                return [
                    CheckResult(
                        name=self.name,
                        category=self.category,
                        severity=Severity.INFO,
                        summary=(
                            "Cgm Unicast Data Buffer Drop Reassembly Cnt row present but "
                            "show clock could not be parsed; cannot compare last update date."
                        ),
                        details=self._enriched_counter_rows(lines, sample_idx),
                        command=self.CMD_PREFIX,
                    )
                ]

            same_day = False
            warn_indices: List[int] = []
            for i, ln in matched_idx:
                last_ts = self._last_update_ts_75xx_line(ln)
                if not last_ts:
                    continue
                try:
                    dt_last = _dt.datetime.strptime(
                        last_ts, "%Y-%m-%d %H:%M:%S"
                    )
                except (ValueError, TypeError):
                    continue
                if dt_last.date() == date_clock:
                    same_day = True
                    if i not in warn_indices:
                        warn_indices.append(i)
                    if len(warn_indices) >= 10:
                        break

            if same_day:
                return [
                    CheckResult(
                        name=self.name,
                        category=self.category,
                        severity=Severity.WARN,
                        summary=(
                            "Last update date for Cgm Unicast Data Buffer Drop Reassembly Cnt "
                            "matches device date (show clock)."
                        ),
                        details=self._enriched_counter_rows(lines, warn_indices),
                        command=self.CMD_PREFIX,
                    )
                ]
            if not any(self._last_update_ts_75xx_line(ln) for _, ln in matched_idx):
                return [
                    CheckResult(
                        name=self.name,
                        category=self.category,
                        severity=Severity.INFO,
                        summary=(
                            "Cgm Unicast Data Buffer Drop Reassembly Cnt row present but "
                            "no YYYY-MM-DD timestamp found on row."
                        ),
                        details=self._enriched_counter_rows(lines, sample_idx),
                        command=self.CMD_PREFIX,
                    )
                ]
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.OK,
                    summary=(
                        "Cgm Unicast Data Buffer Drop Reassembly Cnt present; last update is "
                        "not the same calendar day as show clock."
                    ),
                    details=self._enriched_counter_rows(lines, sample_idx),
                    command=self.CMD_PREFIX,
                )
            ]

        # 78xx
        matched78_idx = [
            (i, ln)
            for i, ln in enumerate(lines)
            if self.CNTR_78_RE.search(ln)
        ]
        if matched78_idx:
            idx78 = [i for i, _ in matched78_idx[:10]]
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="Voq Latency Rjct present in non-zero FAP counters.",
                    details=self._enriched_counter_rows(lines, idx78),
                    command=self.CMD_PREFIX,
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="Voq Latency Rjct not present in non-zero FAP counters.",
                command=self.CMD_PREFIX,
            )
        ]


@register_check
class RedundancyStatusCheck(BaseCheck):
    name = "redundancy_status"
    category = "system"
    supported_platforms = ("78xx", "75xx")
    required_commands = ("show redundancy status",)

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
    required_commands = ("show pci",)

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
    required_commands = ("show agent logs crash",)

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
    required_commands = ("show system environment power detail",)

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


# Regex patterns for "show logging threshold errors" check.
# Add/remove rules by appending or deleting a line in the list below; matching is case-insensitive.
LOGGING_THRESHOLD_ERROR_PATTERNS = [
    # High-severity syslog levels 0/1/2 in tags like %AGENT-0-FOO:, %AGENT-1-FOO:, %AGENT-2-FOO:
    # (e.g. %AGENT-6-INITIALIZED uses level 6; we only match 0/1/2)
    r"%[A-Z0-9_-]+-[0-2]-[A-Z0-9_-]+:",    
    # Memory / link error /DRAM fatal interrupt indicators
    # ECC: exclude "ecc" in IPv6 addresses (e.g. 93f:ecc) via negative lookbehind
    r"(?<!:)\bECC\b",
    r"\bCRC\b",
    r"\bDRAM_FATAL_INTERRUPT\b",
    # AttrLog buffer exhaustion
    r"AttrLog buffer is full",
    r"FEC_RESOURCE",
]


@register_check
class LoggingThresholdErrorsCheck(BaseCheck):
    name = "logging_threshold_errors"
    category = "hardware"
    supported_platforms = ("all",)
    required_commands = ("show logging threshold errors",)
    
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
    required_commands = ("show interfaces counters queue drops",)

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
    required_commands = ("show cpu counters queue",)

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
        MAX_HEADER_SCAN = 50
        for idx, line in enumerate(lines):
            if idx > MAX_HEADER_SCAN:
                break
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
    required_commands = ("show interfaces counters discards",)

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
        
        MAX_HEADER_SCAN = 50
        for idx, line in enumerate(lines):
            if idx > MAX_HEADER_SCAN:
                break
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
    required_commands = ("show interfaces counters errors",)

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
        
        MAX_HEADER_SCAN = 50
        for idx, line in enumerate(lines):
            if idx > MAX_HEADER_SCAN:
                break
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
class InterfaceErrdisabledCheck(BaseCheck):
    name = "interfaces_errdisabled"
    category = "interface"
    supported_platforms = ("all",)
    required_commands = ("show interfaces status errdisabled",)

    def run(self, ctx: TechSupportContext) -> List[CheckResult]:
        blocks = ctx.get_blocks("show interfaces status errdisabled")
        if not blocks:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.INFO,
                    summary="show interfaces status errdisabled output not found.",
                )
            ]

        lines = blocks[0].lines

        # Robust parsing:
        # - EOS output is usually already filtered to errdisabled interfaces,
        #   but interface-name prefix formats vary across platforms/versions.
        # - Therefore we treat any non-header data line containing keyword
        #   `errdisabled` as an offender.
        offenders: List[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Skip table separator / border lines.
            if stripped.replace("-", "").replace("=", "").replace("|", "").strip() == "":
                continue

            lower = stripped.lower()

            # Skip likely header lines.
            # (We only need to identify data rows; header formats vary by platform/versions.)
            if "port" in lower and ("status" in lower or "reason" in lower or "name" in lower):
                continue
            if lower.startswith("no ") and ("errdisabled" in lower or "err-disabled" in lower):
                continue

            # Skip "No err-disabled..." summary line.
            if lower.startswith("no ") and ("err-disabled" in lower or "errdisabled" in lower):
                # Example: "No err-disabled interfaces found"
                continue

            if "errdisabled" in lower:
                offenders.append(stripped)

        # cap details to avoid flooding debug output when many interfaces are affected
        MAX_DETAILS = 200
        details: List[str]
        if offenders:
            details = offenders[:MAX_DETAILS]
            if len(offenders) > MAX_DETAILS:
                details.append(
                    f"... ({len(offenders) - MAX_DETAILS} more line(s) truncated)"
                )
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary=f"Errdisabled interfaces present ({len(offenders)} interface(s)).",
                    details=details,
                    command="show interfaces status errdisabled",
                )
            ]

        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="No errdisabled interfaces found.",
                command="show interfaces status errdisabled",
            )
        ]


@register_check
class HardwareCounterDropCheck(BaseCheck):
    name = "hardware_counter_drop"
    category = "hardware"
    supported_platforms = ("78xx", "75xx", "7289", "7388")
    required_commands = ("show hardware counter drop",)

    SUMMARY_A_RE = re.compile(
        r"Total\s+Adverse\s*\(A\)\s*Drops:\s*(\d+)", re.IGNORECASE
    )
    SUMMARY_C_RE = re.compile(
        r"Total\s+Congestion\s*\(C\)\s*Drops:\s*(\d+)", re.IGNORECASE
    )
    DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")

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

        lines = blocks[0].lines
        text = "\n".join(lines)
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
        adverse_same_day_row_count = 0
        congestion_same_day_row_count = 0
        clock_parse_ok = False
        clock_parse_error = None
        
        try:
            # Example: Thu Jan 29 23:10:00 2026
            dt_clock = _dt.datetime.strptime(clock, "%a %b %d %H:%M:%S %Y")
            date_clock = dt_clock.date()
            clock_parse_ok = True

            _hcd_row_parse = {"A": {"same": 0, "other": 0}, "C": {"same": 0, "other": 0}}
            _hcd_samples = {"A_same": [], "A_other": [], "C_same": [], "C_other": []}
            
            # Check Summary section for total counts
            summary_match_a = self.SUMMARY_A_RE.search(text)
            summary_match_c = self.SUMMARY_C_RE.search(text)
            
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
                        matches = self.DATE_RE.findall(stripped)
                        if matches:
                            # Last match should be Last Occurrence
                            last_occurrence_str = matches[-1]
                            try:
                                dt_last = _dt.datetime.strptime(last_occurrence_str, "%Y-%m-%d %H:%M:%S")
                                if dt_last.date() == date_clock:
                                    drop_same_day = True
                                    adverse_same_day_row_count += 1
                                    _hcd_row_parse["A"]["same"] += 1
                                    if len(_hcd_samples["A_same"]) < 3:
                                        _hcd_samples["A_same"].append(
                                            {"last": last_occurrence_str, "line": stripped[:140]}
                                        )
                                    # Don't break, continue counting all rows
                                else:
                                    _hcd_row_parse["A"]["other"] += 1
                                    if len(_hcd_samples["A_other"]) < 3:
                                        _hcd_samples["A_other"].append(
                                            {"last": last_occurrence_str, "line": stripped[:140]}
                                        )
                            except (ValueError, Exception):
                                continue
                elif stripped.startswith("C "):
                    congestion_row_count += 1
                    # Parse the line to extract Last Occurrence date
                    parts = stripped.split()
                    if len(parts) >= 6:
                        matches = self.DATE_RE.findall(stripped)
                        if matches:
                            last_occurrence_str = matches[-1]
                            try:
                                dt_last = _dt.datetime.strptime(last_occurrence_str, "%Y-%m-%d %H:%M:%S")
                                if dt_last.date() == date_clock:
                                    drop_same_day = True
                                    congestion_same_day_row_count += 1
                                    _hcd_row_parse["C"]["same"] += 1
                                    if len(_hcd_samples["C_same"]) < 3:
                                        _hcd_samples["C_same"].append(
                                            {"last": last_occurrence_str, "line": stripped[:140]}
                                        )
                                    # Don't break, continue counting all rows
                                else:
                                    _hcd_row_parse["C"]["other"] += 1
                                    if len(_hcd_samples["C_other"]) < 3:
                                        _hcd_samples["C_other"].append(
                                            {"last": last_occurrence_str, "line": stripped[:140]}
                                        )
                            except (ValueError, Exception):
                                continue

        except Exception as e:
            clock_parse_error = str(e)
            LOG.debug(f"Failed to parse show clock time for hardware counter drop comparison: {e}")

        # Alert if we have A or C drops AND at least one has Last Occurrence on same day
        if drop_same_day and (has_adverse_drops or has_congestion_drops):
            summary_parts = []
            if has_adverse_drops:
                summary_parts.append(f"A same-day rows: {adverse_same_day_row_count}")
            if has_congestion_drops:
                summary_parts.append(f"C same-day rows: {congestion_same_day_row_count}")
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
            summary_parts.append(f"A rows: {adverse_row_count}")
        if congestion_row_count > 0:
            summary_parts.append(f"C rows: {congestion_row_count}")
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
    required_commands = ("show hardware capacity",)

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
            try:
                val = int(m.group(1))
            except ValueError:
                continue
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
    required_commands = ("show system health storage",)

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
    required_commands = ("show hardware fpga error",)

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


_SCD_SATELLITE_RETRY_ERR_RE = re.compile(
    r"RetryErr\s*[=:]\s*(0x[0-9a-fA-F]+)", re.IGNORECASE
)


def _scd_satellite_nonzero_retry_details(raw_lines: Sequence[str]) -> List[str]:
    """
    For satellite debug: last section title, non-zero *RetryErr (not *RetryErrCnt*),
    then remaining 0x register lines for the same SwitchLcN in that section.
    """
    out: List[str] = []
    last_heading: Optional[str] = None
    time_line_for_section: Optional[str] = None
    for idx, ln in enumerate(raw_lines):
        st = ln.strip()
        if "register values:" in st.lower():
            last_heading = st
            time_line_for_section = None
            continue
        if (
            last_heading is not None
            and time_line_for_section is None
            and st
            and "collection time" in st.lower()
        ):
            time_line_for_section = st
            continue
        if "RetryErr" not in ln:
            continue
        m = _SCD_SATELLITE_RETRY_ERR_RE.search(ln)
        if not m:
            continue
        try:
            v = int(m.group(1), 16)
        except ValueError:
            continue
        if v == 0:
            continue
        lc_m = re.search(r"SwitchLc(\d+)RetryErr\s*[=:]", ln, re.IGNORECASE)
        lc = lc_m.group(1) if lc_m else None
        if last_heading and (not out or out[-1] != last_heading):
            out.append(last_heading)
            if time_line_for_section:
                out.append(time_line_for_section)
        out.append(ln.rstrip("\r\n"))
        if lc is None:
            continue
        needle = f"SwitchLc{lc}"
        for j in range(idx + 1, len(raw_lines)):
            nxt = raw_lines[j]
            nst = nxt.strip()
            if "register values:" in nst.lower():
                break
            if not nst.startswith("0x"):
                continue
            other = re.search(r"SwitchLc(\d+)", nst)
            if other is not None and other.group(1) != lc:
                break
            if needle in nst:
                out.append(nxt.rstrip("\r\n"))
    return out


@register_check
class ScdSatelliteRetryErrCheck(BaseCheck):
    name = "scd_satellite_retry_error"
    category = "hardware"
    supported_platforms = ("7368", "7289", "7388")
    required_commands = ("show platform scd satellite debug",)

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
        detail_lines = _scd_satellite_nonzero_retry_details(lines)
        if detail_lines:
            return [
                CheckResult(
                    name=self.name,
                    category=self.category,
                    severity=Severity.WARN,
                    summary="Non-zero RetryErr in satellite debug.",
                    details=detail_lines,
                )
            ]
        return [
            CheckResult(
                name=self.name,
                category=self.category,
                severity=Severity.OK,
                summary="RetryErr is zero for all matched satellite debug entries.",
            )
        ]


# Platform-specific configurable patterns for running-config checks
# To add/modify/delete patterns, simply edit the corresponding platform list without changing the core logic
# Format: {platform_series: [list of patterns to check]}
RUNNING_CONFIG_PATTERNS_BY_PLATFORM = {
    "78xx": [
        "ip hardware fib next-hop arp dedicated",
        "platform sand lag hardware-only",
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
    required_commands = ("show running-config sanitized",)

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
    required_commands = ("show inventory",)

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
        serial_number=ctx.serial_number,
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
        "platform_fap_counters_nz": "show platform fap counters | nz",
        "redundancy_status": "show redundancy status",
        "pci_errors": "show pci",
        "agent_logs_crash": "show agent logs crash",
        "power_input_voltage": "show system environment power detail",
        "logging_threshold_errors": "show logging threshold errors",
        "interfaces_queue_drops": "show interfaces counters queue drops",
        "cpu_queue_drops": "show cpu counters queue",
        "interfaces_discards": "show interfaces counters discards",
        "interfaces_errors": "show interfaces counters errors",
        "interfaces_errdisabled": "show interfaces status errdisabled",
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
    def _selected_check_names() -> Optional[List[str]]:
        if show_checks_in_brief is None:
            return None
        if len(show_checks_in_brief) == 0:
            return []
        return list(show_checks_in_brief)

    def _is_selected(result_name: str, selected: Sequence[str]) -> bool:
        for n in selected:
            if result_name == n or result_name.startswith(n + "_"):
                return True
        return False

    def _append_raw_or_filtered_output(
        out_lines: List[str],
        r: CheckResult,
        *,
        limit: Optional[int],
    ) -> None:
        """
        Append raw (or filtered) output for a check.
        If limit is None -> full output; else -> only first N lines.
        """
        cmd = r.command or _infer_command_from_check(r)
        if not cmd:
            # Fallback to details when we can't map to a command.
            details = [d for d in r.details if not d.startswith("[DEBUG raw")]
            if not details:
                out_lines.append("  (No output available)")
                return
            snippet = details if limit is None else details[:limit]
            for d in snippet:
                out_lines.append(f"  {d}")
            if limit is not None and len(details) > limit:
                out_lines.append(f"  ... (showing first {limit} line(s))")
            return

        blocks = ctx.get_blocks(cmd)
        if not blocks:
            out_lines.append("  (No output available)")
            return
        raw_lines = blocks[0].lines

        def compute_content_lines() -> List[str]:
            # Reuse the same filtering intent as debug mode.
            if r.name == "fap_fabric_serdes":
                if ctx.platform_series == "78xx":
                    pattern = r"(U--- Ramon|[|]---U Ramon|I---I? Ramon|[|]---I Ramon|[|]--- Ramon|---[|] Ramon)"
                else:
                    pattern = r"(U--- Fe|[|]---U Fe|I---I? Fe|[|]---I Fe|[|]--- Fe|---[|] Fe)"
                return [ln for ln in raw_lines if re.search(pattern, ln)] or [
                    "(No lines matched the pattern)"
                ]

            if r.name == "platform_fap_counters_nz":
                if ctx.platform_series == "75xx":
                    pat = PlatformFapCountersNzCheck.CNTR_75_RE
                    idxs = [i for i, ln in enumerate(raw_lines) if pat.search(ln)]
                else:
                    idxs = [
                        i
                        for i, ln in enumerate(raw_lines)
                        if PlatformFapCountersNzCheck.CNTR_78_RE.search(ln)
                    ]
                enriched = PlatformFapCountersNzCheck._enriched_counter_rows(
                    raw_lines, idxs
                )
                return enriched or ["(No lines matched the pattern)"]

            if r.name == "logging_threshold_errors":
                patterns = LOGGING_THRESHOLD_ERROR_PATTERNS
                matching_lines = []
                for ln in raw_lines:
                    for pat in patterns:
                        if re.search(pat, ln, re.IGNORECASE):
                            matching_lines.append(ln)
                            break
                return matching_lines or ["(No lines matched the patterns)"]

            if r.name == "interfaces_queue_drops":
                # Prefer parsed details (already header + matched lines)
                if r.details:
                    return list(r.details)
                header_line, matched_lines = _parse_queue_drops_output(raw_lines)
                content: List[str] = []
                if header_line:
                    content.append(header_line)
                if matched_lines:
                    content.extend(matched_lines)
                else:
                    content.append("(No matched lines found)")
                return content

            if r.name == "interfaces_errors":
                header_line = None
                header_line_idx = None
                non_zero_lines = []
                for idx, ln in enumerate(raw_lines):
                    stripped = ln.strip()
                    if not stripped:
                        continue
                    if stripped.replace("-", "").replace("|", "").strip() == "":
                        continue
                    parts_lower = [p.lower() for p in stripped.split()]
                    error_keywords = [
                        "error",
                        "crc",
                        "alignment",
                        "fcs",
                        "frame",
                        "overrun",
                        "underrun",
                        "collision",
                    ]
                    if any(keyword in " ".join(parts_lower) for keyword in error_keywords):
                        header_line = stripped
                        header_line_idx = idx
                        break
                if header_line_idx is not None:
                    for ln in raw_lines[header_line_idx + 1 :]:
                        stripped = ln.strip()
                        if not stripped:
                            continue
                        if stripped.replace("-", "").replace("|", "").strip() == "":
                            continue
                        parts = stripped.split()
                        has_non_zero = False
                        for i in range(1, len(parts)):
                            try:
                                val = int(parts[i].replace(",", "").strip())
                                if val != 0:
                                    has_non_zero = True
                                    break
                            except (ValueError, IndexError):
                                continue
                        if has_non_zero:
                            non_zero_lines.append(stripped)
                content: List[str] = []
                if header_line:
                    content.append(header_line)
                if non_zero_lines:
                    content.extend(non_zero_lines)
                else:
                    content.append("(No non-zero error counters found)")
                return content

            if r.name == "hardware_counter_drop":
                header_line = None
                header_line_idx = None
                filtered_lines = []
                for idx, ln in enumerate(raw_lines):
                    stripped = ln.strip()
                    if not stripped:
                        continue
                    if stripped.replace("-", "").replace("|", "").strip() == "":
                        continue
                    if "Last Occurrence" in stripped:
                        header_line = stripped
                        header_line_idx = idx
                        break
                summary_lines = []
                for ln in raw_lines:
                    stripped = ln.strip()
                    if not stripped:
                        continue
                    if (
                        stripped.startswith("Summary:")
                        or "Total Adverse" in stripped
                        or "Total Congestion" in stripped
                    ):
                        summary_lines.append(stripped)
                if header_line_idx is not None:
                    for ln in raw_lines[header_line_idx + 1 :]:
                        stripped = ln.strip()
                        if not stripped:
                            continue
                        if stripped.replace("-", "").replace("|", "").strip() == "":
                            continue
                        if stripped.startswith("A ") or stripped.startswith("C "):
                            filtered_lines.append(stripped)

                # Compute same-day filtered candidates for debugging (do not change output yet)
                _same_day_lines: List[str] = []
                _clock_raw = getattr(ctx, "system_time", None)
                _clock_ok = False
                if _clock_raw:
                    try:
                        _dt_clock = _dt.datetime.strptime(_clock_raw, "%a %b %d %H:%M:%S %Y")
                        _clock_date = _dt_clock.date()
                        _clock_ok = True
                        date_re = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
                        for _ln in filtered_lines:
                            _matches = date_re.findall(_ln)
                            if not _matches:
                                continue
                            _last = _matches[-1]
                            try:
                                _dt_last = _dt.datetime.strptime(_last, "%Y-%m-%d %H:%M:%S")
                                if _dt_last.date() == _clock_date:
                                    _same_day_lines.append(_ln)
                            except Exception:
                                continue
                    except Exception:
                        pass
                content: List[str] = []
                if summary_lines:
                    content.extend(summary_lines)
                    content.append("")
                if header_line:
                    content.append(header_line)
                # Bugfix: Align full output with WARN summary semantics (same-day as show clock).
                # When system time is available and parsed, show same-day A/C rows only.
                _final_lines = _same_day_lines if _clock_ok else filtered_lines
                if _final_lines:
                    content.extend(_final_lines)
                else:
                    content.append("(No A or C type drops found)")
                return content

            if r.name == "scd_satellite_retry_error" and limit is not None:
                fl = _scd_satellite_nonzero_retry_details(raw_lines)
                return fl if fl else ["(No non-zero RetryErr lines)"]

            # Default: no filtering, use full raw output.
            return list(raw_lines)

        content_lines = compute_content_lines()

        # For full output (used by -c override), output all content lines.
        if limit is None:
            out_lines.append(f"[OUTPUT {cmd}]")
            out_lines.append("-" * 80)
            out_lines.extend(content_lines)
            out_lines.append("-" * 80)
            return

        # Preview output (used by verbose/warn-only): first N lines (filtered when applicable).
        snippet = content_lines[:limit]
        if not snippet:
            out_lines.append("  (No output available)")
            return
        for ln in snippet:
            out_lines.append(f"  {ln}")
        if len(content_lines) > limit:
            out_lines.append(f"  ... (showing first {limit} line(s))")

    lines: List[str] = []
    lines.append(f"Source: {ctx.source_id}")
    lines.append("")

    headers = ["Type", "Name", "Value", "Extra"]
    brief_rows: List[List[str]] = [
        ["BRIEF", "Script time", brief.script_time, ""],
        ["BRIEF", "Hostname", brief.hostname or "N/A", ""],
        ["BRIEF", "EOS version", brief.eos_version or "N/A", ""],
        ["BRIEF", "Model", brief.hw_model or "N/A", ""],
        ["BRIEF", "Serial number", brief.serial_number or "N/A", ""],
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

    # Summary mode: one-line output for all checks, no details/raw
    if mode == "summary":
        lines.append("")
        lines.append("Checks summary:")
        lines.append("-" * 80)
        for idx, r in enumerate(results):
            if idx:
                lines.append("")
            lines.append(f"[{r.severity.value}] {r.category}/{r.name}: {r.summary}")
        lines.append("-" * 80)
        return "\n".join(lines)
    
    # Add checks information in brief mode if requested
    selected_names = _selected_check_names()
    if mode == "brief" and selected_names is not None:
        lines.append("")
        if len(selected_names) == 0:
            # No check names specified: show all supported checks list
            lines.append(format_checks_list())
        else:
            # Show specified checks full output (not truncated)
            lines.append("Selected checks (full output):")
            lines.append("=" * 80)
            for check_name in selected_names:
                matching_results: List[CheckResult] = []
                for r in results:
                    if r.name == check_name or r.name.startswith(check_name + "_"):
                        matching_results.append(r)

                if not matching_results:
                    lines.append("")
                    lines.append(f"Check: {check_name} (not found or not executed)")
                    lines.append("-" * 80)
                    continue

                for check_result in matching_results:
                    lines.append("")
                    lines.append(f"[{check_result.severity.value}] {check_result.category}/{check_result.name}: {check_result.summary}")
                    _append_raw_or_filtered_output(lines, check_result, limit=None)
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
            
            # verbose / warn-only (non-debug): show first 10 lines for every result
            if mode in ("verbose", "warn") and not debug:
                selected_for_full = (
                    selected_names is not None
                    and len(selected_names) > 0
                    and _is_selected(r.name, selected_names)
                )
                if selected_for_full:
                    _append_raw_or_filtered_output(lines, r, limit=None)
                else:
                    _append_raw_or_filtered_output(lines, r, limit=10)
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
                    if r.name == "platform_fap_counters_nz":
                        if ctx.platform_series == "75xx":
                            pat = PlatformFapCountersNzCheck.CNTR_75_RE
                            idxs = [i for i, ln in enumerate(raw_lines) if pat.search(ln)]
                        else:
                            idxs = [
                                i
                                for i, ln in enumerate(raw_lines)
                                if PlatformFapCountersNzCheck.CNTR_78_RE.search(ln)
                            ]
                        matching_lines = PlatformFapCountersNzCheck._enriched_counter_rows(
                            raw_lines, idxs
                        )
                        lines.append(f"[DEBUG filtered {cmd}]")
                        lines.append("-" * 80)
                        if matching_lines:
                            for line in matching_lines:
                                lines.append(line)
                        else:
                            lines.append("(No lines matched the pattern)")
                        lines.append("-" * 80)
                    elif r.name == "fap_fabric_serdes":
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
                    elif r.name == "scd_satellite_retry_error":
                        fl = _scd_satellite_nonzero_retry_details(raw_lines)
                        lines.append(f"[DEBUG filtered {cmd}]")
                        lines.append("-" * 80)
                        if fl:
                            for line in fl:
                                lines.append(line)
                        else:
                            lines.append("(No non-zero RetryErr lines)")
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
            "serial_number": brief.serial_number,
            "system_time": brief.system_time,
            "health": brief.health.value,
            "warn_count": brief.warn_count,
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
    # Live-collection fields
    live_creds: Optional["DeviceCredentials"] = None
    live_commands: Optional[List[str]] = None
    live_use_tech_support: bool = False
    live_save_dir: Optional[Path] = None
    live_progress: Optional["LiveProgress"] = None


def load_task_text(task: ProcessingTask) -> str:
    """Load show-tech text for a task (pre-loaded, plain file, archive member, or live device)."""
    if task.text is not None:
        return task.text
    if task.lazy_path is not None:
        return _read_path_maybe_gunzip(task.lazy_path)
    if task.lazy_archive_path is not None and task.lazy_archive_spec is not None:
        return read_text_from_archive_member(task.lazy_archive_path, task.lazy_archive_spec)
    if task.live_creds is not None:
        cmds = task.live_commands or collect_required_commands()
        host = task.live_creds.host
        progress = task.live_progress
        cb: Optional[Callable[[int, int, str], None]] = None
        if progress is not None:
            progress.device_started(host)

            def cb(done: int, total: int, stage: str, _h: str = host,
                   _p: "LiveProgress" = progress) -> None:
                _p.device_cmd(_h, done, total, stage)

        try:
            text = fetch_device_output(
                task.live_creds,
                cmds,
                use_tech_support=task.live_use_tech_support,
                progress_cb=cb,
            )
        finally:
            if progress is not None:
                progress.device_stage(host, "parsing")
        if task.live_save_dir is not None:
            _save_live_collection(task.live_save_dir, host, text)
        return text
    raise ValueError(
        f"Cannot load text for task {task.source_id}: missing lazy-load fields"
    )


def _save_live_collection(directory: Path, host: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_host = re.sub(r"[^A-Za-z0-9_.\-]+", "_", host) or "device"
    target = directory / f"{safe_host}-show-tech-{stamp}.txt"
    target.write_text(text, encoding="utf-8")
    LOG.info("Saved live collection to %s", target)


def _match_command_blocks(blocks: List[CommandBlock], needle: str) -> List[CommandBlock]:
    """Exact normalized command match first; otherwise all blocks whose command starts with needle."""
    n = needle.lower().strip()
    if not n:
        return []
    exact = [b for b in blocks if b.command == n]
    if exact:
        return exact
    return [b for b in blocks if b.command.startswith(n)]


def run_showtech_command_extract(
    tasks: Sequence[ProcessingTask],
    list_commands: bool,
    raw_command: Optional[str],
) -> Tuple[int, str]:
    """
    Build command list (-L) or raw section bodies (-r) for each task.
    Returns (exit_code, text). exit_code is 1 if raw mode had no match in any input.
    """
    parser = TechSupportParser()
    exit_code = 0
    chunks: List[str] = []

    for task in tasks:
        text = load_task_text(task)
        blocks = parser.parse(text)
        del text
        if task.text is not None:
            task.text = None

        chunks.append(f"# source: {task.source_id}")
        if list_commands:
            for b in blocks:
                chunks.append(b.command)
        else:
            assert raw_command is not None
            matched = _match_command_blocks(blocks, raw_command)
            if not matched:
                chunks.append("(no matching command section)")
                exit_code = 1
                print(
                    f"warning: no section matching {raw_command!r} in {task.source_id}",
                    file=sys.stderr,
                )
            else:
                for i, blk in enumerate(matched, start=1):
                    if len(matched) > 1:
                        chunks.append(
                            f"# --- match {i}/{len(matched)}: {blk.command} ---"
                        )
                    chunks.append("\n".join(blk.lines))
        chunks.append("")

    return exit_code, "\n".join(chunks).rstrip()


# ---------------------------------------------------------------------------
# Interactive show-tech CLI (--cli)
# ---------------------------------------------------------------------------

_SHOWTECH_CLI_PIPE_RE = re.compile(r"\s*\|\s*(grep|include)\s+(.+)$", re.IGNORECASE)

@dataclass
class _ShowTechTrieNode:
    children: Dict[str, "_ShowTechTrieNode"] = field(default_factory=dict)
    blocks_here: List["CommandBlock"] = field(default_factory=list)


class _ShowTechCommandTrie:
    """Word-level trie over section commands for EOS-style abbreviation and '?' help."""

    def __init__(self, blocks: Sequence["CommandBlock"]) -> None:
        self._all_blocks = list(blocks)
        self._root = _ShowTechTrieNode()
        self._insert_all()

    def _insert_all(self) -> None:
        for blk in self._all_blocks:
            tokens = blk.command.split()
            if not tokens:
                continue
            node = self._root
            for t in tokens:
                if t not in node.children:
                    node.children[t] = _ShowTechTrieNode()
                node = node.children[t]
            node.blocks_here.append(blk)

    @staticmethod
    def _match_unique_child(node: _ShowTechTrieNode, user_tok: str) -> Tuple[Optional[str], List[str]]:
        """Return (canonical_key, []) if unique; (None, []) if none; (None, candidates) if ambiguous."""
        u = user_tok.lower()
        # Exact child keyword (case-insensitive) wins over prefix match, so ``ip`` is not
        # treated as an abbreviation of ``ipv6`` when both exist under ``show``.
        exact = [k for k in node.children if k.lower() == u]
        if len(exact) == 1:
            return exact[0], []
        cand = [k for k in node.children if k.lower().startswith(u)]
        if len(cand) == 1:
            return cand[0], []
        if len(cand) == 0:
            return None, []
        return None, sorted(cand)

    def _walk_prefix(self, prefix_tokens: List[str]) -> Tuple[Optional[_ShowTechTrieNode], Optional[str]]:
        """
        Walk trie using abbreviation resolution for each user token.
        Returns (node, error_message). node is set on success.
        """
        node = self._root
        for i, ut in enumerate(prefix_tokens):
            if not ut:
                return None, "Empty token in command."
            key, amb = self._match_unique_child(node, ut)
            if amb:
                ctx = " ".join(prefix_tokens[:i] + [ut])
                lines = "\n".join(f"  {c}" for c in amb)
                return None, f"Ambiguous token {ut!r} after {ctx!r}:\n{lines}"
            if key is None:
                ctx = " ".join(prefix_tokens[: i + 1])
                nxt = sorted(node.children.keys())
                hint = "\n".join(f"  {c}" for c in nxt) if nxt else "  (no subcommands)"
                return None, f"Unknown token {ut!r} in {ctx!r}. Next level options:\n{hint}"
            node = node.children[key]
        return node, None

    def help_candidates(self, prefix_tokens: List[str], partial: str) -> Tuple[Optional[List[str]], Optional[str]]:
        """List next-level command keywords (full spelling) after optional prefix filter."""
        node, err = self._walk_prefix(prefix_tokens)
        if err:
            return None, err
        assert node is not None
        p = partial.lower()
        keys = sorted(
            k for k in node.children if not p or k.lower().startswith(p)
        )
        return keys, None

    def resolve_blocks(self, tokens: List[str]) -> Tuple[Optional[List["CommandBlock"]], Optional[str]]:
        if not tokens:
            return None, "Empty command."
        node, err = self._walk_prefix(tokens)
        if err:
            return None, err
        assert node is not None
        # Strict tree semantics per requirements:
        # If a command node has both (1) a runnable section here (blocks_here) and
        # (2) multiple child keywords, we must NOT execute the parent command.
        # The user must refine the command further (or use '?').
        if node.blocks_here:
            if len(node.children) > 1:
                nxt = sorted(node.children.keys())
                hint = "\n".join(f"  {c}" for c in nxt)
                return None, (
                    "Incomplete command; type '?' to list next keywords:\n" + hint
                )
            return node.blocks_here, None
        nxt = sorted(node.children.keys())
        hint = "\n".join(f"  {c}" for c in nxt)
        return None, f"Incomplete command; type '?' to list next keywords:\n{hint}"

    def expand_cli_line(self, line: str) -> str:
        """
        EOS-style: expand each token to the canonical child keyword when the abbreviation
        is unique; on ambiguous or unknown token, leave that token and the rest unchanged.
        """
        line = line.strip()
        if not line:
            return ""
        parts = line.split()
        node = self._root
        out: List[str] = []
        i = 0
        while i < len(parts):
            tok = parts[i]
            key, amb = self._match_unique_child(node, tok)
            if amb or key is None:
                out.append(tok)
                out.extend(parts[i + 1 :])
                break
            out.append(key)
            node = node.children[key]
            i += 1
        return " ".join(out)


def _cli_expand_full_input_line(trie: Optional[_ShowTechCommandTrie], s: str) -> str:
    """Expand abbreviations for the command part only (before '|'); preserve '?' suffix and pipe tail."""
    if not trie:
        return s.strip()
    s = s.rstrip("\r\n")
    # Only treat `|` as a "pipe tail" splitter for `| grep/include ...`.
    # For show-tech commands that literally contain `|` as a keyword (e.g. `... recent | nz`),
    # keep it in the command token stream so abbreviation/Tab completion works naturally.
    m_pipe = _SHOWTECH_CLI_PIPE_RE.search(s)
    if m_pipe:
        left = s[: m_pipe.start()].rstrip()
        right = s[m_pipe.start() :]
    else:
        left, right = s, ""

    q = ""
    if left.endswith("?"):
        # Preserve whether user typed a space before '?'.
        # "show ip ?" should be parsed as last token "?" (partial == ''),
        # but "show ip?" should be parsed as last token ending with '?' (partial == 'ip').
        had_space_before_q = len(left) >= 2 and left[-2].isspace()
        q = " ?" if had_space_before_q else "?"
        left = left[:-1].rstrip()

    expanded_left = trie.expand_cli_line(left) if left.strip() else left.rstrip()

    merged = expanded_left + q
    return _cli_join_cmd_pipe(merged, right).strip()


def _cli_join_cmd_pipe(left: str, right: str) -> str:
    """Join expanded command prefix with '| grep' / '| include' tail, spacing like EOS."""
    merged = left.rstrip()
    if right:
        if (
            merged
            and right.lstrip().startswith("|")
            and not merged.endswith(" ")
            and not right.startswith(" ")
        ):
            merged += " "
        merged += right
    return merged


def _cli_tab_complete_line(trie: _ShowTechCommandTrie, core: str) -> str:
    """
    Tab: first apply unique abbrev expansion; if unchanged, complete the last token or
    print candidate list (root, next-level keywords, or subcommands when last token is exact).
    """
    exp = _cli_expand_full_input_line(trie, core)
    if exp != core:
        m0 = _SHOWTECH_CLI_PIPE_RE.search(core)
        idx0 = m0.start() if m0 else -1
        right0 = core[idx0:] if idx0 >= 0 else ""
        if not right0.strip():
            e = exp.rstrip()
            ret = (e + " ") if e else exp
        else:
            ret = exp.strip()
        return ret

    m = _SHOWTECH_CLI_PIPE_RE.search(core)
    idx = m.start() if m else -1
    if idx >= 0:
        left = core[:idx].rstrip()
        right = core[idx:]
    else:
        left = core.rstrip()
        right = ""

    parts = left.split()

    def merge(new_left: str, *, space_after: bool = False) -> str:
        merged = _cli_join_cmd_pipe(new_left.rstrip(), right)
        if space_after and (not right or not right.strip()):
            s = merged.rstrip()
            return (s + " ") if s else s
        return merged.strip()

    if not parts:
        keys, err = trie.help_candidates([], "")
        if err:
            print(f"\n{err}", flush=True)
            return core
        if not keys:
            return core
        if len(keys) == 1:
            ret0 = merge(keys[0], space_after=True)
            return ret0
        print("\n" + "\n".join(keys), flush=True)
        return core

    pref, last = parts[:-1], parts[-1]
    keys, err = trie.help_candidates(pref, last)
    if err:
        print(f"\n{err}", flush=True)
        return core

    # Match execution semantics: a token that equals a full child keyword (e.g. ``ip``)
    # is not ambiguous with longer siblings (``ipv6``). help_candidates keeps prefix
    # filtering for ``?``/refine; Tab narrows here so ``show ip`` + Tab lists ``ip``'s subtree.
    if len(keys) > 1 and last:
        exact = [k for k in keys if k.lower() == last.lower()]
        if len(exact) == 1:
            keys = exact

    if len(keys) == 1:
        nk = keys[0]
        if nk != last:
            ret1 = merge(" ".join(pref + [nk]), space_after=True)
            return ret1
        keys2, err2 = trie.help_candidates(parts, "")
        if err2:
            print(f"\n{err2}", flush=True)
            return core
        if keys2:
            if len(keys2) == 1:
                ret2 = merge(" ".join(parts + [keys2[0]]), space_after=True)
                return ret2
            print("\n" + "\n".join(keys2), flush=True)
            # If `last` is an exact keyword and we listed its children (multiple
            # next-level options), keep a trailing space so the user can type
            # the next keyword directly without pressing space manually.
            ret3 = merge(" ".join(parts), space_after=True)
            return ret3
        return core

    if len(keys) > 1:
        print("\n" + "\n".join(keys), flush=True)
        return core

    keys2, err2 = trie.help_candidates(parts, "")
    if not err2 and keys2:
        print("\n" + "\n".join(keys2), flush=True)
    return core


def _parse_showtech_cli_pipe(
    line: str,
) -> Tuple[str, Optional[str], Optional[str]]:
    """Split `... | grep ...` / `| include ...` from the rest (case-insensitive).

    Returns:
      (cmd_part, pipe_pattern, pipe_kind) where pipe_kind is "grep" or "include".
    """
    m = _SHOWTECH_CLI_PIPE_RE.search(line)
    if not m:
        # Normalize bare pipe tokens for command resolution so `|nz` behaves like `| nz`.
        # Do NOT do this for `| grep/include ...` because the pattern portion may contain '|'.
        if "|" in line:
            normalized = re.sub(r"\s*\|\s*", " | ", line)
            normalized = " ".join(normalized.strip().split())
            # If user typed repeated pipes (e.g. `... | | |`), collapse them and drop trailing pipes.
            # This avoids producing an "unknown token '|'" error while still guiding the user.
            parts = normalized.split()
            if "|" in parts:
                collapsed: List[str] = []
                for p in parts:
                    if p == "|" and collapsed and collapsed[-1] == "|":
                        continue
                    collapsed.append(p)
                while collapsed and collapsed[-1] == "|":
                    collapsed.pop()
                normalized2 = " ".join(collapsed)
            else:
                normalized2 = normalized
            return normalized2, None, None
        return line.strip(), None, None
    cmd = line[: m.start()].strip()
    kind = m.group(1).lower()
    pat = m.group(2).strip()
    return cmd, pat if pat else None, kind


def _parse_showtech_cli_help(cmd_part: str) -> Optional[Tuple[List[str], str]]:
    """
    If cmd_part asks for help via ASCII '?', return (prefix_tokens, partial_filter) else None.
    partial_filter is '' to list all next-level words at the node after prefix_tokens.
    """
    # Only treat explicit '?' as a help request. Empty input or normal commands
    # (including a line that's only a pipe tail like "| grep ...") must NOT enter
    # help mode implicitly.
    if "?" not in cmd_part:
        return None
    parts = cmd_part.split()
    if not parts:
        return None
    last = parts[-1]
    if last == "?":
        return parts[:-1], ""
    if last.endswith("?"):
        # e.g. "show inter?" -> walk ["show"], filter next level by "inter"
        return parts[:-1], last[:-1]
    return None


def _cli_refine_help_prefix(
    trie: _ShowTechCommandTrie,
    prefix_tokens: List[str],
    partial: str,
) -> Tuple[List[str], str]:
    """
    For suffix ``... word?`` (no space before ``?``): same as ``... word ?`` when we can
    resolve ``word`` into a single trie step.

    - If ``word`` equals a full child keyword (case-insensitive) among prefix matches,
      descend into that node and list *its* sub-keys (e.g. ``show ip?`` → under ``ip``,
      not ``ip`` vs ``ipv6`` at ``show``).
    - If exactly one child matches ``word`` as an abbreviation (unique prefix), descend.
    - If several children match and ``word`` is not an exact keyword name, stay and list
      those matches (e.g. ``show ip r?`` → ``rip`` / ``route``).

    Applies at any depth (``sh ver?``, ``show ipv6 neigh?``, etc.).
    """
    if not partial:
        return list(prefix_tokens), partial
    keys_probe, err_probe = trie.help_candidates(prefix_tokens, partial)
    if err_probe or not keys_probe:
        return list(prefix_tokens), partial
    # '?' help semantics:
    # - Only descend when the typed partial uniquely resolves to exactly one keyword.
    # - If multiple candidates match (e.g. "ip" matches both "ip" and "ipv6"), keep
    #   the current level so we list all matching options for that partial.
    if len(keys_probe) == 1:
        ret = list(prefix_tokens) + [keys_probe[0]], ""
        return ret
    ret = list(prefix_tokens), partial
    return ret


def _cli_resume_line_after_help(raw: str) -> str:
    """
    Command typed before '?' (no trailing help marker), for pre-filling the next prompt.
    Uses the section before '| grep' / '| include' only.
    """
    cmd_part, _, _ = _parse_showtech_cli_pipe(raw.strip())
    if "?" not in cmd_part:
        return ""
    base = cmd_part.rsplit("?", 1)[0]
    had_space = base.endswith(" ")
    trimmed = base.rstrip()
    return trimmed


def _showtech_cli_apply_grep(
    lines: List[str],
    pattern: Optional[str],
    pipe_kind: Optional[str],
) -> List[str]:
    if pattern is None:
        return lines
    kind = (pipe_kind or "grep").lower()

    # If grep is available, delegate to it so we support real grep behavior
    # (regex, flags like -E/-v/-w, etc.).
    grep_cmd: List[str]
    try:
        args = shlex.split(pattern)
        if kind == "include":
            grep_cmd = ["grep", "-F", *args]
        else:
            grep_cmd = ["grep", *args]

        input_bytes = ("\n".join(lines) + "\n").encode("utf-8", errors="replace")
        proc = subprocess.run(
            grep_cmd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        # grep: 0 = matches, 1 = no matches, >1 = error
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", errors="replace").splitlines()
        if proc.returncode == 1:
            return []

        # Error path: show grep stderr so the user can fix regex/flags.
        err_txt = proc.stderr.decode("utf-8", errors="replace").strip()
        if err_txt:
            print(err_txt)
        return []
    except FileNotFoundError:
        # Fallback when grep binary isn't available: approximate substring match.
        # Real grep is case-sensitive by default and only case-insensitive with `-i`.
        pl_raw = pattern.strip()
        parts = pl_raw.split()
        ignore_case = False
        while parts and parts[0] in {"-i", "--ignore-case"}:
            ignore_case = True
            parts.pop(0)
        if not parts:
            return []
        pl = " ".join(parts)
        if ignore_case:
            pl = pl.lower()
            return [ln for ln in lines if pl in ln.lower()]
        return [ln for ln in lines if pl in ln]
    except Exception:
        # Never break the CLI due to grep/filtering issues.
        return []


def _showtech_cli_paginate(lines: List[str]) -> None:
    if not lines:
        print("(no output)")
        return
    try:
        h = max(os.get_terminal_size().lines - 2, 8)
    except OSError:
        h = 22
    if not sys.stdin.isatty():
        print("\n".join(lines))
        return
    i = 0
    n = len(lines)
    while i < n:
        end = min(i + h, n)
        print("\n".join(lines[i:end]))
        i = end
        # No pager hint on the final page (or when output fits one screen).
        if end >= n:
            break
        print("[Space=next q=quit other=next] ", end="", flush=True)
        ch = "\n"
        if sys.platform == "win32":
            ch = sys.stdin.read(1)
        else:
            try:
                import termios
                import tty

                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                try:
                    tty.setcbreak(fd)
                    ch = sys.stdin.read(1)
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except (ImportError, OSError, AttributeError):
                ch = sys.stdin.read(1)
        if ch in ("q", "Q"):
            print("\r\033[2K--- pager quit ---")
            break
        print("\r\033[2K", end="", flush=True)


def _format_showtech_cli_matches(matched: List["CommandBlock"]) -> List[str]:
    out: List[str] = []
    for i, blk in enumerate(matched, start=1):
        if len(matched) > 1:
            out.append(f"# --- match {i}/{len(matched)}: {blk.command} ---")
        out.extend(blk.lines)
    return out


def _showtech_cli_posix_tty_line(
    prompt: str,
    hist: List[str],
    initial: Optional[str] = None,
    trie: Optional[_ShowTechCommandTrie] = None,
) -> str:
    """
    Read one line on a POSIX TTY with local echo, Backspace, Up/Down history,
    and immediate submit when ASCII '?' is typed (no extra Enter).

    ``initial`` seeds the buffer (e.g. after '?' help so the command prefix is kept).
    Space / Enter expand unique abbreviations to full keywords (EOS-style).
    Tab also expands; if nothing extra to expand, completes the last token or lists candidates.
    """
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    hi = len(hist)
    buf: List[str] = list(initial) if initial else []

    def redraw() -> None:
        sys.stdout.write("\r\033[K" + prompt + "".join(buf))
        sys.stdout.flush()

    def finish(ok: str) -> str:
        sys.stdout.write("\n")
        sys.stdout.flush()
        return ok.strip()

    try:
        tty.setcbreak(fd)
        redraw()
        while True:
            ch = sys.stdin.read(1)
            if ch == "":
                raise EOFError
            if ch in ("\n", "\r"):
                line = "".join(buf)
                exp = _cli_expand_full_input_line(trie, line) if trie else line.strip()
                return finish(exp)
            if ch in ("\x7f", "\x08"):
                if buf:
                    buf.pop()
                    redraw()
                continue
            if ch == "\x04" and not buf:
                raise EOFError
            if ch == "\x03":
                buf.clear()
                sys.stdout.write("^C\n")
                sys.stdout.flush()
                return finish("")
            if ch == "\x1b":
                # Arrow keys and other escapes — read the full sequence in one go.
                # If we return early after only ESC, '[' and 'A' leak into the line as literals
                # (common with IDE terminals that delay bytes after ESC).
                def hist_up() -> None:
                    nonlocal hi, buf
                    if not hist:
                        return
                    if hi == len(hist):
                        hi = len(hist) - 1
                    elif hi > 0:
                        hi -= 1
                    buf = list(hist[hi])
                    redraw()

                def hist_down() -> None:
                    nonlocal hi, buf
                    if not hist:
                        return
                    if hi < len(hist) - 1:
                        hi += 1
                        buf = list(hist[hi])
                    elif hi == len(hist) - 1:
                        hi = len(hist)
                        buf = []
                    redraw()

                # Robust escape reader:
                # 1) Wait up to ESC_TOTAL for the complete arrow sequence.
                # 2) Interpret if it's an up/down arrow.
                # 3) Drain for a short window to discard any leftover bytes (e.g. '['/'A'/'B')
                #    that otherwise leak into printable token handling.
                import time
                parts: List[str] = [ch]
                MAX_ESC_BYTES = 8
                # Keep ESC handling fast. If we fail to assemble the full arrow
                # sequence due to terminal timing, the printable-path fallback
                # below will still trigger hist_up/hist_down.
                ESC_TOTAL = 0.06
                esc_deadline = time.monotonic() + ESC_TOTAL
                while len(parts) < MAX_ESC_BYTES and time.monotonic() < esc_deadline:
                    remaining = esc_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if not select.select([fd], [], [], min(0.01, remaining))[0]:
                        continue
                    c = sys.stdin.read(1)
                    if not c:
                        break
                    parts.append(c)
                    if len(parts) == 2 and c not in "[O":
                        break
                    if len(parts) >= 3:
                        # For our purposes, arrow keys have fixed last byte A/B.
                        if parts[1] == "O" and parts[2] in "AB":
                            break
                        if parts[1] == "[" and parts[2] in "AB":
                            break

                seq = "".join(parts)

                # NOTE: We intentionally do not "wait-more" here. Any leaked
                # '[' + 'A'/'B' fragments will be handled in the printable-path
                # fallback so Up/Down still works and remains responsive.

                action: Optional[str] = None
                if seq.startswith("\x1bO") and len(seq) >= 3:
                    if seq[2] == "A":
                        hist_up()
                        action = "up"
                    elif seq[2] == "B":
                        hist_down()
                        action = "down"
                elif seq.startswith("\x1b[") and len(seq) >= 3:
                    if seq[2] == "A":
                        hist_up()
                        action = "up"
                    elif seq[2] == "B":
                        hist_down()
                        action = "down"

                if len(seq) == 1:
                    continue
                continue
            if ch == " ":
                core = "".join(buf).rstrip()
                exp = _cli_expand_full_input_line(trie, core) if trie else core
                buf = list(exp + " ")
                redraw()
                continue
            if ch == "\t":
                if not trie:
                    continue
                before = "".join(buf)
                had_trailing_space = before.endswith(" ")
                core = before.rstrip()
                new_line = _cli_tab_complete_line(trie, core)
                buf = list(new_line)
                redraw()
                continue
            # Printable / UTF-8 continuation handled by read(1) one code point in text mode
            if ch.isprintable():
                # If the terminal leaked an unfinished arrow escape as literal
                # characters (typically `[` followed by `A`/`B`), consume those
                # fragments so they don't become a command token.
                if ch in ("A", "B") and buf and buf[-1] == "[":
                    # Treat leaked arrow fragments as a real up/down key.
                    buf.pop()
                    if ch == "A":
                        hist_up()
                    else:
                        hist_down()
                    redraw()
                    continue
                buf.append(ch)
                if ch == "?":
                    line = "".join(buf)
                    exp = (
                        _cli_expand_full_input_line(trie, line) if trie else line.strip()
                    )
                    return finish(exp)
                redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _showtech_cli_read_line(
    display: str,
    hist: List[str],
    initial: Optional[str] = None,
    trie: Optional[_ShowTechCommandTrie] = None,
) -> str:
    """TTY-aware line read; POSIX uses custom reader so '?' submits without relying on readline."""
    prompt = f"{display}> "
    if (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or sys.platform == "win32"
    ):
        try:
            if initial:
                sys.stdout.write(prompt + initial)
                sys.stdout.flush()
                rest = sys.stdin.readline()
                combined = (initial + rest).strip()
            else:
                combined = input(prompt).strip()
            return _cli_expand_full_input_line(trie, combined)
        except EOFError:
            raise
    try:
        return _showtech_cli_posix_tty_line(prompt, hist, initial, trie)
    except (OSError, AttributeError, ValueError, ImportError):
        if initial:
            try:
                sys.stdout.write(prompt + initial)
                sys.stdout.flush()
                rest = sys.stdin.readline()
                combined = (initial + rest).strip()
            except EOFError:
                raise
        else:
            combined = input(prompt).strip()
        return _cli_expand_full_input_line(trie, combined)


def _populate_showtech_cli_hostname_ctx(
    source_id: str, blocks: Sequence["CommandBlock"]
) -> TechSupportContext:
    """
    Same hostname-related parsing as run_all_checks (show version + clock + running-config).
    """
    ctx = TechSupportContext(source_id, list(blocks))
    parse_show_version(ctx)
    parse_show_clock(ctx)
    populate_hostname_from_running_config(ctx)
    return ctx


def run_interactive_showtech_cli(
    blocks: Sequence["CommandBlock"],
    source_id: str,
) -> None:
    cli_ctx = _populate_showtech_cli_hostname_ctx(source_id, blocks)
    trie = _ShowTechCommandTrie(blocks)
    hn = cli_ctx.hostname
    if hn:
        disp = hn if len(hn) <= 56 else hn[:53] + "..."
    else:
        short = Path(source_id).name
        disp = short if len(short) <= 56 else "..." + short[-53:]
    hist: List[str] = []
    print(
        "Interactive show-tech CLI (first bundle). Commands: EOS-style abbreviations; "
        "'?' or 'word?' for next keywords (typing ? submits the line on POSIX TTY); "
        "Space/Enter/Tab expand unique abbreviations (Tab also lists candidates); "
        "'| grep PAT' / '| include PAT'; Up/Down recall prior commands; exit/quit/^D to leave."
    )
    pending_initial: Optional[str] = None
    while True:
        try:
            init_line = pending_initial
            pending_initial = None
            raw = _showtech_cli_read_line(
                disp, hist, init_line if init_line else None, trie
            )
        except EOFError:
            print()
            break
        if not raw:
            continue
        low = raw.lower()
        if low in ("exit", "quit", "q"):
            break
        append_hist: Optional[str] = None
        try:
            if low in ("help", "?"):
                print(
                    "Examples:  show ?   show ver   sh ver|grep Arista\n"
                    "Pagination: Space = next page, q = stop output.\n"
                    "Line editing: Up/Down history; ASCII ? submits the line on POSIX TTY.\n"
                    "Space/Enter/Tab expand unique token abbreviations; Tab lists next keywords if needed.\n"
                    "After '?' help the command prefix is kept on the next line."
                )
                continue

            cmd_part, pipe_pat, pipe_kind = _parse_showtech_cli_pipe(raw)
            help_spec = _parse_showtech_cli_help(cmd_part)

            if help_spec is not None:
                prefix_tokens, partial = help_spec
                # Keep original values for debug visibility.
                orig_prefix_tokens, orig_partial = list(prefix_tokens), partial
                prefix_tokens, partial = _cli_refine_help_prefix(
                    trie, list(prefix_tokens), partial
                )
                # List next-level keywords under the refined trie prefix using the
                # (possibly reduced) partial filter.
                keys, err = trie.help_candidates(prefix_tokens, partial)
                if err:
                    print(err)
                    resume_err = _cli_resume_line_after_help(raw)
                    if resume_err:
                        pending_initial = resume_err
                    continue
                assert keys is not None
                if not keys:
                    print("(no matching next-level commands)")
                else:
                    print("\n".join(keys))
                print()
                # For '?' help, we prefill the next prompt based on the refined
                # prefix_tokens/partial we just computed (not on the raw string).
                # When partial == '' (i.e. the current token is unambiguous and fully
                # resolved), we must append a trailing space so the user can
                # immediately continue typing the next keyword.
                if partial == "":
                    resume = (" ".join(prefix_tokens) + " ") if prefix_tokens else ""
                else:
                    resume = " ".join(prefix_tokens + [partial]) if prefix_tokens else partial

                pending_initial = resume
                continue

            tokens = cmd_part.split()
            matched, err = trie.resolve_blocks(tokens)
            ambiguous = bool(err and err.startswith("Ambiguous"))
            if ambiguous:
                print(err)
                continue
            if err:
                # Strict tree semantics: if the trie says "incomplete" / "unknown token",
                # we must not fallback to loose prefix execution.
                if err.startswith("Incomplete command"):
                    # Record incomplete commands too, so Up/Down can find them.
                    append_hist = raw
                print(err)
                continue
            if not matched:
                print("No match.")
                continue

            # Requirement: history should only record commands that resolve to
            # runnable leaf nodes (no further trie children).
            node, err2 = trie._walk_prefix(tokens)
            is_leaf = bool(node is not None and not node.children)
            if err2:
                is_leaf = False

            body = _format_showtech_cli_matches(matched)
            body = _showtech_cli_apply_grep(body, pipe_pat, pipe_kind)
            _showtech_cli_paginate(body)
            if is_leaf:
                # Only record successful leaf-node commands.
                append_hist = raw
        finally:
            if append_hist is not None:
                hist.append(append_hist)
                if len(hist) > 500:
                    hist[:] = hist[-500:]
                # Note: history only records runnable leaf-node commands.


def process_single_task(task: ProcessingTask) -> Tuple[str, str]:
    """
    Process a single file task and return (source_id, report).
    This function is designed to be called in parallel.
    Supports both pre-loaded text and lazy-loading modes.
    """
    text = task.text
    try:
        # Lazy-load text if needed (file, archive, or live device collection)
        if text is None:
            text = load_task_text(task)

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


def _load_inventory_file(path: Path) -> List[Dict[str, object]]:
    """Parse a device inventory file. Tries JSON first, then YAML.

    Returns a list of dicts: [{host, user?, password?, port?, transport?, ...}].
    """
    text = path.read_text(encoding="utf-8")
    import json as _json
    try:
        data = _json.loads(text)
    except Exception:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ValueError(
                f"Inventory {path} is not valid JSON and PyYAML is not installed; "
                f"install pyyaml or convert to JSON"
            ) from exc
        data = yaml.safe_load(text)
    if isinstance(data, dict) and "devices" in data:
        data = data["devices"]
    if not isinstance(data, list):
        raise ValueError(f"Inventory {path} must be a list (or {{devices: [...]}})")
    return data  # type: ignore[return-value]


def _resolve_password(args: argparse.Namespace, host: str, inventory_pw: Optional[str]) -> str:
    """Apply password precedence: CLI > env EOS_PASSWORD > inventory > getpass."""
    if args.password:
        return args.password
    env_pw = os.environ.get("EOS_PASSWORD")
    if env_pw:
        return env_pw
    if inventory_pw:
        return inventory_pw
    import getpass
    return getpass.getpass(f"Password for {args.user or 'admin'}@{host}: ")


def collect_live_tasks(args: argparse.Namespace) -> List[ProcessingTask]:
    """Build ProcessingTasks for live device collection from CLI args + inventory."""
    entries: List[Dict[str, object]] = []
    if args.inventory:
        entries.extend(_load_inventory_file(Path(args.inventory)))
    for host in args.paths or []:
        entries.append({"host": host})

    if not entries:
        return []

    save_dir = Path(args.save) if args.save else None
    commands = collect_required_commands()

    tasks: List[ProcessingTask] = []
    for entry in entries:
        host = str(entry.get("host", "")).strip()
        if not host:
            LOG.warning("Inventory entry missing 'host', skipping: %r", entry)
            continue
        user = str(entry.get("user") or args.user or "admin")
        inv_pw = entry.get("password")
        inv_pw_str = str(inv_pw) if inv_pw is not None else None
        password = _resolve_password(args, host, inv_pw_str)
        port_val = entry.get("port") if entry.get("port") is not None else args.port
        try:
            port = int(port_val) if port_val is not None else None
        except (TypeError, ValueError):
            port = None
        transport = str(entry.get("transport") or args.transport)
        use_https = not args.http
        verify_tls = not args.insecure

        creds = DeviceCredentials(
            host=host,
            user=user,
            password=password,
            port=port,
            transport=transport,
            use_https=use_https,
            verify_tls=verify_tls,
        )
        tasks.append(ProcessingTask(
            source_id=f"live://{host}",
            text=None,
            mode=args.mode,
            as_json=args.json,
            debug=args.debug,
            show_checks_in_brief=args.show_checks_in_brief,
            skip_checks=args.skip_checks,
            skip_categories=args.skip_categories,
            live_creds=creds,
            live_commands=commands,
            live_use_tech_support=args.use_tech_support,
            live_save_dir=save_dir,
        ))
    return tasks


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
                        text = _read_path_maybe_gunzip(f)
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
                        text = _read_path_maybe_gunzip(path)
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
    
    # Explicitly release large objects to help garbage collection.
    del blocks
    del ctx
    del results
    del brief
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
    if not args.paths and not getattr(args, "live", False) and not getattr(args, "inventory", None):
        print("error: the following arguments are required: PATH (unless using --list-checks)", file=sys.stderr)
        sys.exit(2)

    low_memory = getattr(args, 'low_memory', False)
    live_progress: Optional[LiveProgress] = None
    if getattr(args, "live", False) or getattr(args, "inventory", None):
        LOG.info("Live mode: collecting tasks for %d device(s)...",
                 len(args.paths or []) + (1 if args.inventory else 0))
        tasks = collect_live_tasks(args)
        live_progress = LiveProgress(total_devices=len(tasks), debug=args.debug)
        for t in tasks:
            t.live_progress = live_progress
    else:
        # Collect all processing tasks
        LOG.info("Collecting processing tasks from %d path(s)...", len(args.paths))
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
        # For -L / -r / --cli extraction modes, returning success is misleading.
        if (
            args.list_showtech_commands
            or args.raw_command is not None
            or getattr(args, "cli", False)
        ):
            print(
                "error: no show-tech/show-tech-support-all files found for -L/-r/--cli under given PATH",
                file=sys.stderr,
            )
            sys.exit(1)
        LOG.warning("No show-tech files found to process.")
        return

    if getattr(args, "cli", False):
        if len(tasks) > 1:
            print(
                f"warning: --cli uses first show-tech only ({tasks[0].source_id!r}); "
                f"{len(tasks) - 1} other file(s) ignored.",
                file=sys.stderr,
            )
        cli_task = tasks[0]
        text = load_task_text(cli_task)
        st_parser = TechSupportParser()
        cli_blocks = st_parser.parse(text)
        del text
        del st_parser
        if cli_task.text is not None:
            cli_task.text = None
        run_interactive_showtech_cli(cli_blocks, cli_task.source_id)
        sys.exit(0)

    if args.list_showtech_commands or args.raw_command is not None:
        ec, extract_out = run_showtech_command_extract(
            tasks,
            list_commands=args.list_showtech_commands,
            raw_command=args.raw_command,
        )
        if args.output:
            out_path = Path(args.output)
            try:
                out_path.write_text(extract_out, encoding="utf-8")
                LOG.info("Extract output written to: %s", out_path)
            except OSError as exc:
                LOG.error("Failed to write output to %s: %s", out_path, exc)
                print(extract_out)
                # Ensure write failures are visible to automation (do not keep original ec=0).
                ec = max(ec, 1)
        else:
            print(extract_out)
        sys.exit(ec)

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
            if live_progress is not None and task.live_creds is not None:
                live_progress.device_finished(
                    task.live_creds.host,
                    ok=not report.startswith("Error processing "),
                )
            # Release task text reference immediately after processing
            del task.text
        
        # Clean up tasks list after sequential processing
        del tasks
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
                        task_ok = True
                        try:
                            source_id, report = future.result()
                            batch_results[idx] = report
                            if report.startswith("Error processing "):
                                task_ok = False
                            LOG.info("Completed: %s", source_id)
                        except Exception as exc:
                            LOG.error("Task %s raised an exception: %s", task.source_id, exc, exc_info=args.debug)
                            batch_results[idx] = f"Error processing {task.source_id}: {exc}"
                            task_ok = False
                        finally:
                            if live_progress is not None and task.live_creds is not None:
                                live_progress.device_finished(
                                    task.live_creds.host, ok=task_ok
                                )
                            # Release task text reference immediately after processing
                            if task.text is not None:
                                del task.text
                    
                    # Add batch results to outputs
                    outputs.extend(r for r in batch_results if r is not None)
                    
                    # Clean up batch
                    del batch_results
                    del future_to_index
            
            # Clean up tasks list after all batches complete
            del tasks
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
                    task_ok = True
                    try:
                        source_id, report = future.result()
                        results[idx] = report
                        if report.startswith("Error processing "):
                            task_ok = False
                        LOG.info("Completed: %s", source_id)
                    except Exception as exc:
                        LOG.error("Task %s raised an exception: %s", task.source_id, exc, exc_info=args.debug)
                        results[idx] = f"Error processing {task.source_id}: {exc}"
                        task_ok = False
                    finally:
                        if live_progress is not None and task.live_creds is not None:
                            live_progress.device_finished(
                                task.live_creds.host, ok=task_ok
                            )
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

    if live_progress is not None:
        live_progress.close()

    final_output = "\n\n".join(outputs)

    # Release outputs list after creating final_output
    del outputs

    if args.output:
        out_path = Path(args.output)
        try:
            out_path.write_text(final_output, encoding="utf-8")
            LOG.info("Report written to: %s", out_path)
            # Release final_output after writing to file
            del final_output
        except OSError as exc:
            LOG.error("Failed to write output to %s: %s", out_path, exc)
            print(final_output)
            # final_output will be released when function exits
    else:
        print(final_output)
        # final_output will be released when function exits


if __name__ == "__main__":
    main()


