import logging
import os
import platform
import socket
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SKIP_USERS_WINDOWS = {"Public", "Default", "Default User", "All Users", "desktop.ini"}
SKIP_USERS_MACOS = {"Shared", ".localized"}
SKIP_USERS_LINUX = set()


def get_os() -> str:
    system = platform.system()
    if system == "Windows":
        return "windows"
    elif system == "Darwin":
        return "macos"
    elif system == "Linux":
        return "linux"
    else:
        raise RuntimeError(f"Unsupported OS: {system}")


def enumerate_users(filter_users: list[str] | None = None) -> list[tuple[str, str]]:
    """Return list of (username, home_path) for all user profiles on the system."""
    current_os = get_os()
    users = []

    if current_os == "windows":
        base = Path("C:/Users")
        skip = SKIP_USERS_WINDOWS
    elif current_os == "macos":
        base = Path("/Users")
        skip = SKIP_USERS_MACOS
    else:
        base = Path("/home")
        skip = SKIP_USERS_LINUX

    try:
        if not base.exists():
            logger.warning("User base directory %s does not exist", base)
            return users
    except PermissionError:
        logger.warning("Permission denied accessing %s", base)
        return users

    try:
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in skip:
                continue
            if filter_users and entry.name not in filter_users:
                continue
            users.append((entry.name, str(entry)))
    except PermissionError:
        logger.warning("Permission denied listing %s", base)

    if not users:
        # Fallback to current user
        home = str(Path.home())
        username = os.getenv("USERNAME") or os.getenv("USER") or Path.home().name
        users.append((username, home))
        logger.info("Falling back to current user: %s", username)

    return users


def resolve_path(template: str, user_home: str) -> Path:
    """Resolve a path template by substituting the user home directory.

    Templates use {home} as placeholder, and {localappdata}, {appdata} for Windows.
    """
    current_os = get_os()
    user_home_path = Path(user_home)
    username = user_home_path.name

    replacements = {
        "{home}": user_home,
    }

    if current_os == "windows":
        replacements["{localappdata}"] = str(user_home_path / "AppData" / "Local")
        replacements["{appdata}"] = str(user_home_path / "AppData" / "Roaming")
        replacements["{userprofile}"] = user_home
    elif current_os == "macos":
        replacements["{library}"] = str(user_home_path / "Library")

    result = template
    for key, value in replacements.items():
        result = result.replace(key, value)

    return Path(result)


def get_hostname() -> str:
    """Return the machine FQDN."""
    return socket.getfqdn()


def get_ip_addresses() -> list[str]:
    """Return all non-loopback IP addresses for this machine."""
    ips = set()
    hostname = socket.gethostname()
    try:
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            # Skip loopback and link-local
            if addr.startswith("127.") or addr == "::1" or addr.startswith("fe80"):
                continue
            ips.add(addr)
    except socket.gaierror:
        pass

    # Also try connecting to a public DNS to find the primary IP
    for target in [("8.8.8.8", 53), ("1.1.1.1", 53)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(target)
            addr = s.getsockname()[0]
            if not addr.startswith("127."):
                ips.add(addr)
            s.close()
        except OSError:
            pass

    return sorted(ips)


def get_ctime_iso(path: Path | str) -> str:
    """Get file/folder creation time as ISO 8601 UTC string."""
    try:
        ctime = os.path.getctime(str(path))
        dt = datetime.fromtimestamp(ctime, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return ""
