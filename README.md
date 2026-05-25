# Arista EOS Health Check Tool

A comprehensive health check tool for analyzing Arista EOS device show-tech files and support-bundle diagnostic archives.

**Author**: chris.li@arista.com  
**Version**: 1.4.5  
**Last Modified**: 2026-05-25

## Description

This tool analyzes Arista EOS show-tech / show-tech-support-all outputs and related support-bundle archives/directories to generate health reports. It supports multiple input formats, platform-specific checks, and flexible output modes. You can pass many paths in one invocation (including several archives at once); filename wildcards are handled by your shell, which expands them into separate path arguments before the tool runs.

## Features

- **Multiple Input Formats**:
  - Single or multiple show-tech files in one run
  - Unpacked support-bundle directories (one or more)
  - Support-bundle archives (zip, tar, tar.gz, tgz), including many archives in one command (for example after shell glob expansion)
  - Nested archives (archives containing other archives)

- **Batch and wildcard-friendly workflow**:
  - Any number of `PATH` arguments: mix files, directories, and archives
  - Typical patterns such as `*.zip`, `bundle-*.tar.gz`, or `site1/*.tgz` are expanded by **bash/zsh** (or your shell) into multiple paths; the tool then discovers show-tech in each input and can process them in parallel (see **Performance**)
  - If a glob matches nothing, behavior depends on the shell (bash often passes the pattern literally; zsh may error by default). Use `shopt -s nullglob` in bash, or zsh options such as `NULL_GLOB` / `NONOMATCH`, if you want “no match” to expand to nothing instead of a bad path

- **Platform Support** (phased implementation):
  - Phase 1: 78xx series
  - Phase 2: 75xx series
  - Phase 3: 7368, 7289, and 7388 series
  - Phase 4: Other series

- **Output Modes**:
  - Brief mode: Summary with key information (hostname, version, model, system time, health status)
  - Summary mode: One-line output for all checks (no details)
  - Warn-only mode: Brief summary plus all WARN-severity check results
  - Verbose mode: Per-check output limited to first 10 lines (to avoid flooding)
  - Debug mode: Full raw command outputs for troubleshooting
  - JSON mode: Machine-readable JSON format

- **Show-tech command introspection** (no health checks run):
  - `-L / --list-showtech-commands`: List each **bundle** command section in parse order (`------------- show … -------------` / `------------- bash … -------------`). In-output dashed headings (e.g. table titles) are ignored.
  - `-r / --raw COMMAND`: Print the full captured output for that command (case-insensitive; exact match first, then prefix). Quote multi-word commands.
  - `--cli`: Interactive EOS-style CLI over the first show-tech found under the given paths (abbreviations, `?`, paging, `| grep` / `| include`). Does not run health checks; if multiple inputs resolve to multiple show-techs, only the first is used (a warning is printed).

- **Comprehensive Health Checks**:
  - System information (version, uptime, memory, temperature, cooling)
  - Process monitoring (CPU usage, memory usage)
  - Hardware status (modules, PCI errors, FPGA errors)
  - Interface statistics (errors, discards, queue drops)
  - Storage health (flash usage, storage status)
  - Platform-specific checks (FAP fabric SerDes, FAP counters `| nz`, redundancy status)
  - Configuration checks (running-config patterns)

## Requirements

- Python 3.6 or higher
- Standard library only for offline mode and `--live` over eAPI
- Optional: `paramiko` for `--live` SSH fallback (`pip install paramiko`)
- Optional: `pyyaml` for YAML-format `--inventory` files (JSON inventories work without it)

## Installation

No installation required. Simply download the `health_check_eos.py` file and run it directly:

```bash
python3 health_check_eos.py --help
```

## Usage

### Basic Usage

```bash
# Analyze a single show-tech file
python3 health_check_eos.py /path/to/show-tech

# Analyze a support-bundle directory
python3 health_check_eos.py /path/to/support-bundle/

# Analyze a compressed archive
python3 health_check_eos.py /path/to/support-bundle.zip

# Analyze multiple inputs
python3 health_check_eos.py file1 file2 directory1 archive.zip

# Many archives at once (shell expands the glob into separate paths)
python3 health_check_eos.py /data/bundles/*.zip

# List every command section in a show-tech file (section headers only)
python3 health_check_eos.py -L /path/to/show-tech

# Dump the raw output of one section (quote the command if it contains spaces)
python3 health_check_eos.py -r "show version" /path/to/show-tech
```

### Multiple paths, wildcards, and archives

