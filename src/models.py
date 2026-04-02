from dataclasses import dataclass, asdict


@dataclass
class ExtensionInfo:
    os: str
    user: str
    category: str
    application: str
    profile: str
    extension_id: str
    extension_name: str
    version: str
    enabled: bool
    description: str
    permissions: str = ""
    install_time: str = ""
    from_webstore: bool | None = None
    install_source: str = ""
    content_scripts: str = ""
    manifest_hash: str = ""
    is_default: bool = False

    def __post_init__(self):
        # Sanitize all string fields: strip control chars, surrogates, normalize whitespace
        for field_name in ("extension_name", "extension_id", "version",
                           "description", "permissions", "content_scripts",
                           "application", "profile", "install_source"):
            val = getattr(self, field_name, "")
            if isinstance(val, str):
                # Remove surrogates, null bytes, and control characters
                val = val.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
                val = "".join(c for c in val if c == "\t" or c == "\n" or ord(c) >= 32)
                val = " ".join(val.split())
                setattr(self, field_name, val)

    def to_dict(self) -> dict:
        return asdict(self)
