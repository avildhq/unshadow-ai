import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from src.collectors.base import BaseCollector
from src.models import ExtensionInfo
from src.platform_utils import get_os, resolve_path, get_ctime_iso

logger = logging.getLogger(__name__)

# Windows registry paths for installed programs
UNINSTALL_REGISTRY_PATHS = [
    ("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
    ("HKLM", "SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
    ("HKCU", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
]

# Windows Store apps registry
APPX_REGISTRY_PATH = (
    "Software\\Classes\\Local Settings\\Software\\Microsoft\\"
    "Windows\\CurrentVersion\\AppModel\\Repository\\Packages"
)

# Windows FILETIME epoch offset
_FILETIME_EPOCH_DIFF = 116444736000000000


def _filetime_to_iso(filetime: int) -> str:
    try:
        unix_ts = (filetime - _FILETIME_EPOCH_DIFF) / 10_000_000
        dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        if dt.year < 2000 or dt.year > 2100:
            return ""
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return ""


def _run_cmd(cmd: list[str], timeout: int = 30) -> str | None:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug("Command failed %s: %s", cmd[0], e)
    return None


class WindowsSoftwareCollector(BaseCollector):
    """Collects installed programs and Store apps on Windows."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        if get_os() != "windows":
            return []

        try:
            import winreg
        except ImportError:
            return []

        results = []
        seen_ids = set()

        # 1. Traditional installed programs from registry
        for info in self._collect_registry_programs(winreg):
            if info.extension_id not in seen_ids:
                seen_ids.add(info.extension_id)
                results.append(info)

        # 2. Microsoft Store apps
        for info in self._collect_store_apps(winreg):
            if info.extension_id not in seen_ids:
                seen_ids.add(info.extension_id)
                results.append(info)

        # 3. Winget packages
        for info in self._collect_winget():
            if info.extension_id not in seen_ids:
                seen_ids.add(info.extension_id)
                results.append(info)

        # 4. Chocolatey packages
        for info in self._collect_chocolatey():
            if info.extension_id not in seen_ids:
                seen_ids.add(info.extension_id)
                results.append(info)

        return results

    def _collect_registry_programs(self, winreg) -> list[ExtensionInfo]:
        results = []
        hive_map = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}

        for hive_name, reg_path in UNINSTALL_REGISTRY_PATHS:
            hive = hive_map[hive_name]
            try:
                key = winreg.OpenKey(hive, reg_path)
            except OSError:
                continue

            try:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        i += 1
                    except OSError:
                        break

                    try:
                        subkey = winreg.OpenKey(key, subkey_name)

                        # Skip system components
                        try:
                            sys_comp, _ = winreg.QueryValueEx(subkey, "SystemComponent")
                            if sys_comp == 1:
                                winreg.CloseKey(subkey)
                                continue
                        except OSError:
                            pass

                        display_name = self._read_val(winreg, subkey, "DisplayName", "")
                        if not display_name:
                            winreg.CloseKey(subkey)
                            continue
                        # Ensure strings are valid for JSON serialization
                        display_name = display_name.encode("utf-8", errors="replace").decode("utf-8")

                        version = self._read_val(winreg, subkey, "DisplayVersion", "unknown")
                        publisher = self._read_val(winreg, subkey, "Publisher", "")
                        publisher = str(publisher).encode("utf-8", errors="replace").decode("utf-8") if publisher else ""
                        install_date_raw = self._read_val(winreg, subkey, "InstallDate", "")

                        # Get registry key timestamp
                        _, _, last_modified = winreg.QueryInfoKey(subkey)
                        reg_time = _filetime_to_iso(last_modified)

                        # Parse InstallDate (YYYYMMDD format)
                        install_time = ""
                        if install_date_raw and len(install_date_raw) == 8:
                            try:
                                dt = datetime.strptime(install_date_raw, "%Y%m%d")
                                install_time = dt.strftime("%Y-%m-%dT00:00:00Z")
                            except ValueError:
                                pass
                        if not install_time:
                            install_time = reg_time

                        winreg.CloseKey(subkey)

                        results.append(ExtensionInfo(
                            os="windows",
                            user="system" if hive_name == "HKLM" else "user",
                            category="software",
                            application="Windows Programs",
                            profile="system",
                            extension_id=subkey_name,
                            extension_name=display_name,
                            version=version,
                            enabled=True,
                            description=publisher,
                            install_time=install_time,
                        ))
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(key)

        return results

    def _read_val(self, winreg, key, name, default):
        try:
            value, _ = winreg.QueryValueEx(key, name)
            return value
        except OSError:
            return default

    def _collect_store_apps(self, winreg) -> list[ExtensionInfo]:
        results = []
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPX_REGISTRY_PATH)
        except OSError:
            return results

        try:
            i = 0
            while True:
                try:
                    pkg_full_name = winreg.EnumKey(key, i)
                    i += 1
                except OSError:
                    break

                try:
                    subkey = winreg.OpenKey(key, pkg_full_name)
                    display_name = self._read_val(winreg, subkey, "DisplayName", "")
                    # DisplayName can be a resource reference like @{...}
                    if display_name.startswith("@{"):
                        display_name = ""

                    # Parse package name from full name: Name_Version_Arch_ResourceId_PublisherId
                    parts = pkg_full_name.split("_")
                    pkg_name = parts[0] if parts else pkg_full_name
                    pkg_version = parts[1] if len(parts) > 1 else "unknown"

                    _, _, last_modified = winreg.QueryInfoKey(subkey)
                    install_time = _filetime_to_iso(last_modified)
                    winreg.CloseKey(subkey)

                    # Skip framework packages
                    is_framework = "Framework" in pkg_full_name or ".NET" in pkg_name

                    results.append(ExtensionInfo(
                        os="windows",
                        user="user",
                        category="software",
                        application="Microsoft Store",
                        profile="system",
                        extension_id=pkg_name,
                        extension_name=display_name or pkg_name,
                        version=pkg_version,
                        enabled=True,
                        description="",
                        install_time=install_time,
                        is_default=is_framework,
                    ))
                except OSError:
                    continue
        finally:
            winreg.CloseKey(key)

        return results

    def _collect_winget(self) -> list[ExtensionInfo]:
        if not shutil.which("winget"):
            return []

        output = _run_cmd([
            "winget", "list",
            "--accept-source-agreements", "--disable-interactivity",
        ], timeout=60)
        if not output:
            return []

        results = []
        lines = output.splitlines()

        # Find header line (contains "Name" and "Id")
        header_idx = -1
        for idx, line in enumerate(lines):
            if "Name" in line and "Id" in line and "Version" in line:
                header_idx = idx
                break
        if header_idx < 0:
            return results

        # Find column positions from separator line (dashes)
        sep_line = lines[header_idx + 1] if header_idx + 1 < len(lines) else ""
        if not sep_line.startswith("-"):
            return results

        # Parse column positions from dash groups
        cols = []
        start = 0
        in_dash = False
        for ci, ch in enumerate(sep_line):
            if ch == "-" and not in_dash:
                start = ci
                in_dash = True
            elif ch != "-" and in_dash:
                cols.append((start, ci))
                in_dash = False
        if in_dash:
            cols.append((start, len(sep_line)))

        for line in lines[header_idx + 2:]:
            if not line.strip() or len(line) < 10:
                continue
            fields = []
            for cs, ce in cols:
                fields.append(line[cs:ce].strip() if cs < len(line) else "")

            if len(fields) >= 3:
                name, pkg_id, version = fields[0], fields[1], fields[2]
                source = fields[3] if len(fields) > 3 else ""
                if name and pkg_id:
                    results.append(ExtensionInfo(
                        os="windows",
                        user="user",
                        category="software",
                        application="winget",
                        profile="system",
                        extension_id=pkg_id,
                        extension_name=name,
                        version=version,
                        enabled=True,
                        description=f"Source: {source}" if source else "",
                    ))

        return results

    def _collect_chocolatey(self) -> list[ExtensionInfo]:
        if not shutil.which("choco"):
            return []

        output = _run_cmd(["choco", "list", "--local-only", "--limit-output"])
        if not output:
            return []

        results = []
        for line in output.splitlines():
            line = line.strip()
            if "|" in line:
                parts = line.split("|", 1)
                name = parts[0].strip()
                version = parts[1].strip() if len(parts) > 1 else "unknown"
                if name:
                    results.append(ExtensionInfo(
                        os="windows",
                        user="user",
                        category="software",
                        application="Chocolatey",
                        profile="system",
                        extension_id=name,
                        extension_name=name,
                        version=version,
                        enabled=True,
                        description="",
                    ))

        return results


class MacSoftwareCollector(BaseCollector):
    """Collects installed applications on macOS."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        if get_os() != "macos":
            return []

        results = []
        seen = set()

        # 1. Scan /Applications/
        for info in self._collect_applications():
            if info.extension_id not in seen:
                seen.add(info.extension_id)
                results.append(info)

        # 2. Homebrew
        for info in self._collect_homebrew():
            if info.extension_id not in seen:
                seen.add(info.extension_id)
                results.append(info)

        return results

    def _collect_applications(self) -> list[ExtensionInfo]:
        results = []
        app_dirs = [Path("/Applications")]

        for app_dir in app_dirs:
            if not app_dir.exists():
                continue
            try:
                for item in app_dir.rglob("*.app"):
                    plist_path = item / "Contents" / "Info.plist"
                    if not plist_path.exists():
                        continue

                    try:
                        import plistlib
                        with open(plist_path, "rb") as f:
                            plist = plistlib.load(f)

                        bundle_id = plist.get("CFBundleIdentifier", item.stem)
                        name = plist.get("CFBundleName", plist.get("CFBundleDisplayName", item.stem))
                        version = plist.get("CFBundleShortVersionString", plist.get("CFBundleVersion", "unknown"))

                        results.append(ExtensionInfo(
                            os="macos",
                            user="system",
                            category="software",
                            application="macOS Applications",
                            profile="system",
                            extension_id=bundle_id,
                            extension_name=name,
                            version=version,
                            enabled=True,
                            description="",
                            install_time=get_ctime_iso(item),
                            is_default=str(item).startswith("/Applications/") and bundle_id.startswith("com.apple."),
                        ))
                    except Exception as e:
                        logger.debug("Failed to parse plist %s: %s", plist_path, e)
            except PermissionError:
                logger.warning("Permission denied: %s", app_dir)

        return results

    def _collect_homebrew(self) -> list[ExtensionInfo]:
        brew_path = shutil.which("brew")
        if not brew_path:
            return []

        output = _run_cmd(["brew", "info", "--json=v2", "--installed"], timeout=30)
        if not output:
            return []

        results = []
        try:
            data = json.loads(output)
            for formula in data.get("formulae", []):
                name = formula.get("name", "")
                version = formula.get("versions", {}).get("stable", "unknown")
                desc = formula.get("desc", "")
                results.append(ExtensionInfo(
                    os="macos",
                    user="user",
                    category="software",
                    application="Homebrew",
                    profile="system",
                    extension_id=name,
                    extension_name=name,
                    version=version,
                    enabled=True,
                    description=desc[:200] if desc else "",
                ))
            for cask in data.get("casks", []):
                name = cask.get("token", "")
                version = cask.get("version", "unknown")
                desc = cask.get("desc", "")
                results.append(ExtensionInfo(
                    os="macos",
                    user="user",
                    category="software",
                    application="Homebrew Cask",
                    profile="system",
                    extension_id=name,
                    extension_name=cask.get("name", [name])[0] if cask.get("name") else name,
                    version=version,
                    enabled=True,
                    description=desc[:200] if desc else "",
                ))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse Homebrew JSON: %s", e)

        return results


class LinuxSoftwareCollector(BaseCollector):
    """Collects installed packages on Linux."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        if get_os() != "linux":
            return []

        results = []
        seen = set()

        for collector in [
            self._collect_dpkg, self._collect_rpm,
            self._collect_snap, self._collect_flatpak, self._collect_pacman,
        ]:
            for info in collector():
                if info.extension_id not in seen:
                    seen.add(info.extension_id)
                    results.append(info)

        return results

    def _collect_dpkg(self) -> list[ExtensionInfo]:
        if not shutil.which("dpkg-query"):
            return []

        output = _run_cmd([
            "dpkg-query", "-W",
            "-f", "${Package}\t${Version}\t${Status}\t${binary:Summary}\n",
        ])
        if not output:
            return []

        results = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            pkg, version, status = parts[0], parts[1], parts[2]
            desc = parts[3] if len(parts) > 3 else ""
            if "install ok installed" not in status:
                continue

            # Try to get install time from dpkg info file
            info_file = Path(f"/var/lib/dpkg/info/{pkg}.list")
            install_time = get_ctime_iso(info_file) if info_file.exists() else ""

            results.append(ExtensionInfo(
                os="linux",
                user="system",
                category="software",
                application="dpkg",
                profile="system",
                extension_id=pkg,
                extension_name=pkg,
                version=version,
                enabled=True,
                description=desc[:200],
                install_time=install_time,
            ))

        return results

    def _collect_rpm(self) -> list[ExtensionInfo]:
        if not shutil.which("rpm"):
            return []

        output = _run_cmd([
            "rpm", "-qa",
            "--queryformat", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{INSTALLTIME}\t%{SUMMARY}\n",
        ])
        if not output:
            return []

        results = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            pkg, version, install_epoch = parts[0], parts[1], parts[2]
            desc = parts[3] if len(parts) > 3 else ""

            install_time = ""
            try:
                dt = datetime.fromtimestamp(int(install_epoch), tz=timezone.utc)
                install_time = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, OSError):
                pass

            results.append(ExtensionInfo(
                os="linux",
                user="system",
                category="software",
                application="rpm",
                profile="system",
                extension_id=pkg,
                extension_name=pkg,
                version=version,
                enabled=True,
                description=desc[:200],
                install_time=install_time,
            ))

        return results

    def _collect_snap(self) -> list[ExtensionInfo]:
        if not shutil.which("snap"):
            return []

        output = _run_cmd(["snap", "list"])
        if not output:
            return []

        results = []
        lines = output.splitlines()
        for line in lines[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 3:
                name, version, rev = parts[0], parts[1], parts[2]
                results.append(ExtensionInfo(
                    os="linux",
                    user="system",
                    category="software",
                    application="snap",
                    profile="system",
                    extension_id=name,
                    extension_name=name,
                    version=version,
                    enabled=True,
                    description=f"rev {rev}",
                ))

        return results

    def _collect_flatpak(self) -> list[ExtensionInfo]:
        if not shutil.which("flatpak"):
            return []

        output = _run_cmd([
            "flatpak", "list", "--app",
            "--columns=name,application,version,origin",
        ])
        if not output:
            return []

        results = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[0].strip()
                app_id = parts[1].strip() if len(parts) > 1 else name
                version = parts[2].strip() if len(parts) > 2 else "unknown"
                origin = parts[3].strip() if len(parts) > 3 else ""
                results.append(ExtensionInfo(
                    os="linux",
                    user="system",
                    category="software",
                    application="flatpak",
                    profile="system",
                    extension_id=app_id,
                    extension_name=name,
                    version=version,
                    enabled=True,
                    description=f"Origin: {origin}" if origin else "",
                ))

        return results

    def _collect_pacman(self) -> list[ExtensionInfo]:
        if not shutil.which("pacman"):
            return []

        output = _run_cmd(["pacman", "-Qe"])
        if not output:
            return []

        results = []
        for line in output.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) >= 2:
                name, version = parts[0], parts[1]
                results.append(ExtensionInfo(
                    os="linux",
                    user="system",
                    category="software",
                    application="pacman",
                    profile="system",
                    extension_id=name,
                    extension_name=name,
                    version=version,
                    enabled=True,
                    description="",
                ))

        return results


class DevPackageCollector(BaseCollector):
    """Collects globally-installed packages from developer package managers."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        current_os = get_os()
        results = []
        seen = set()

        for collector in [
            self._collect_npm, self._collect_pip,
            self._collect_cargo, self._collect_gem,
            self._collect_scoop,
        ]:
            for info in collector(current_os):
                if info.extension_id not in seen:
                    seen.add(info.extension_id)
                    results.append(info)

        return results

    def _collect_npm(self, current_os: str) -> list[ExtensionInfo]:
        if not shutil.which("npm"):
            return []

        output = _run_cmd(["npm", "list", "-g", "--depth=0", "--json"], timeout=30)
        if not output:
            return []

        results = []
        try:
            data = json.loads(output)
            for name, info in data.get("dependencies", {}).items():
                version = info.get("version", "unknown") if isinstance(info, dict) else "unknown"
                results.append(ExtensionInfo(
                    os=current_os,
                    user="user",
                    category="software",
                    application="npm (global)",
                    profile="system",
                    extension_id=name,
                    extension_name=name,
                    version=version,
                    enabled=True,
                    description="",
                ))
        except json.JSONDecodeError:
            pass

        return results

    def _collect_pip(self, current_os: str) -> list[ExtensionInfo]:
        # Try pip3 first, then pip
        pip_cmd = shutil.which("pip3") or shutil.which("pip")
        if not pip_cmd:
            return []

        output = _run_cmd([pip_cmd, "list", "--format=json"], timeout=30)
        if not output:
            return []

        results = []
        try:
            packages = json.loads(output)
            for pkg in packages:
                name = pkg.get("name", "")
                version = pkg.get("version", "unknown")
                if name:
                    results.append(ExtensionInfo(
                        os=current_os,
                        user="user",
                        category="software",
                        application="pip",
                        profile="system",
                        extension_id=name,
                        extension_name=name,
                        version=version,
                        enabled=True,
                        description="",
                    ))
        except json.JSONDecodeError:
            pass

        return results

    def _collect_cargo(self, current_os: str) -> list[ExtensionInfo]:
        if not shutil.which("cargo"):
            return []

        output = _run_cmd(["cargo", "install", "--list"])
        if not output:
            return []

        results = []
        for line in output.splitlines():
            # Lines starting without whitespace are "package_name vX.Y.Z:"
            if line and not line[0].isspace() and ":" in line:
                parts = line.rstrip(":").split()
                if len(parts) >= 2:
                    name = parts[0]
                    version = parts[1].strip("v")
                    results.append(ExtensionInfo(
                        os=current_os,
                        user="user",
                        category="software",
                        application="cargo",
                        profile="system",
                        extension_id=name,
                        extension_name=name,
                        version=version,
                        enabled=True,
                        description="",
                    ))

        return results

    def _collect_gem(self, current_os: str) -> list[ExtensionInfo]:
        if not shutil.which("gem"):
            return []

        output = _run_cmd(["gem", "list", "--local"])
        if not output:
            return []

        results = []
        for line in output.splitlines():
            # Format: "gem_name (version1, version2)"
            line = line.strip()
            if "(" in line and ")" in line:
                name = line[:line.index("(")].strip()
                versions = line[line.index("(") + 1:line.index(")")].strip()
                version = versions.split(",")[0].strip()
                if name:
                    results.append(ExtensionInfo(
                        os=current_os,
                        user="user",
                        category="software",
                        application="gem",
                        profile="system",
                        extension_id=name,
                        extension_name=name,
                        version=version,
                        enabled=True,
                        description="",
                    ))

        return results

    def _collect_scoop(self, current_os: str) -> list[ExtensionInfo]:
        if current_os != "windows":
            return []

        # Scoop stores apps in ~/scoop/apps/
        scoop_dir = Path.home() / "scoop" / "apps"
        if not scoop_dir.exists():
            return []

        results = []
        try:
            for app_dir in scoop_dir.iterdir():
                if not app_dir.is_dir() or app_dir.name == "scoop":
                    continue
                # Read manifest from current version
                current = app_dir / "current"
                manifest = current / "manifest.json" if current.exists() else None

                version = "unknown"
                description = ""
                if manifest and manifest.exists():
                    try:
                        with open(manifest, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        version = data.get("version", "unknown")
                        description = data.get("description", "")[:200]
                    except (json.JSONDecodeError, PermissionError):
                        pass

                results.append(ExtensionInfo(
                    os=current_os,
                    user="user",
                    category="software",
                    application="Scoop",
                    profile="system",
                    extension_id=app_dir.name,
                    extension_name=app_dir.name,
                    version=version,
                    enabled=True,
                    description=description,
                    install_time=get_ctime_iso(app_dir),
                ))
        except PermissionError:
            pass

        return results


class WSLCollector(BaseCollector):
    """Collects installed packages from WSL distributions (Windows only)."""

    WSL_REGISTRY_PATH = "Software\\Microsoft\\Windows\\CurrentVersion\\Lxss"

    # Package manager commands to try in each distro (in order)
    PKG_MANAGERS = [
        ("dpkg", ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"]),
        ("rpm", ["rpm", "-qa", "--queryformat", "%{NAME}\t%{VERSION}-%{RELEASE}\n"]),
        ("pacman", ["pacman", "-Q"]),
    ]

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        if get_os() != "windows":
            return []

        if not shutil.which("wsl"):
            return []

        distros = self._enumerate_distros()
        if not distros:
            return []

        results = []
        for distro_name, wsl_version in distros:
            logger.info("Scanning WSL distro: %s (WSL%d)", distro_name, wsl_version)
            results.extend(self._collect_distro_packages(distro_name))

        return results

    def _enumerate_distros(self) -> list[tuple[str, int]]:
        """Read WSL distributions from registry."""
        distros = []
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.WSL_REGISTRY_PATH)
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    i += 1
                    subkey = winreg.OpenKey(key, subkey_name)
                    try:
                        name = winreg.QueryValueEx(subkey, "DistributionName")[0]
                        state = winreg.QueryValueEx(subkey, "State")[0]
                        version = winreg.QueryValueEx(subkey, "Version")[0]
                        # State 1 = normal/installed
                        if state == 1 and name:
                            distros.append((name, version))
                    except OSError:
                        pass
                    winreg.CloseKey(subkey)
                except OSError:
                    break
            winreg.CloseKey(key)
        except (ImportError, OSError):
            logger.debug("WSL registry not found")

        return distros

    def _collect_distro_packages(self, distro_name: str) -> list[ExtensionInfo]:
        """Try each package manager inside a WSL distro."""
        for pkg_type, cmd in self.PKG_MANAGERS:
            wsl_cmd = ["wsl", "-d", distro_name, "--"] + cmd
            output = _run_cmd(wsl_cmd, timeout=60)
            if output:
                return self._parse_package_list(output, distro_name, pkg_type)
        return []

    def _parse_package_list(
        self, output: str, distro_name: str, pkg_type: str
    ) -> list[ExtensionInfo]:
        results = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            # Both dpkg and rpm use tab-separated, pacman uses space
            if "\t" in line:
                parts = line.split("\t", 1)
            else:
                parts = line.split(None, 1)
            if len(parts) >= 2:
                name, version = parts[0].strip(), parts[1].strip()
            elif len(parts) == 1:
                name, version = parts[0].strip(), "unknown"
            else:
                continue

            if name:
                results.append(ExtensionInfo(
                    os="windows",
                    user="user",
                    category="software",
                    application=f"WSL: {distro_name}",
                    profile="wsl",
                    extension_id=name,
                    extension_name=name,
                    version=version,
                    enabled=True,
                    description=f"{pkg_type} package",
                ))

        return results


class SoftwareCollector(BaseCollector):
    """Unified software collector that delegates to OS-specific implementations."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        current_os = get_os()
        results = []

        if current_os == "windows":
            results.extend(WindowsSoftwareCollector().collect(users))
            results.extend(WSLCollector().collect(users))
        elif current_os == "macos":
            results.extend(MacSoftwareCollector().collect(users))
        elif current_os == "linux":
            results.extend(LinuxSoftwareCollector().collect(users))

        # Cross-platform dev package managers
        results.extend(DevPackageCollector().collect(users))

        return results