- The trailing `PATH ...` arguments accept **any number** of inputs. Each path is classified independently as a plain show-tech file, an unpacked support-bundle tree, or an archive (zip / tar / tar.gz / tgz).
- **Wildcards** (`*`, `?`, `[…]`) are not interpreted inside Python’s argparse; your **shell** expands them before arguments reach the script. Examples (POSIX shell):
  - `python3 health_check_eos.py *.zip` — all zip archives in the current directory
  - `python3 health_check_eos.py -t 4 ~/downloads/switch-*.tar.gz` — several tarballs with up to four worker threads
- To pass a literal `*` in a path, quote it: `'file*.zip'`.
- With multiple inputs, reports are printed **in order**, separated by a blank line between each file’s output (same when writing to `-o`).
- Parallelism (`-t`) speeds up **multiple independent show-tech tasks** (multiple files or multiple archives). A single huge archive still corresponds to one primary parse task per discovered show-tech inside it.

### Command Line Options

#### Output Modes

- `-b, --brief`: Brief report mode (default)
- `-v, --summary`: Summary report mode (one-line output for all checks, no details)
- `-w, --warn-only`: Warn-only mode (brief summary + all WARN-severity checks)
- `-V, --verbose`: Verbose report mode (per-check output limited to first 10 lines)
- `-d, --debug`: Enable debug logging and show full raw outputs
- `-j, --json`: Output report in JSON format

#### Output Control

- `-o FILE, --output FILE`: Write the health report, or `-L` / `-r` extract output, to FILE instead of stdout (not used with `--cli`, which is interactive-only)

#### Information and Filtering

- `-l, --list-checks`: List all supported health checks and exit
- `-L, --list-showtech-commands`: List all command section headers from the show-tech input(s) in order and exit (requires PATH; does not run health checks). Mutually exclusive with `-r` / `--raw` / `--cli`.
- `-r COMMAND, --raw COMMAND`: Dump the raw text captured for `COMMAND` and exit (requires PATH; does not run health checks). Matching is case-insensitive: exact normalized command first, otherwise prefix match over sections in file order. If the same command appears multiple times, every matching section is printed with `match i/n` headers. Exit code `1` if a given input has no matching section. Mutually exclusive with `-L` / `--cli`.
- `--cli`: Start the interactive show-tech CLI on the **first** resolved show-tech (requires PATH; does not run health checks). Mutually exclusive with `-L` / `-r`.

- `-c [CHECK_NAME ...], --show-checks-in-brief [CHECK_NAME ...]`: 
  - Show specified checks in brief mode output (full output for selected checks, not truncated)
  - If no check names provided, shows all supported checks list
  - Use `--list-checks` to see available check names
  - `-c` no longer automatically enables debug mode (`-d`)
- `-s CHECK_NAME [CHECK_NAME ...], --skip-checks CHECK_NAME [CHECK_NAME ...]`:
  - Skip specified checks during execution
  - Can specify multiple check names to skip
  - Use `--list-checks` to see available check names
- `-S CATEGORY [CATEGORY ...], --skip-categories CATEGORY [CATEGORY ...]`:
  - Skip all checks in specified categories during execution
  - Can specify multiple categories (e.g., system, hardware, interface, process, storage, software, environment, config)
  - Use `--list-checks` to see available categories

#### Performance

- `-t N, --threads N`: Number of worker threads for parallel processing
  - Default: number of CPU cores (capped at 8 for memory efficiency)
  - Set to 1 to disable parallel processing (recommended on very small VMs or shared jump hosts where you must avoid loading the machine)
- `-m, --low-memory`: Enable low-memory mode
  - Files are loaded on-demand instead of pre-loading entire archives/files up front
  - Reduces peak memory at the cost of some extra I/O and often slower runs
  - Intended primarily for **low-performance or memory-tight environments**—for example **tac-sftp**-class servers, small shared SFTP VMs, or other boxes with little RAM and slow storage where the default “load everything” behavior risks OOM or heavy swapping
  - If you do **not** pass `-t`, low-memory mode uses a **conservative default** (at most 2 worker threads). You may still set `-t N` explicitly if you need a different cap on that host
  - When there are many tasks, low-memory mode may process them in **batches** so only a limited number of files are in flight at once

#### Help

- `-h, --help`: Show help message and exit

### Examples

