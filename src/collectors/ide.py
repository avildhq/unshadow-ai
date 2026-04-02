import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from src.collectors.base import BaseCollector
from src.models import ExtensionInfo
from src.platform_utils import get_os, resolve_path, get_ctime_iso

logger = logging.getLogger(__name__)

# VS Code and forks: {app_name: {os: template}}
VSCODE_FAMILY = {
    "VS Code": {
        "windows": "{userprofile}/.vscode/extensions",
        "macos": "{home}/.vscode/extensions",
        "linux": "{home}/.vscode/extensions",
    },
    "Cursor": {
        "windows": "{userprofile}/.cursor/extensions",
        "macos": "{home}/.cursor/extensions",
        "linux": "{home}/.cursor/extensions",
    },
    "Antigravity": {
        "windows": "{appdata}/Antigravity/extensions",
        "macos": "{home}/.config/antigravity/extensions",
        "linux": "{home}/.config/antigravity/extensions",
    },
}

# Visual Studio (Windows only)
VISUAL_STUDIO_LOCALAPPDATA = "{localappdata}/Microsoft/VisualStudio"
VISUAL_STUDIO_INSTALL_DIRS = [
    "C:/Program Files/Microsoft Visual Studio",
    "C:/Program Files (x86)/Microsoft Visual Studio",
]
VISUAL_STUDIO_EDITIONS = [
    "Community", "Professional", "Enterprise", "Insiders", "Preview", "BuildTools",
]

# JetBrains IDEs
JETBRAINS_PRODUCTS = [
    "IntelliJIdea", "PyCharm", "WebStorm", "PhpStorm", "Rider",
    "CLion", "GoLand", "RubyMine", "DataGrip", "DataSpell",
    "AndroidStudio", "Fleet",
]

JETBRAINS_PATHS = {
    "windows": "{appdata}/JetBrains",
    "macos": "{library}/Application Support/JetBrains",
    "linux": "{home}/.local/share/JetBrains",
}


