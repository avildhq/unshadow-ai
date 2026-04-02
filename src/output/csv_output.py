import csv
import io
import sys

from src.models import ExtensionInfo
from src.platform_utils import get_hostname, get_ip_addresses

FIELDNAMES = [
    "hostname", "ip_addresses",
    "os", "user", "category", "application", "profile",
    "extension_id", "extension_name", "version", "enabled", "description",
    "permissions", "install_time", "from_webstore", "install_source",
    "content_scripts", "manifest_hash", "is_default",
]


def _make_row(ext: ExtensionInfo, hostname: str, ip_addrs: str) -> dict:
    row = ext.to_dict()
    row["hostname"] = hostname
    row["ip_addresses"] = ip_addrs
    return row


def generate_csv(results: list[ExtensionInfo]) -> str:
    """Generate CSV string from results."""
    hostname = get_hostname()
    ip_addrs = "; ".join(get_ip_addresses())

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=FIELDNAMES)
    writer.writeheader()
    for ext in results:
        writer.writerow(_make_row(ext, hostname, ip_addrs))
    return buf.getvalue()


def write_csv(results: list[ExtensionInfo], output_path: str | None = None) -> None:
    hostname = get_hostname()
    ip_addrs = "; ".join(get_ip_addresses())

    if output_path:
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for ext in results:
                writer.writerow(_make_row(ext, hostname, ip_addrs))
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="")
        writer = csv.DictWriter(sys.stdout, fieldnames=FIELDNAMES)
        writer.writeheader()
        for ext in results:
            writer.writerow(_make_row(ext, hostname, ip_addrs))