```bash
# Basic analysis (brief mode, default)
python3 health_check_eos.py /path/to/show-tech

# List all supported checks
python3 health_check_eos.py -l
# or
python3 health_check_eos.py --list-checks

# Verbose mode with detailed output
python3 health_check_eos.py -V /path/to/show-tech
# or
python3 health_check_eos.py --verbose /path/to/show-tech

# Debug mode with full raw outputs
python3 health_check_eos.py -d /path/to/show-tech
# or
python3 health_check_eos.py --debug /path/to/show-tech

# JSON output to file
python3 health_check_eos.py -j -o report.json /path/to/show-tech
# or
python3 health_check_eos.py --json --output report.json /path/to/show-tech

# Show specific checks in brief mode
python3 health_check_eos.py -c memory_usage_top cpu_usage_top /path/to/show-tech
# or
python3 health_check_eos.py --show-checks-in-brief memory_usage_top cpu_usage_top /path/to/show-tech

# Show all checks list in brief mode
python3 health_check_eos.py -c /path/to/show-tech

# Skip specific checks
python3 health_check_eos.py -s memory_usage_top cpu_usage_top /path/to/show-tech
# or
python3 health_check_eos.py --skip-checks memory_usage_top cpu_usage_top /path/to/show-tech

# Skip entire category
python3 health_check_eos.py -S hardware /path/to/show-tech
# or
python3 health_check_eos.py --skip-categories hardware /path/to/show-tech

# Skip multiple categories
python3 health_check_eos.py -S hardware interface /path/to/show-tech

# Combine options: skip checks and categories
python3 health_check_eos.py -s memory_usage_top -S hardware /path/to/show-tech

# Analyze multiple files
python3 health_check_eos.py file1 file2 directory1 archive.zip

# Analyze archive file
python3 health_check_eos.py /path/to/support-bundle.zip

# Process multiple files in parallel (using 4 threads)
python3 health_check_eos.py -t 4 *.zip
# or
python3 health_check_eos.py --threads 4 *.zip

# Disable parallel processing (single-threaded)
python3 health_check_eos.py -t 1 /path/to/show-tech

# Low-memory mode (best on tac-sftp / small SFTP VMs / low-RAM jump hosts)
python3 health_check_eos.py -m /path/to/show-tech
# or
python3 health_check_eos.py --low-memory /path/to/show-tech
# or
python3 health_check_eos.py -m *.zip

# Low-memory + single-thread (gentlest on CPU and I/O for shared servers)
python3 health_check_eos.py -m -t 1 large_archive*.zip

# Process multiple archives with parallel processing
python3 health_check_eos.py -t 8 archive1.zip archive2.zip archive3.zip

# Interactive show-tech CLI (first matching show-tech only if several are found)
python3 health_check_eos.py --cli /path/to/show-tech
```

### Live mode (`--live`)

Connect directly to one or more EOS devices via eAPI (HTTPS/JSON-RPC) — and
optionally fall back to SSH — to collect just the commands each check needs,
then run the same health checks you would run offline against a show-tech file.
No `show tech-support` round-trip required.

```bash
# Single device (eAPI over HTTPS, self-signed cert OK)
python3 health_check_eos.py --live 10.0.0.1 -u admin --insecure

# Password via env var to keep it out of shell history
EOS_PASSWORD=*** python3 health_check_eos.py --live 10.0.0.1 -u admin --insecure

# Batch from an inventory file, 8 devices in parallel, verbose to a file
python3 health_check_eos.py --live --inventory hosts.yaml -t 8 -V -o report.txt

# Force SSH (paramiko required) when eAPI is disabled
python3 health_check_eos.py --live 10.0.0.1 -u admin --transport ssh

# Have the device run `show tech-support all` and analyze that instead
python3 health_check_eos.py --live 10.0.0.1 -u admin -T --insecure

# Collect from device and also drop a show-tech-style file for offline re-check
python3 health_check_eos.py --live 10.0.0.1 -u admin --insecure --save ./collected/
python3 health_check_eos.py ./collected/10.0.0.1-show-tech-*.txt
```

#### Inventory file

Either JSON or YAML; each entry needs at least `host`. Per-entry fields override
the matching CLI flags:

```yaml
# hosts.yaml
- host: spine1.lab
  user: admin
- host: spine2.lab
  user: netops
  password: hunter2
  transport: eapi
- host: leaf1.lab
  transport: ssh
  port: 22
```

#### Enabling eAPI on the device

```
configure
management api http-commands
   no shutdown
```

#### Live-mode options