class VSCodeFamilyCollector(BaseCollector):
    """Collects extensions from VS Code and its forks (Cursor, Antigravity)."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        current_os = get_os()
        results = []

        for app_name, os_paths in VSCODE_FAMILY.items():
            template = os_paths.get(current_os)
            if not template:
                continue

            for username, home in users:
                ext_dir = resolve_path(template, home)
                if not ext_dir.exists():
                    logger.debug("%s extensions not found for user %s at %s", app_name, username, ext_dir)
                    continue

                results.extend(self._collect_extensions(ext_dir, current_os, username, app_name))

        return results

    def _collect_extensions(
        self, ext_dir: Path, current_os: str, username: str, app_name: str
    ) -> list[ExtensionInfo]:
        results = []

        try:
            for entry in ext_dir.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name.startswith("."):
                    continue

                package_json = entry / "package.json"
                if not package_json.exists():
                    continue

                try:
                    with open(package_json, "r", encoding="utf-8") as f:
                        pkg = json.load(f)

                    publisher = pkg.get("publisher", "")
                    name = pkg.get("name", entry.name)
                    display_name = pkg.get("displayName", name)
                    ext_id = f"{publisher}.{name}" if publisher else name

                    results.append(ExtensionInfo(
                        os=current_os,
                        user=username,
                        category="ide",
                        application=app_name,
                        profile="N/A",
                        extension_id=ext_id,
                        extension_name=display_name,
                        version=pkg.get("version", "unknown"),
                        enabled=True,
                        description=pkg.get("description", "")[:200] if pkg.get("description") else "",
                        install_time=get_ctime_iso(entry),
                    ))
                except (json.JSONDecodeError, PermissionError) as e:
                    logger.debug("Failed to parse package.json in %s: %s", entry, e)
        except PermissionError:
            logger.warning("Permission denied: %s", ext_dir)

        return results


class VisualStudioCollector(BaseCollector):
    """Collects Visual Studio extensions (Windows only)."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        if get_os() != "windows":
            return []

        results = []

        # 1. Scan Program Files for VS installations
        for install_base in VISUAL_STUDIO_INSTALL_DIRS:
            base = Path(install_base)
            if not base.exists():
                continue
            try:
                for year_dir in base.iterdir():
                    if not year_dir.is_dir():
                        continue
                    for edition_dir in year_dir.iterdir():
                        if not edition_dir.is_dir():
                            continue
                        vs_label = f"{year_dir.name} {edition_dir.name}"
                        ide_dir = edition_dir / "Common7" / "IDE"
                        if not ide_dir.exists():
                            continue

                        # Scan CommonExtensions (built-in) and Extensions (user-installed)
                        for ext_folder, default in [("CommonExtensions", True), ("Extensions", False)]:
                            ext_dir = ide_dir / ext_folder
                            if ext_dir.exists():
                                results.extend(
                                    self._scan_extension_tree(ext_dir, "system", vs_label, default)
                                )
            except PermissionError:
                logger.warning("Permission denied: %s", base)

        # 2. Scan per-user LocalAppData for user-installed extensions
        for username, home in users:
            vs_local = resolve_path(VISUAL_STUDIO_LOCALAPPDATA, home)
            if not vs_local.exists():
                continue
            try:
                for version_dir in vs_local.iterdir():
                    if not version_dir.is_dir():
                        continue
                    extensions_dir = version_dir / "Extensions"
                    if not extensions_dir.exists():
                        continue
                    results.extend(
                        self._scan_extension_tree(extensions_dir, username, version_dir.name)
                    )
            except PermissionError:
                logger.warning("Permission denied: %s", vs_local)

        return results

    def _scan_extension_tree(
        self, root_dir: Path, username: str, vs_version: str, is_default: bool = False
    ) -> list[ExtensionInfo]:
        """Recursively find extension.vsixmanifest files under a directory tree."""
        results = []
        try:
            for manifest_path in root_dir.rglob("extension.vsixmanifest"):
                info = self._parse_vsix_manifest(manifest_path, username, vs_version, is_default)
                if info:
                    results.append(info)
        except PermissionError:
            logger.warning("Permission denied scanning: %s", root_dir)
        return results

    def _parse_vsix_manifest(
        self, manifest_path: Path, username: str, vs_version: str, is_default: bool = False
    ) -> ExtensionInfo | None:
        try:
            tree = ET.parse(manifest_path)
            root = tree.getroot()

            identity_id = "unknown"
            identity_version = "unknown"
            display_name = "unknown"
            description = ""

            # Try 2011 schema first (PackageManifest with Identity element)
            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "Identity":
                    identity_id = elem.get("Id", "unknown")
                    identity_version = elem.get("Version", "unknown")
                elif tag == "DisplayName":
                    display_name = elem.text or "unknown"
                elif tag == "Description":
                    description = elem.text or ""

            # Fallback: 2010 schema (Vsix with Identifier element)
            if identity_id == "unknown":
                for elem in root.iter():
                    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                    if tag == "Identifier":
                        identity_id = elem.get("Id", "unknown")
                        for child in elem:
                            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                            if ctag == "Name" and child.text:
                                display_name = child.text
                            elif ctag == "Version" and child.text and identity_version == "unknown":
                                identity_version = child.text
                            elif ctag == "Description" and child.text:
                                description = child.text
                        break

            return ExtensionInfo(
                os="windows",
                user=username,
                category="ide",
                application=f"Visual Studio ({vs_version})",
                profile="N/A",
                extension_id=identity_id,
                extension_name=display_name,
                version=identity_version,
                enabled=True,
                description=description[:200],
                install_time=get_ctime_iso(manifest_path.parent),
                is_default=is_default,
            )
        except (ET.ParseError, PermissionError) as e:
            logger.debug("Failed to parse vsixmanifest %s: %s", manifest_path, e)
            return None


