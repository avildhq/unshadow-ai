import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from src.collectors.base import BaseCollector
from src.models import ExtensionInfo
from src.platform_utils import get_os, resolve_path, get_ctime_iso

logger = logging.getLogger(__name__)

OFFICE_APPS = ["Word", "Excel", "Outlook"]

# Windows FILETIME epoch offset: 100-ns intervals between 1601-01-01 and 1970-01-01
_FILETIME_EPOCH_DIFF = 116444736000000000


def _filetime_to_iso(filetime: int) -> str:
    """Convert a Windows FILETIME (from QueryInfoKey) to ISO 8601 UTC string."""
    try:
        unix_ts = (filetime - _FILETIME_EPOCH_DIFF) / 10_000_000
        dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        if dt.year < 2000 or dt.year > 2100:
            return ""
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return ""

# Office internal version numbers: 14.0=2010, 15.0=2013, 16.0=2016/2019/2021/365
OFFICE_VERSIONS = ["16.0", "15.0", "14.0"]

# Windows registry paths for Office add-ins
REGISTRY_ROOTS = [
    # Version-independent paths
    ("HKCU", "Software\\Microsoft\\Office\\{app}\\Addins"),
    ("HKLM", "Software\\Microsoft\\Office\\{app}\\Addins"),
    # Version-specific paths
    ("HKCU", "Software\\Microsoft\\Office\\16.0\\{app}\\Addins"),
    ("HKLM", "Software\\Microsoft\\Office\\16.0\\{app}\\Addins"),
    ("HKCU", "Software\\Microsoft\\Office\\15.0\\{app}\\Addins"),
    ("HKLM", "Software\\Microsoft\\Office\\15.0\\{app}\\Addins"),
    ("HKCU", "Software\\Microsoft\\Office\\14.0\\{app}\\Addins"),
    ("HKLM", "Software\\Microsoft\\Office\\14.0\\{app}\\Addins"),
    # Click-to-Run virtualized paths
    ("HKCU", "Software\\Microsoft\\Office\\ClickToRun\\REGISTRY\\MACHINE\\Software\\Microsoft\\Office\\{app}\\Addins"),
]

# Registry paths for Excel XLL/XLA add-ins (stored as OPEN, OPEN1, OPEN2, ... values)
XLL_REGISTRY_PATHS = [
    "Software\\Microsoft\\Office\\{version}\\Excel\\Options",
]

# Windows startup add-in directories
WINDOWS_STARTUP_TEMPLATES = [
    ("{appdata}/Microsoft/Excel/XLSTART", "Excel"),
    ("{appdata}/Microsoft/Word/STARTUP", "Word"),
]

# macOS paths for Office web add-ins (per-app containers, all version dirs scanned)
MAC_CONTAINER_TEMPLATES = {
    "Word": "{library}/Containers/com.microsoft.Word/Data/Library/Application Support/Microsoft/Office",
    "Excel": "{library}/Containers/com.microsoft.Excel/Data/Library/Application Support/Microsoft/Office",
    "Outlook": "{library}/Containers/com.microsoft.Outlook/Data/Library/Application Support/Microsoft/Office",
}

MAC_STARTUP_TEMPLATE = "{library}/Group Containers/UBF8T346G9.Office/User Content/Startup"