| Option | Meaning |
|---|---|
| `--live` | Treat `PATH` arguments as hostnames/IPs |
| `--inventory FILE` | JSON or YAML device list (combines with `PATH` hosts) |
| `-u / --user` | Default username |
| `--password` | Default password (precedence: CLI > `EOS_PASSWORD` env > inventory > getpass) |
| `--port N` | eAPI port (default 443 / 80 with `--http`); SSH always uses 22 |
| `--http` | eAPI over plain HTTP |
| `--insecure` | Skip TLS verification (self-signed certs) |
| `--transport {auto,eapi,ssh}` | Default `auto` tries eAPI then SSH |
| `-T / --use-tech-support` | Run `show tech-support all` on the device instead of the per-check command set |
| `--save DIR` | Also write the collected output to `DIR/<host>-show-tech-<ts>.txt` |

`-t / --threads`, `-o / --output`, `-V / -v / -w / -j`, `-s / -S / -c` all work
the same as in offline mode and apply across all live devices.

#### Progress display

When stderr is a TTY (and `--debug` is off), live mode shows a single
self-updating progress line on stderr so long collections aren't mistaken
for a hang:

- Single device: `host: [5/22] show interfaces counters discards` → `host: parsing`
- Multiple devices: `[3/10 (1 failed)] host-a: [4/22] show ...; host-b: parsing (+2 more)`

SSH gives per-command progress; eAPI is a single round-trip so only a
coarse stage event is shown. A final `Live collection: X/Y ok` summary is
printed once everything finishes. When stderr is redirected to a file or
pipe, the progress line is suppressed automatically.

## Health Checks

The tool performs various health checks organized by category:

### System Checks
- `show version`: Hardware model, software version, architecture, uptime, free memory
- `show clock`: System time
- `show system env cooling`: Cooling status
- `show system env temperature`: Temperature status
- `show system health storage`: Storage health status and lifetime remaining

### Process Checks
- `show processes top once`: CPU usage monitoring
- `show processes top memory once`: Memory usage monitoring

### Hardware Checks
- `show module`: Module uptime status
- `show platform sand health`: Linecard and fabric card initialization status
- `show platform fap fabric detail`: SerDes link status (78xx, 75xx)
- `show platform fap counters | nz`: Non-zero FAP counters — **75xx**: *Cgm Unicast Data Buffer Drop Reassembly Cnt* warns if *Last update* is the same calendar day as `show clock`; **78xx**: *Voq Latency Rjct* warns if the counter row appears. Report details repeat the original chip title, separator, column header, and `[Block]` lines so data stays column-aligned with the table.
- `show redundancy status`: Redundancy protocol status (78xx, 75xx)
- `show pci`: PCI errors (FatalErr, SMBusERR)
- `show hardware counter drop`: Hardware drop counters
- `show hardware capacity`: Hardware capacity usage
- `show hardware fpga error`: FPGA errors
- `show platform scd satellite debug`: SCD satellite retry errors (7368, 7289, 7388)

### Storage Checks
- `bash ls -ltr /var/core`: Core dump file detection
- `bash df -h`: Flash filesystem usage

### Interface Checks
- `show interfaces counters queue drops`: Interface queue drops
- `show interfaces counters discards`: Interface discards
- `show interfaces counters errors`: Interface errors
- `show interfaces status errdisabled`: Errdisabled interfaces
- `show cpu counters queue`: CPU queue drops

### System Logs
- `show agent logs crash`: Agent crash logs
- `show logging threshold errors`: Pattern-based scan for ECC/CRC keywords and high-severity syslog entries (levels 0–2), with warning summary and matching lines.
- `show system environment power detail`: Power input voltage

### Configuration Checks
- `show running-config sanitized`: Platform-specific configuration patterns
- `show extensions detail`: Extension patch status

## Output Format

### Brief Mode

Brief mode displays a summary table with:
- Script execution time
- Hostname
- EOS version
- Hardware model
- System time
- Overall health status (OK/WARN/ERROR) with counts

### Verbose Mode

Verbose mode includes:
- All information from brief mode
- Per-check output limited to the first 10 lines (to avoid excessive output)

### Summary Mode

Summary mode includes:
- All information from brief mode
- One-line output for all checks (no details)

### Debug Mode

Debug mode provides:
- All information from brief and verbose modes
- Full raw command outputs for troubleshooting
- Filtered outputs for specific checks (e.g., only matching lines for regex-based checks)

### JSON Mode