class JetBrainsCollector(BaseCollector):
    """Collects extensions from JetBrains IDEs."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        current_os = get_os()
        template = JETBRAINS_PATHS.get(current_os)
        if not template:
            return []

        results = []

        for username, home in users:
            jb_base = resolve_path(template, home)
            if not jb_base.exists():
                logger.debug("JetBrains directory not found for user %s at %s", username, jb_base)
                continue

            try:
                for product_dir in jb_base.iterdir():
                    if not product_dir.is_dir():
                        continue

                    # Match known JetBrains product prefixes
                    product_name = self._identify_product(product_dir.name)
                    if not product_name:
                        continue

                    plugins_dir = product_dir / "plugins"
                    if not plugins_dir.exists():
                        continue

                    results.extend(
                        self._collect_plugins(plugins_dir, current_os, username, product_name)
                    )
            except PermissionError:
                logger.warning("Permission denied: %s", jb_base)

        return results

    def _identify_product(self, dirname: str) -> str | None:
        """Match a directory name to a known JetBrains product."""
        for product in JETBRAINS_PRODUCTS:
            if dirname.startswith(product):
                return product
        # Also match common community/professional suffixes
        for product in JETBRAINS_PRODUCTS:
            if product.lower() in dirname.lower():
                return product
        return None

    def _collect_plugins(
        self, plugins_dir: Path, current_os: str, username: str, product_name: str
    ) -> list[ExtensionInfo]:
        results = []

        try:
            for plugin_dir in plugins_dir.iterdir():
                if not plugin_dir.is_dir():
                    continue

                # Look for META-INF/plugin.xml
                plugin_xml = plugin_dir / "META-INF" / "plugin.xml"
                if not plugin_xml.exists():
                    # Some plugins have it nested in lib/
                    plugin_xml = plugin_dir / "lib" / "META-INF" / "plugin.xml"
                if not plugin_xml.exists():
                    # Fallback: just record the directory name
                    results.append(ExtensionInfo(
                        os=current_os,
                        user=username,
                        category="ide",
                        application=f"JetBrains {product_name}",
                        profile="N/A",
                        extension_id=plugin_dir.name,
                        extension_name=plugin_dir.name,
                        version="unknown",
                        enabled=True,
                        description="",
                        install_time=get_ctime_iso(plugin_dir),
                    ))
                    continue

                info = self._parse_plugin_xml(plugin_xml, current_os, username, product_name)
                if info:
                    results.append(info)
        except PermissionError:
            logger.warning("Permission denied: %s", plugins_dir)

        return results

    def _parse_plugin_xml(
        self, plugin_xml: Path, current_os: str, username: str, product_name: str
    ) -> ExtensionInfo | None:
        try:
            tree = ET.parse(plugin_xml)
            root = tree.getroot()

            plugin_id = root.findtext("id", default=root.findtext("name", default="unknown"))
            name = root.findtext("name", default=plugin_id)
            version = root.findtext("version", default="unknown")
            description_elem = root.find("description")
            description = ""
            if description_elem is not None and description_elem.text:
                # Strip HTML tags from description
                desc_text = description_elem.text
                import re
                description = re.sub(r"<[^>]+>", "", desc_text).strip()[:200]

            return ExtensionInfo(
                os=current_os,
                user=username,
                category="ide",
                application=f"JetBrains {product_name}",
                profile="N/A",
                extension_id=plugin_id,
                extension_name=name,
                version=version,
                enabled=True,
                description=description,
                install_time=get_ctime_iso(plugin_xml.parent.parent),
            )
        except (ET.ParseError, PermissionError) as e:
            logger.debug("Failed to parse plugin.xml %s: %s", plugin_xml, e)
            return None


class IDECollector(BaseCollector):
    """Unified IDE collector that delegates to all IDE-specific implementations."""

    def collect(self, users: list[tuple[str, str]]) -> list[ExtensionInfo]:
        results = []
        collectors = [
            VSCodeFamilyCollector(),
            VisualStudioCollector(),
            JetBrainsCollector(),
        ]
        for collector in collectors:
            results.extend(collector.collect(users))
        return results
