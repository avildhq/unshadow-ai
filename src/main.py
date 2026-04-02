import argparse
import logging
import sys
from datetime import datetime, timezone

from src.collectors.browsers import ChromiumCollector, FirefoxCollector, SafariCollector
from src.collectors.office import OfficeCollector
from src.collectors.ide import IDECollector
from src.collectors.software import SoftwareCollector
from src.output.json_output import write_json, generate_json
from src.output.csv_output import write_csv, generate_csv
from src.output.upload import upload_results, upload_to_s3
from src.platform_utils import enumerate_users, get_os

VERSION = "2.0.0"

# ANSI color codes
CYAN = "\033[96m"
MAGENTA = "\033[95m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
WHITE = "\033[97m"
DIM = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"

BANNER = f"""{CYAN}{BOLD}
 ██╗   ██╗███╗   ██╗███████╗██╗  ██╗ █████╗ ██████╗  ██████╗ ██╗    ██╗       {MAGENTA} █████╗ ██╗
 {CYAN}██║   ██║████╗  ██║██╔════╝██║  ██║██╔══██╗██╔══██╗██╔═══██╗██║    ██║       {MAGENTA}██╔══██╗██║
 {CYAN}██║   ██║██╔██╗ ██║███████╗███████║███████║██║  ██║██║   ██║██║ █╗ ██║{WHITE}█████╗{MAGENTA}███████║██║
 {CYAN}██║   ██║██║╚██╗██║╚════██║██╔══██║██╔══██║██║  ██║██║   ██║██║███╗██║{WHITE}╚════╝{MAGENTA}██╔══██║██║
 {CYAN}╚██████╔╝██║ ╚████║███████║██║  ██║██║  ██║██████╔╝╚██████╔╝╚███╔███╔╝      {MAGENTA}██║  ██║██║
 {CYAN} ╚═════╝ ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝  ╚═════╝  ╚══╝╚══╝       {MAGENTA}╚═╝  ╚═╝╚═╝
{RESET}"""

TABLE = f"""
  {DIM}Cross-platform extension, add-on, and software inventory tool{RESET}
  {DIM}Version {VERSION}{RESET}

"""

# Build table programmatically for guaranteed alignment
_C1 = 20  # category column width (content)
_C2 = 53  # products column width (content)
_SEP = f"  {{W}}|{{R}} {{D}}{('- ' * (_C1 // 2))}{{R}}{{W}}|{{R}} {{D}}{('- ' * (_C2 // 2))} {{R}}{{W}}|{{R}}"
_ROWS = [
    ("Browsers",           "Chrome, Brave, Opera, Edge, Arc, 360 Browser,"),
    ("",                   "Firefox, Safari (macOS)"),
    None,
    ("Office Add-ins",     "Word, Excel, Outlook (COM, VSTO, Web, XLL)"),
    None,
    ("IDEs",               "VS Code, Cursor, Antigravity, Visual Studio,"),
    ("",                   "JetBrains (IntelliJ, PyCharm, WebStorm, +9 more)"),
    None,
    ("Installed Software", "Windows Registry, Microsoft Store, WSL distros,"),
    ("",                   "macOS /Applications, Homebrew,"),
    ("",                   "Linux: dpkg, rpm, snap, flatpak, pacman"),
    None,
    ("Package Managers",   "winget, Chocolatey, Scoop, npm, pip, cargo, gem"),
]

