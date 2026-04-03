import configparser
import glob
import hashlib
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.collectors.base import BaseCollector
from src.models import ExtensionInfo
from src.platform_utils import get_os, resolve_path, get_ctime_iso

logger = logging.getLogger(__name__)

# WebKit epoch: 1601-01-01 00:00:00 UTC (microseconds)
WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

# Chromium-based browser path templates: {browser_name: {os: template}}
CHROMIUM_PATHS = {
    "Chrome": {
        "windows": "{localappdata}/Google/Chrome/User Data",
        "macos": "{library}/Application Support/Google/Chrome",
        "linux": "{home}/.config/google-chrome",
    },
    "Brave": {
        "windows": "{localappdata}/BraveSoftware/Brave-Browser/User Data",
        "macos": "{library}/Application Support/BraveSoftware/Brave-Browser",
        "linux": "{home}/.config/BraveSoftware/Brave-Browser",
    },
    "Opera": {
        "windows": "{appdata}/Opera Software/Opera Stable",
        "macos": "{library}/Application Support/com.operasoftware.Opera",
        "linux": "{home}/.config/opera",
    },
    "Edge": {
        "windows": "{localappdata}/Microsoft/Edge/User Data",
        "macos": "{library}/Application Support/Microsoft Edge",
        "linux": "{home}/.config/microsoft-edge",
    },
    "360 Browser": {
        "windows": "{appdata}/360se6/User Data",
    },
}

# Registry policy paths for enterprise-forced extensions (Windows)
CHROMIUM_POLICY_REGISTRY = {
    "Chrome": "SOFTWARE\\Policies\\Google\\Chrome\\ExtensionInstallForcelist",
    "Edge": "SOFTWARE\\Policies\\Microsoft\\Edge\\ExtensionInstallForcelist",
    "Brave": "SOFTWARE\\Policies\\BraveSoftware\\Brave\\ExtensionInstallForcelist",
}

FIREFOX_PATHS = {
    "windows": "{appdata}/Mozilla/Firefox",
    "macos": "{library}/Application Support/Firefox",
    "linux": "{home}/.mozilla/firefox",
}


def _webkit_to_iso(webkit_time_str: str) -> str:
    """Convert a WebKit timestamp (microseconds since 1601-01-01) to ISO 8601."""
    try:
        microseconds = int(webkit_time_str)
        if microseconds <= 0:
            return ""
        dt = WEBKIT_EPOCH + timedelta(microseconds=microseconds)
        if dt.year < 2000 or dt.year > 2100:
            return ""
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError):
        return ""


def _extract_permissions(manifest: dict) -> str:
    """Extract permissions and optional_permissions from a Chromium manifest."""
    perms = []
    for p in manifest.get("permissions", []):
        if isinstance(p, str):
            perms.append(p)
        elif isinstance(p, dict):
            perms.extend(p.keys())
    for p in manifest.get("optional_permissions", []):
        if isinstance(p, str):
            perms.append(f"[optional]{p}")
    return ", ".join(perms)


def _extract_content_scripts(manifest: dict) -> str:
    """Extract content_scripts match patterns from a Chromium manifest."""
    matches = []
    for cs in manifest.get("content_scripts", []):
        for m in cs.get("matches", []):
            matches.append(m)
    return ", ".join(matches)


