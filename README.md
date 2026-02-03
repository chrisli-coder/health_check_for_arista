# Arista EOS Health Check Tool

A comprehensive health check tool for analyzing Arista EOS device show-tech files and support-bundle diagnostic archives.

**Author**: chris.li@arista.com  
**Company**: Arista Networks  
**Last Modified**: 2026-02-03

## Description

This tool analyzes Arista EOS show-tech / show-tech-support-all outputs and related support-bundle archives/directories to generate health reports. It supports multiple input formats, platform-specific checks, and flexible output modes.

## Features

- **Multiple Input Formats**:
  - Single or multiple show-tech files
  - Unpacked support-bundle directories
  - Single support-bundle archive files (zip, tar, tar.gz, tgz)
  - Nested archives (archives containing other archives)

- **Platform Support** (phased implementation):
  - Phase 1: 78xx series
  - Phase 2: 75xx series
  - Phase 3: 7368, 7289, and 7388 series
  - Phase 4: Other series

- **Output Modes**:
  - Brief mode: Summary with key information (hostname, version, model, system time, health status)
  - Verbose mode: Detailed output for all checked items
  - Debug mode: Full raw command outputs for troubleshooting
  - JSON mode: Machine-readable JSON format

- **Comprehensive Health Checks**:
  - System information (version, uptime, memory, temperature, cooling)
  - Process monitoring (CPU usage, memory usage)
  - Hardware status (modules, PCI errors, FPGA errors)
  - Interface statistics (errors, discards, queue drops)
  - Storage health (flash usage, storage status)
  - Platform-specific checks (FAP fabric SerDes links, redundancy status)
  - Configuration checks (running-config patterns)

## Requirements

- Python 3.6 or higher
- Standard library only (no external dependencies)

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
```

### Command Line Options

#### Output Modes

- `-b, --brief`: Brief report mode (default)
- `-v, --verbose`: Verbose report mode (includes all check details)
- `-d, --debug`: Enable debug logging and show full raw outputs
- `-j, --json`: Output report in JSON format

#### Output Control

- `-o FILE, --output FILE`: Write report to FILE instead of stdout

#### Information and Filtering

- `-l, --list-checks`: List all supported health checks and exit
- `-c [CHECK_NAME ...], --show-checks-in-brief [CHECK_NAME ...]`: 
  - Show specified checks in brief mode output
  - If no check names provided, shows all supported checks list
  - Use `--list-checks` to see available check names
- `-s CHECK_NAME [CHECK_NAME ...], --skip-checks CHECK_NAME [CHECK_NAME ...]`:
  - Skip specified checks during execution
  - Can specify multiple check names to skip
  - Use `--list-checks` to see available check names
- `-S, --skip-categories CATEGORY [CATEGORY ...]`:
  - Skip all checks in specified categories during execution
  - Can specify multiple categories (e.g., system, hardware, interface, process, storage, software, environment, config)
  - Use `--list-checks` to see available categories

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
python3 health_check_eos.py -v /path/to/show-tech
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
```

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
- `show cpu counters queue`: CPU queue drops

### System Logs
- `show agent logs crash`: Agent crash logs
- `show logging threshold errors`: ECC/CRC error logs
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
- Detailed output for all checked items
- Summary and important lines/columns (limited to avoid excessive output)

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

## Troubleshooting

### Enable Debug Mode

Use `-d` or `--debug` flag to see:
- Processing logs (which files are being processed)
- Full raw command outputs
- Detailed parsing information

### List Available Checks

Use `-l` or `--list-checks` to see all supported checks with their commands and supported platforms.

### View Specific Checks

Use `-c` or `--show-checks-in-brief` to view details of specific checks in brief mode.

## Notes

- The tool loads files into memory for fast processing, then releases memory
- Command blocks in show-tech files are identified by `---` delimiters (e.g., `------------- show—cmd -------------`)
- Some checks are platform-specific and will return INFO if the platform doesn't match
- The tool supports nested archives (archives containing other archives)

## License

Copyright (c) 2026 Arista Networks, Inc. All rights reserved.

## Support

For issues or questions, please contact: chris.li@arista.com