def _build_table():
    W, R, G, D, B = WHITE, RESET, GREEN, DIM, BOLD
    border = f"  {W}{B}+{'-' * (_C1 + 2)}+{'-' * (_C2 + 2)}+{R}"
    header = f"  {W}{B}| {'Category':{_C1}} | {'Supported Products':{_C2}} |{R}"
    sep_left = ('- ' * (_C1 // 2)).ljust(_C1)
    sep_right = ('- ' * (_C2 // 2)).ljust(_C2)
    sep = f"  {W}|{R} {D}{sep_left}{R} {W}|{R} {D}{sep_right}{R} {W}|{R}"
    lines = [border, header, border]
    for row in _ROWS:
        if row is None:
            lines.append(sep)
        else:
            cat, prod = row
            cat_str = f"{G}{cat}{R}" if cat else ""
            pad = _C1 - len(cat)
            lines.append(f"  {W}|{R} {cat_str}{' ' * pad} {W}|{R} {prod:{_C2}} {W}|{R}")
    lines.append(border)
    return "\n".join(lines)

TABLE = f"""
  {DIM}Cross-platform extension, add-on, and software inventory tool{RESET}
  {DIM}Version {VERSION}{RESET}

{_build_table()}

  {YELLOW}{BOLD}Usage:{RESET}
    unshadow-ai {GREEN}-f json{RESET}                            Scan all, output JSON to stdout
    unshadow-ai {GREEN}-f csv -o report.csv{RESET}               Scan all, save CSV to file
    unshadow-ai {GREEN}-c browser{RESET}                         Scan browser extensions only
    unshadow-ai {GREEN}-c software{RESET}                        Scan installed software only
    unshadow-ai {GREEN}--s3 s3://bucket-name{RESET}              Upload to public S3 bucket
    unshadow-ai {GREEN}--s3 s3://bucket/prefix{RESET}            Upload to S3 with key prefix
    unshadow-ai {GREEN}--upload https://server/api{RESET}        Upload via HTTP PUT
    unshadow-ai {GREEN}--upload http://server/api -k{RESET}      Upload to HTTP (skip SSL)
    unshadow-ai {GREEN}-h{RESET}                                 Show all options

  {DIM}Output filename: FQDN-YYYYMMDD-HHMMSS.json (auto-generated for S3 uploads){RESET}
"""


def show_banner():
    """Display the colorful welcome screen."""
    if sys.platform == "win32":
        try:
            # Enable ANSI escape codes on Windows
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print(BANNER)
    print(TABLE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="unshadow-ai",
        description="Unshadow-AI: Cross-platform extension, add-on, and software inventory tool.",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"Unshadow-AI {VERSION}",
    )
    parser.add_argument(
        "--list-supported", "-l",
        action="store_true",
        help="List all supported browsers, IDEs, Office products, and software sources",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["json", "csv"],
        default=None,
        help="Output format (json or csv)",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--category", "-c",
        choices=["browser", "office", "ide", "software", "all"],
        default=None,
        help="Category to scan (default: all)",
    )
    parser.add_argument(
        "--users", "-u",
        nargs="*",
        default=None,
        help="Specific usernames to scan (default: all users)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--upload",
        default=None,
        metavar="URL",
        help="Upload results to URL via HTTP (supports S3 presigned URLs, any HTTP/HTTPS endpoint)",
    )
    parser.add_argument(
        "--upload-method",
        choices=["PUT", "POST"],
        default="PUT",
        help="HTTP method for upload (default: PUT)",
    )
    parser.add_argument(
        "--s3",
        default=None,
        metavar="S3_URI",
        help="Upload results to S3 bucket (e.g. s3://bucket-name or s3://bucket/prefix). "
             "Uses unsigned PUT — works on public/overpermissive buckets without AWS credentials.",
    )
    parser.add_argument(
        "--no-verify-ssl", "-k",
        action="store_true",
        help="Skip SSL/TLS certificate verification for upload",
    )
    return parser.parse_args()


def default_filename(ext: str = "json") -> str:
    """Generate default output filename: FQDN-YYYYMMDD-HHMMSS.ext"""
    from src.platform_utils import get_hostname
    hostname = get_hostname()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{hostname}-{timestamp}.{ext}"


def main() -> None:
    # Show banner when no arguments provided
    if len(sys.argv) == 1:
        show_banner()
        return

    args = parse_args()

    if args.list_supported:
        show_banner()
        return

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    # Default format to json if not specified
    output_format = args.format or "json"
    category = args.category or "all"

    current_os = get_os()
    logging.info("Detected OS: %s", current_os)

    users = enumerate_users(args.users)
    logging.info("Scanning users: %s", [u[0] for u in users])

    results = []

    if category in ("browser", "all"):
        results.extend(ChromiumCollector().collect(users))
        results.extend(FirefoxCollector().collect(users))
        results.extend(SafariCollector().collect(users))

    if category in ("office", "all"):
        results.extend(OfficeCollector().collect(users))

    if category in ("ide", "all"):
        results.extend(IDECollector().collect(users))

    if category in ("software", "all"):
        results.extend(SoftwareCollector().collect(users))

    logging.info("Found %d items total", len(results))

    # Generate output
    file_ext = output_format
    if output_format == "json":
        output_str = generate_json(results)
        content_type = "application/json"
    else:
        output_str = generate_csv(results)
        content_type = "text/csv"

    # Determine output filename (default: FQDN-date-time.ext)
    out_filename = default_filename(file_ext)
    uploading = args.upload or args.s3

    # Write to file and/or stdout
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="" if output_format == "csv" else None) as f:
            f.write(output_str)
            if output_format == "json":
                f.write("\n")
        print(f"Output written to {args.output} ({len(results)} items)", file=sys.stderr)
    elif not uploading:
        # Write to stdout only if not upload-only
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               **{"newline": ""} if output_format == "csv" else {})
        sys.stdout.write(output_str)
        if output_format == "json":
            sys.stdout.write("\n")

    # Upload to HTTP endpoint if requested
    if args.upload:
        data = output_str.encode("utf-8")
        success = upload_results(
            data=data,
            url=args.upload,
            content_type=content_type,
            method=args.upload_method,
            verify_ssl=not args.no_verify_ssl,
        )
        if not success:
            sys.exit(1)

    # Upload to S3 bucket if requested
    if args.s3:
        data = output_str.encode("utf-8")
        success = upload_to_s3(
            data=data,
            s3_uri=args.s3,
            filename=out_filename,
            content_type=content_type,
            verify_ssl=not args.no_verify_ssl,
        )
        if not success:
            sys.exit(1)


if __name__ == "__main__":
    main()