class ChromiumCollector(BaseCollector):
    """Collects extensions from Chromium-based browsers with full metadata."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        current_os = get_os()
        results = []

        for browser_name, os_paths in CHROMIUM_PATHS.items():
            template = os_paths.get(current_os)
            if not template:
                continue

            for username, home in users:
                base_path = resolve_path(template, home)
                try:
                    if not base_path.exists():
                        logger.debug("%s not found for user %s at %s", browser_name, username, base_path)
                        continue
                except PermissionError:
                    logger.debug("Permission denied checking %s for user %s", base_path, username)
                    continue

                try:
                    profiles = self._discover_profiles(base_path, browser_name)
                except PermissionError:
                    logger.warning("Permission denied reading profiles at %s", base_path)
                    continue

                for profile_name, profile_path in profiles:
                    extensions = self._collect_profile_extensions(
                        profile_path, current_os, username, browser_name, profile_name
                    )
                    results.extend(extensions)

            # Collect enterprise policy-forced extensions (per browser, not per user)
            if current_os == "windows":
                results.extend(self._collect_policy_extensions(browser_name, current_os))

        # Arc browser: UWP package with dynamic path suffix — needs glob discovery
        for username, home in users:
            arc_paths = self._discover_arc_paths(home, current_os)
            for arc_base in arc_paths:
                profiles = self._discover_profiles(arc_base, "Arc")
                for profile_name, profile_path in profiles:
                    results.extend(self._collect_profile_extensions(
                        profile_path, current_os, username, "Arc", profile_name
                    ))

        return results

    def _discover_arc_paths(self, user_home: str, current_os: str) -> list[Path]:
        """Find Arc browser User Data paths (UWP on Windows, standard on macOS)."""
        paths = []
        if current_os == "windows":
            # Arc on Windows is a UWP package: TheBrowserCompany.Arc_<suffix>
            packages_dir = Path(user_home) / "AppData" / "Local" / "Packages"
            if packages_dir.exists():
                for match in packages_dir.glob("TheBrowserCompany.Arc_*"):
                    user_data = match / "LocalCache" / "Local" / "Arc" / "User Data"
                    if user_data.exists():
                        paths.append(user_data)
            # Also check non-UWP install path
            non_uwp = Path(user_home) / "AppData" / "Local" / "Arc" / "User Data"
            if non_uwp.exists():
                paths.append(non_uwp)
        elif current_os == "macos":
            mac_path = Path(user_home) / "Library" / "Application Support" / "Arc" / "User Data"
            if mac_path.exists():
                paths.append(mac_path)
        if not paths:
            logger.debug("Arc not found for user at %s", user_home)
        return paths

    def _discover_profiles(self, base_path: Path, browser_name: str) -> list[tuple[str, Path]]:
        """Discover browser profiles from Local State or by directory scan."""
        profiles = []

        if browser_name == "Opera":
            if (base_path / "Extensions").exists():
                return [("Default", base_path)]
            return []

        local_state = base_path / "Local State"
        if local_state.exists():
            try:
                with open(local_state, "r", encoding="utf-8") as f:
                    data = json.load(f)
                profile_info = data.get("profile", {}).get("info_cache", {})
                for profile_dir_name, info in profile_info.items():
                    profile_path = base_path / profile_dir_name
                    if profile_path.exists():
                        # Build a meaningful profile label: email > display name > dir name
                        email = info.get("user_name", "") if isinstance(info, dict) else ""
                        display = info.get("name", "") if isinstance(info, dict) else ""
                        profile_label = email or display or profile_dir_name
                        profiles.append((profile_label, profile_path))
            except (json.JSONDecodeError, KeyError, PermissionError) as e:
                logger.warning("Failed to parse Local State for %s: %s", browser_name, e)

        if not profiles:
            try:
                for candidate in base_path.iterdir():
                    if candidate.is_dir() and (
                        candidate.name == "Default"
                        or candidate.name.startswith("Profile ")
                    ):
                        profiles.append((candidate.name, candidate))
            except PermissionError:
                pass

        return profiles

    def _read_preferences(self, profile_path: Path) -> dict:
        """Read extensions.settings from profile Preferences and Secure Preferences.

        Merges settings from both files, with Secure Preferences taking priority.
        """
        merged = {}
        for filename in ["Preferences", "Secure Preferences"]:
            prefs_path = profile_path / filename
            if not prefs_path.exists():
                continue
            try:
                with open(prefs_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                settings = data.get("extensions", {}).get("settings", {})
                for ext_id, ext_data in settings.items():
                    if ext_id not in merged:
                        merged[ext_id] = ext_data
                    else:
                        merged[ext_id].update(ext_data)
            except (json.JSONDecodeError, PermissionError) as e:
                logger.debug("Failed to parse %s: %s", prefs_path, e)
        return merged

    def _collect_profile_extensions(
        self,
        profile_path: Path,
        current_os: str,
        username: str,
        browser_name: str,
        profile_name: str,
    ) -> list[ExtensionInfo]:
        results = []
        extensions_dir = profile_path / "Extensions"
        if not extensions_dir.exists():
            return results

        # Read Preferences for extension metadata
        ext_settings = self._read_preferences(profile_path)

        # Track which disk extensions are referenced in Preferences
        disk_ext_ids = set()

        try:
            for ext_id_dir in extensions_dir.iterdir():
                if not ext_id_dir.is_dir():
                    continue
                ext_id = ext_id_dir.name
                disk_ext_ids.add(ext_id)

                version_dirs = sorted(
                    [d for d in ext_id_dir.iterdir() if d.is_dir()],
                    key=lambda d: d.name,
                    reverse=True,
                )
                if not version_dirs:
                    continue

                manifest_path = version_dirs[0] / "manifest.json"
                if not manifest_path.exists():
                    continue

                try:
                    manifest_bytes = manifest_path.read_bytes()
                    manifest = json.loads(manifest_bytes)

                    name = manifest.get("name", ext_id)
                    description = manifest.get("description", "")
                    if name.startswith("__MSG_") and name.endswith("__"):
                        name = self._resolve_localized_name(version_dirs[0], name, manifest) or ext_id
                    if description.startswith("__MSG_") and description.endswith("__"):
                        description = self._resolve_localized_name(version_dirs[0], description, manifest) or ""

                    # Enrich from Preferences / Secure Preferences
                    prefs = ext_settings.get(ext_id, {})
                    # disable_reasons: 0 = enabled, nonzero = disabled
                    disable_reasons = prefs.get("disable_reasons", 0)
                    enabled = disable_reasons == 0
                    from_webstore = prefs.get("from_webstore", None)
                    # Chrome uses first_install_time (WebKit microseconds)
                    install_time_raw = prefs.get("first_install_time", "") or prefs.get("install_time", "")
                    install_time = _webkit_to_iso(str(install_time_raw)) if install_time_raw else ""
                    # Fallback: folder creation time
                    if not install_time:
                        install_time = get_ctime_iso(version_dirs[0])

                    # Determine install source
                    if from_webstore:
                        install_source = "webstore"
                    elif ext_id not in ext_settings:
                        install_source = "orphaned"
                    elif prefs.get("was_installed_by_default"):
                        install_source = "default"
                    elif prefs.get("was_installed_by_oem"):
                        install_source = "oem"
                    else:
                        install_source = "sideloaded"

                    results.append(ExtensionInfo(
                        os=current_os,
                        user=username,
                        category="browser",
                        application=browser_name,
                        profile=profile_name,
                        extension_id=ext_id,
                        extension_name=name,
                        version=manifest.get("version", "unknown"),
                        enabled=enabled,
                        description=description[:200],
                        permissions=_extract_permissions(manifest),
                        install_time=install_time,
                        from_webstore=from_webstore,
                        install_source=install_source,
                        content_scripts=_extract_content_scripts(manifest),
                        manifest_hash=hashlib.sha256(manifest_bytes).hexdigest(),
                        is_default=install_source in ("default", "oem"),
                    ))
                except (json.JSONDecodeError, PermissionError) as e:
                    logger.debug("Failed to parse manifest for %s/%s: %s", ext_id, version_dirs[0].name, e)
        except PermissionError:
            logger.warning("Permission denied accessing %s", extensions_dir)

        return results

    def _collect_policy_extensions(
        self, browser_name: str, current_os: str
    ) -> list[ExtensionInfo]:
        """Detect enterprise policy-forced extensions from registry (Windows)."""
        if current_os != "windows":
            return []

        reg_path = CHROMIUM_POLICY_REGISTRY.get(browser_name)
        if not reg_path:
            return []

        try:
            import winreg
        except ImportError:
            return []

        results = []
        seen = set()

        for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            try:
                key = winreg.OpenKey(hive, reg_path)
            except OSError:
                continue

            try:
                i = 0
                while True:
                    try:
                        _, value, _ = winreg.EnumValue(key, i)
                        i += 1
                    except OSError:
                        break

                    # Format: "extension_id;update_url" or just "extension_id"
                    value = str(value)
                    ext_id = value.split(";")[0].strip()
                    if not ext_id or ext_id in seen:
                        continue
                    seen.add(ext_id)

                    results.append(ExtensionInfo(
                        os=current_os,
                        user="policy",
                        category="browser",
                        application=browser_name,
                        profile="Enterprise Policy",
                        extension_id=ext_id,
                        extension_name=ext_id,
                        version="unknown",
                        enabled=True,
                        description="Enterprise policy-forced extension",
                        install_source="policy",
                    ))
            finally:
                winreg.CloseKey(key)

        return results

    def _resolve_localized_name(self, version_dir: Path, msg_key: str, manifest: dict) -> str | None:
        """Try to resolve __MSG_xxx__ style localized extension names."""
        key = msg_key[6:-2]
        default_locale = manifest.get("default_locale", "en")

        for locale in [default_locale, "en", "en_US"]:
            messages_path = version_dir / "_locales" / locale / "messages.json"
            if messages_path.exists():
                try:
                    with open(messages_path, "r", encoding="utf-8") as f:
                        messages = json.load(f)
                    for mkey, mval in messages.items():
                        if mkey.lower() == key.lower():
                            return mval.get("message", None)
                except (json.JSONDecodeError, PermissionError):
                    continue
        return None


class FirefoxCollector(BaseCollector):
    """Collects extensions from Firefox."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        current_os = get_os()
        template = FIREFOX_PATHS.get(current_os)
        if not template:
            return []

        results = []
        for username, home in users:
            firefox_path = resolve_path(template, home)
            try:
                if not firefox_path.exists():
                    logger.debug("Firefox not found for user %s at %s", username, firefox_path)
                    continue
            except PermissionError:
                logger.debug("Permission denied checking %s for user %s", firefox_path, username)
                continue

            try:
                profiles = self._discover_profiles(firefox_path)
            except PermissionError:
                logger.warning("Permission denied reading Firefox profiles at %s", firefox_path)
                continue
            for profile_name, profile_path in profiles:
                extensions = self._collect_profile_extensions(
                    profile_path, current_os, username, profile_name
                )
                results.extend(extensions)

        return results

    def _discover_profiles(self, firefox_path: Path) -> list[tuple[str, Path]]:
        """Parse profiles.ini to find all Firefox profile directories."""
        profiles = []
        profiles_ini = firefox_path / "profiles.ini"

        if profiles_ini.exists():
            config = configparser.ConfigParser()
            try:
                config.read(str(profiles_ini), encoding="utf-8")
                for section in config.sections():
                    if not section.startswith("Profile"):
                        continue
                    name = config.get(section, "Name", fallback=section)
                    path = config.get(section, "Path", fallback=None)
                    is_relative = config.getint(section, "IsRelative", fallback=1)
                    if path:
                        if is_relative:
                            profile_path = firefox_path / path
                        else:
                            profile_path = Path(path)
                        if profile_path.exists():
                            profiles.append((name, profile_path))
            except (configparser.Error, PermissionError) as e:
                logger.warning("Failed to parse Firefox profiles.ini: %s", e)

        if not profiles:
            try:
                profiles_dir = firefox_path / "Profiles" if (firefox_path / "Profiles").exists() else firefox_path
                for entry in profiles_dir.iterdir():
                    if entry.is_dir() and "." in entry.name:
                        profiles.append((entry.name, entry))
            except PermissionError:
                pass

        return profiles

    def _collect_profile_extensions(
        self, profile_path: Path, current_os: str, username: str, profile_name: str
    ) -> list[ExtensionInfo]:
        results = []
        extensions_json = profile_path / "extensions.json"

        if not extensions_json.exists():
            return results

        try:
            with open(extensions_json, "r", encoding="utf-8") as f:
                data = json.load(f)

            for addon in data.get("addons", []):
                addon_type = addon.get("type", "")
                if addon_type == "dictionary":
                    continue
                location = addon.get("location", "")

                # Extract permissions from Firefox addon data
                perms_list = []
                user_perms = addon.get("userPermissions") or {}
                opt_perms = addon.get("optionalPermissions") or {}
                for p in user_perms.get("permissions", []):
                    perms_list.append(str(p))
                for p in user_perms.get("origins", []):
                    perms_list.append(str(p))
                for p in opt_perms.get("permissions", []):
                    perms_list.append(f"[optional]{p}")

                # Install time from Firefox (milliseconds since epoch)
                # Try installDate → updateDate → signedDate → xpi file ctime
                install_time = ""
                for date_field in ("installDate", "updateDate", "signedDate"):
                    ts = addon.get(date_field)
                    if ts:
                        try:
                            dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
                            install_time = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                            break
                        except (ValueError, OverflowError, OSError):
                            continue
                # Fallback: xpi file creation time
                if not install_time:
                    ext_id = addon.get("id", "")
                    xpi_path = profile_path / "extensions" / f"{ext_id}.xpi"
                    if xpi_path.exists():
                        install_time = get_ctime_iso(xpi_path)

                # Determine source
                install_source = "unknown"
                source_uri = addon.get("sourceURI") or ""
                if location in ("app-builtin", "app-system-defaults", "app-builtin-addons"):
                    install_source = "builtin"
                elif addon_type == "builtin":
                    install_source = "builtin"
                elif "addons.mozilla.org" in source_uri:
                    install_source = "webstore"
                elif location == "app-profile":
                    install_source = "sideloaded"

                results.append(ExtensionInfo(
                    os=current_os,
                    user=username,
                    category="browser",
                    application="Firefox",
                    profile=profile_name,
                    extension_id=addon.get("id", "unknown"),
                    extension_name=addon.get("name", addon.get("id", "unknown")),
                    version=addon.get("version", "unknown"),
                    enabled=addon.get("active", False),
                    description=addon.get("description", "")[:200] if addon.get("description") else "",
                    permissions=", ".join(perms_list),
                    install_time=install_time,
                    install_source=install_source,
                    is_default=install_source == "builtin",
                ))
        except (json.JSONDecodeError, PermissionError) as e:
            logger.warning("Failed to parse Firefox extensions.json at %s: %s", extensions_json, e)

        return results