JSON mode outputs structured data:
```json
{
  "source": "file_path",
  "brief": {
    "script_time": "2026-01-30T15:00:00",
    "hostname": "device-hostname",
    "eos_version": "4.30.2F",
    "hw_model": "Arista DCS-7816-CH",
    "system_time": "Tue Jan 27 14:04:43 2026",
    "health": "WARN",
    "warn_count": 5,
    "error_count": 0
  },
  "checks": [...]
}
```

## Platform-Specific Features

### 78xx Series
- FAP fabric SerDes link checks (patterns: `U--- Ramon`, `I---I Ramon`, etc.)
- Redundancy status checks
- Running-config pattern checks

### 75xx Series
- FAP fabric SerDes link checks (patterns: `U--- Fe`, `I---I Fe`, etc.)
- Redundancy status checks

### 7368, 7289, and 7388 Series
- SCD satellite retry error checks

## File Discovery

The tool automatically detects input type (file, directory, or archive) and searches for:
- Exact filenames: `show-tech` or `show-tech-support-all`
- Files in support-bundle directories: `support-bundle/tmp/support-bundle-cmds/show-tech`
- Files in nested archives

## Performance and resource limits

On a normal laptop or build host, defaults are tuned for speed: each task may preload full file text, and the worker pool size follows CPU count (up to eight threads). On **underpowered shared infrastructure**—especially **tac-sftp-style** machines that only host uploads, have little RAM, slow disks, or strict CPU quotas—you should treat **`-m` (low-memory)** and **`-t 1`** as the primary levers so one analysis does not exhaust the box for other users.

### Parallel processing (`-t`)

The tool uses a thread pool so **multiple show-techs** (from multiple paths or multiple members inside archives) can be analyzed concurrently.

- **Default**: Worker count follows CPU cores, capped at eight, to balance throughput and RAM
- **`-t N`**: Set the pool size explicitly (for example `-t 2` on a four-core SFTP VM)
- **`-t 1`**: Fully sequential—best for debugging, or when the host must stay idle-friendly (common on tac-sftp or similar)

**Good fits for parallelism:** many separate files or archives, or many discovered show-techs where work is spread across tasks.

**Caveat:** Peak memory scales with how many large bodies are loaded at once; if the host is small, prefer **`-m`** and/or a lower **`-t`** even when processing many globs.

```bash
# Many archives expanded by the shell; four workers
python3 health_check_eos.py -t 4 archive*.zip

# Several unpacked trees at once
python3 health_check_eos.py -t 8 dir1/ dir2/ dir3/
```

### Low-memory mode (`-m`)

**Primary audience:** low-RAM or I/O-weak servers (for example **tac-sftp** hosts, minimal cloud instances, or crowded jump boxes) where preloading every bundle into memory is risky.

**Behavior (summary):**
- Archive and file contents are **read on demand** when a task runs, instead of loading everything up front where that path applies
- With many tasks, processing may occur in **batches** so only a subset of files is active at a time
- If you omit **`-t`**, the default worker count in low-memory mode stays **small** (at most two threads) to limit concurrent large reads
- You can still pass **`-t 1`** for the lightest footprint, or a higher **`-t`** if you measured that the host can sustain it

**Trade-off:** usually lower peak RAM and less risk of OOM, often at the cost of longer wall-clock time.

```bash
python3 health_check_eos.py -m *.zip
python3 health_check_eos.py -m -t 1 *.zip   # safest pattern on a busy tac-sftp server
```

## Troubleshooting

### Enable Debug Mode

Use `-d` or `--debug` flag to see:
- Processing logs (which files are being processed)
- Full raw command outputs
- Detailed parsing information

### List Available Checks

Use `-l` or `--list-checks` to see all supported checks with their commands and supported platforms.

### View Specific Checks

Use `-c` or `--show-checks-in-brief` to view full output of specific checks in brief mode (not truncated). `-c` no longer automatically enables debug mode (`-d`).

## Notes

- **Memory and CPU**: Default mode favors speed on capable machines. Use **`-m`** (and often **`-t 1`**) on **tac-sftp-class** or other **low-performance** servers where aggressive parallelism and full preloads are a bad fit.
- **Wildcards** are a shell feature: the program receives a list of paths; ensure your shell expands globs as you expect (or use explicit paths / `find` / `xargs` if globs are unavailable, for example in some non-interactive contexts).
- Command blocks in show-tech files are identified by `---` delimiters (e.g., `------------- show—cmd -------------`)
- Some checks are platform-specific and will return INFO if the platform doesn't match
- The tool supports nested archives (archives containing other archives)

## Support

For issues or questions, please contact: chris.li@arista.com