class WindowsOfficeCollector(BaseCollector):
    """Collects Office add-ins on Windows via registry."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        if get_os() != "windows":
            return []

        try:
            import winreg
        except ImportError:
            logger.warning("winreg not available — not on Windows")
            return []

        results = []
        seen_ids = set()

        for username, home in users:
            try:
                # 1. Registry-based add-ins (COM/VSTO) — deduplicated
                for app in OFFICE_APPS:
                    for info in self._collect_registry_addins(winreg, app, username):
                        if info.extension_id not in seen_ids:
                            seen_ids.add(info.extension_id)
                            results.append(info)

                # 2. Web add-ins from Wef directory — scan all Office version dirs
                office_base = resolve_path("{localappdata}/Microsoft/Office", home)
                if office_base.exists():
                    for version_dir in office_base.iterdir():
                        if not version_dir.is_dir():
                            continue
                        wef_path = version_dir / "Wef"
                        if wef_path.exists():
                            for info in self._collect_wef_addins(wef_path, username):
                                if info.extension_id not in seen_ids:
                                    seen_ids.add(info.extension_id)
                                    results.append(info)

                # 3. XLL/XLA add-ins from Excel Options registry
                for info in self._collect_xll_addins(winreg, username):
                    if info.extension_id not in seen_ids:
                        seen_ids.add(info.extension_id)
                        results.append(info)

                # 4. Startup directory add-ins (XLSTART, Word STARTUP)
                for template, app in WINDOWS_STARTUP_TEMPLATES:
                    startup_path = resolve_path(template, home)
                    if startup_path.exists():
                        for info in self._collect_startup_dir(startup_path, username, app):
                            if info.extension_id not in seen_ids:
                                seen_ids.add(info.extension_id)
                                results.append(info)
            except PermissionError as e:
                logger.warning("Permission denied scanning Office for user %s: %s", username, e)
                continue

        return results

    def _collect_registry_addins(self, winreg, app: str, username: str) -> list[ExtensionInfo]:
        results = []
        hive_map = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}

        for hive_name, path_template in REGISTRY_ROOTS:
            hive = hive_map.get(hive_name)
            if not hive:
                continue

            reg_path = path_template.replace("{app}", app)
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
                        # Get registry key last-write-time
                        _, _, last_modified = winreg.QueryInfoKey(subkey)
                        install_time = _filetime_to_iso(last_modified)
                        friendly_name = self._read_reg_value(winreg, subkey, "FriendlyName", subkey_name)
                        description = self._read_reg_value(winreg, subkey, "Description", "")
                        load_behavior = self._read_reg_value(winreg, subkey, "LoadBehavior", 0)
                        winreg.CloseKey(subkey)

                        results.append(ExtensionInfo(
                            os="windows",
                            user=username,
                            category="office",
                            application=f"Office {app}",
                            profile="Shared",
                            extension_id=subkey_name,
                            extension_name=friendly_name,
                            version="unknown",
                            enabled=bool(load_behavior),
                            description=str(description)[:200],
                            install_time=install_time,
                        ))
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(key)

        return results

    def _read_reg_value(self, winreg, key, name: str, default):
        try:
            value, _ = winreg.QueryValueEx(key, name)
            return value
        except OSError:
            return default

    def _collect_xll_addins(self, winreg, username: str) -> list[ExtensionInfo]:
        """Collect Excel XLL/XLA binary add-ins from registry OPEN keys."""
        results = []

        for version in OFFICE_VERSIONS:
            for path_template in XLL_REGISTRY_PATHS:
                reg_path = path_template.replace("{version}", version)
                try:
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path)
                except OSError:
                    continue

                try:
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, i)
                            i += 1
                        except OSError:
                            break

                        # XLL add-ins are registered as OPEN, OPEN1, OPEN2, etc.
                        if not name.startswith("OPEN"):
                            continue

                        # Value format: "/R \"path\\to\\addin.xll\"" or just the path
                        addin_path = str(value).strip().strip('"')
                        if addin_path.startswith("/R "):
                            addin_path = addin_path[3:].strip().strip('"')

                        addin_name = Path(addin_path).stem if addin_path else name

                        results.append(ExtensionInfo(
                            os="windows",
                            user=username,
                            category="office",
                            application="Office Excel",
                            profile="Shared",
                            extension_id=addin_name,
                            extension_name=addin_name,
                            version="unknown",
                            enabled=True,
                            description=f"XLL add-in: {addin_path}",
                        ))
                finally:
                    winreg.CloseKey(key)

        return results

    def _collect_startup_dir(
        self, startup_path: Path, username: str, app: str
    ) -> list[ExtensionInfo]:
        """Collect add-in files from XLSTART or Word STARTUP directories."""
        results = []
        addin_extensions = {".xlam", ".xla", ".xll", ".dotm", ".dot", ".ppam"}
        try:
            for item in startup_path.iterdir():
                if item.is_file() and item.suffix.lower() in addin_extensions:
                    results.append(ExtensionInfo(
                        os="windows",
                        user=username,
                        category="office",
                        application=f"Office {app}",
                        profile="Shared",
                        extension_id=item.stem,
                        extension_name=item.stem,
                        version="unknown",
                        enabled=True,
                        description=f"Startup add-in: {item.name}",
                        install_time=get_ctime_iso(item),
                    ))
        except PermissionError:
            logger.warning("Permission denied: %s", startup_path)
        return results

    def _collect_wef_addins(self, wef_path: Path, username: str) -> list[ExtensionInfo]:
        """Collect web add-ins from the Wef directory."""
        results = []
        seen_ids = set()

        try:
            # Find Manifests directories and parse XML files within them
            resolved_store_ids = set()
            for manifests_dir in wef_path.rglob("Manifests"):
                if not manifests_dir.is_dir():
                    continue
                for manifest_file in manifests_dir.iterdir():
                    if not manifest_file.is_file():
                        continue
                    info = self._parse_wef_manifest(manifest_file, username)
                    if info and info.extension_id not in seen_ids:
                        seen_ids.add(info.extension_id)
                        results.append(info)
                        # Mark the store ID from the filename as resolved
                        store_id = manifest_file.name.split("_")[0]
                        resolved_store_ids.add(store_id)
        except PermissionError:
            logger.warning("Permission denied: %s", wef_path)

        return results

    def _parse_wef_manifest(self, file_path: Path, username: str) -> ExtensionInfo | None:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read(100)
            if "<?xml" not in content and "<OfficeApp" not in content:
                return None

            tree = ET.parse(file_path)
            root = tree.getroot()

            addin_id = "unknown"
            display_name = "unknown"
            version = "unknown"
            description = ""
            permissions = ""

            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "Id" and addin_id == "unknown":
                    addin_id = (elem.text or "").strip()
                elif tag == "DisplayName":
                    display_name = elem.get("DefaultValue", elem.text or "unknown")
                elif tag == "Version" and version == "unknown":
                    version = (elem.text or "").strip()
                elif tag == "Description":
                    description = elem.get("DefaultValue", elem.text or "")
                elif tag == "Permissions" and not permissions:
                    permissions = (elem.text or "").strip()

            # Determine host app from manifest
            app = "Office"
            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "Host":
                    host_name = elem.get("Name", "") or elem.get("xsi:type", "")
                    host_map = {
                        "Workbook": "Excel", "Document": "Word",
                        "Mailbox": "Outlook", "Presentation": "PowerPoint",
                        "Notebook": "OneNote",
                    }
                    if host_name in host_map:
                        app = host_map[host_name]
                        break

            return ExtensionInfo(
                os="windows",
                user=username,
                category="office",
                application=f"Office {app}",
                profile="Shared",
                extension_id=addin_id,
                extension_name=display_name,
                version=version,
                enabled=True,
                description=description[:200],
                permissions=permissions,
                install_source="webstore",
                install_time=get_ctime_iso(file_path),
            )
        except (ET.ParseError, PermissionError, UnicodeDecodeError) as e:
            logger.debug("Failed to parse Wef manifest %s: %s", file_path, e)
            return None


class MacOfficeCollector(BaseCollector):
    """Collects Office add-ins on macOS."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        if get_os() != "macos":
            return []

        results = []

        for username, home in users:
            # Web add-ins per Office app — scan all version directories
            for app, template in MAC_CONTAINER_TEMPLATES.items():
                container_base = resolve_path(template, home)
                if not container_base.exists():
                    continue
                try:
                    for version_dir in container_base.iterdir():
                        if not version_dir.is_dir():
                            continue
                        wef_path = version_dir / "Wef"
                        if wef_path.exists():
                            results.extend(self._collect_wef_dir(wef_path, username, app))
                except PermissionError:
                    logger.warning("Permission denied: %s", container_base)

            # Startup add-ins (traditional)
            startup_path = resolve_path(MAC_STARTUP_TEMPLATE, home)
            if startup_path.exists():
                results.extend(self._collect_startup_dir(startup_path, username))

        return results

    def _collect_wef_dir(self, wef_path: Path, username: str, app: str) -> list[ExtensionInfo]:
        results = []
        try:
            for item in wef_path.iterdir():
                if item.is_dir():
                    for xml_file in item.glob("*.xml"):
                        info = self._parse_manifest(xml_file, username, app)
                        if info:
                            results.append(info)
        except PermissionError:
            logger.warning("Permission denied: %s", wef_path)
        return results

    def _parse_manifest(self, xml_path: Path, username: str, app: str) -> ExtensionInfo | None:
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()

            addin_id = "unknown"
            display_name = "unknown"
            version = "unknown"
            description = ""

            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "Id" and addin_id == "unknown":
                    addin_id = elem.text or "unknown"
                elif tag == "DisplayName":
                    display_name = elem.get("DefaultValue", elem.text or "unknown")
                elif tag == "Version" and version == "unknown":
                    version = elem.text or "unknown"
                elif tag == "Description":
                    description = elem.get("DefaultValue", elem.text or "")

            return ExtensionInfo(
                os="macos",
                user=username,
                category="office",
                application=f"Office {app}",
                profile="Shared",
                extension_id=addin_id,
                extension_name=display_name,
                version=version,
                enabled=True,
                description=description[:200],
            )
        except (ET.ParseError, PermissionError) as e:
            logger.debug("Failed to parse manifest %s: %s", xml_path, e)
            return None

    def _collect_startup_dir(self, startup_path: Path, username: str) -> list[ExtensionInfo]:
        results = []
        try:
            for item in startup_path.iterdir():
                if item.is_file() and item.suffix in (".xlam", ".dotm", ".ppam", ".xla", ".dot"):
                    results.append(ExtensionInfo(
                        os="macos",
                        user=username,
                        category="office",
                        application="Office Startup Add-in",
                        profile="Shared",
                        extension_id=item.stem,
                        extension_name=item.stem,
                        version="unknown",
                        enabled=True,
                        description=f"Startup add-in: {item.name}",
                        install_time=get_ctime_iso(item),
                    ))
        except PermissionError:
            logger.warning("Permission denied: %s", startup_path)
        return results


class OfficeCollector(BaseCollector):
    """Unified Office collector that delegates to OS-specific implementations."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        current_os = get_os()
        if current_os == "linux":
            logger.info("Microsoft Office is not natively supported on Linux — skipping")
            return []

        results = []
        if current_os == "windows":
            results.extend(WindowsOfficeCollector().collect(users))
        elif current_os == "macos":
            results.extend(MacOfficeCollector().collect(users))
        return results
