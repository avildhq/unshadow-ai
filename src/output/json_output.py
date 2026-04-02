import json
import sys
from datetime import datetime, timezone

from src.models import ExtensionInfo
from src.platform_utils import get_hostname, get_ip_addresses


def generate_json(results: list[ExtensionInfo]) -> str:
    """Generate JSON string from results."""
    data = {
        "hostname": get_hostname(),
        "ip_addresses": get_ip_addresses(),
        "scan_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_extensions": len(results),
        "extensions": [ext.to_dict() for ext in results],
    }
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def write_json(results: list[ExtensionInfo], output_path: str | None = None) -> None:
    json_str = generate_json(results)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(json_str)
            f.write("\n")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.write(json_str)
        sys.stdout.write("\n")