class SafariCollector(BaseCollector):
    """Collects Safari extensions (macOS only)."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        current_os = get_os()
        if current_os != "macos":
            return []

        results = []

        try:
            output = subprocess.run(
                ["pluginkit", "-mAvvp", "-i", "com.apple.Safari.extension"],
                capture_output=True, text=True, timeout=15
            )
            if output.returncode == 0:
                results.extend(self._parse_pluginkit_output(output.stdout))
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("pluginkit command failed: %s", e)

        for username, home in users:
            safari_ext_dir = Path(home) / "Library" / "Safari" / "Extensions"
            if safari_ext_dir.exists():
                try:
                    for ext_file in safari_ext_dir.iterdir():
                        if ext_file.suffix in (".safariextz", ".appex"):
                            results.append(ExtensionInfo(
                                os=current_os,
                                user=username,
                                category="browser",
                                application="Safari",
                                profile="Default",
                                extension_id=ext_file.stem,
                                extension_name=ext_file.stem,
                                version="unknown",
                                enabled=True,
                                description="",
                            ))
                except PermissionError:
                    logger.warning("Permission denied: %s", safari_ext_dir)

        return results

    def _parse_pluginkit_output(self, output: str) -> list[ExtensionInfo]:
        """Parse pluginkit -mAvvp output into ExtensionInfo objects."""
        results = []
        current_ext = {}

        for line in output.splitlines():
            line = line.strip()
            if not line:
                if current_ext.get("identifier"):
                    results.append(ExtensionInfo(
                        os="macos",
                        user="system",
                        category="browser",
                        application="Safari",
                        profile="Default",
                        extension_id=current_ext.get("identifier", ""),
                        extension_name=current_ext.get("name", current_ext.get("identifier", "")),
                        version=current_ext.get("version", "unknown"),
                        enabled=True,
                        description=current_ext.get("path", ""),
                    ))
                current_ext = {}
            elif ":" in line:
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if "identifier" in key or "bundle" in key:
                    current_ext["identifier"] = value
                elif "display" in key or "name" in key:
                    current_ext["name"] = value
                elif "version" in key:
                    current_ext["version"] = value
                elif "path" in key:
                    current_ext["path"] = value

        if current_ext.get("identifier"):
            results.append(ExtensionInfo(
                os="macos",
                user="system",
                category="browser",
                application="Safari",
                profile="Default",
                extension_id=current_ext.get("identifier", ""),
                extension_name=current_ext.get("name", current_ext.get("identifier", "")),
                version=current_ext.get("version", "unknown"),
                enabled=True,
                description=current_ext.get("path", ""),
            ))

        return results
