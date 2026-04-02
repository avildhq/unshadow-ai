# Contributing to Unshadow-AI

Thank you for considering contributing to Unshadow-AI! This document covers the process and guidelines for contributing.

## Getting Started

### Prerequisites

- Python 3.11+
- PyInstaller (`pip install pyinstaller`)
- Git

### Development Setup

```bash
git clone https://github.com/avildhq/unshadow-ai.git
cd unshadow-ai
git checkout dev
```

Run directly with Python (no build needed):
```bash
python -m src.main -c browser -v
```

Build the executable:
```bash
pyinstaller build.spec
# Output: dist/unshadow-ai.exe (or dist/unshadow-ai)
```

### Project Structure

```
unshadow-ai/
  src/
    main.py                  # CLI entry point, banner, argument parsing
    models.py                # ExtensionInfo dataclass
    platform_utils.py        # OS detection, user enumeration, path helpers
    collectors/
      base.py                # Abstract BaseCollector
      browsers.py            # Chromium, Firefox, Safari collectors
      ide.py                 # VS Code, Visual Studio, JetBrains collectors
      office.py              # Office COM/VSTO/Web add-in collectors
      software.py            # Installed software, package managers, WSL
    output/
      json_output.py         # JSON output formatter
      csv_output.py          # CSV output formatter
      upload.py              # HTTP and S3 upload functions
  build.spec                 # PyInstaller build configuration
  demo.tape                  # VHS terminal demo script
```

## How to Contribute

### Reporting Bugs

Use the [Bug Report](https://github.com/avildhq/unshadow-ai/issues/new?template=bug_report.yml) template. Please include:

1. **Exact Unshadow-AI version** (`unshadow-ai --version`)
2. **OS and version** (e.g., Windows 11 Pro 23H2, macOS 14.3)
3. **Steps to reproduce** the issue
4. **Expected vs actual behavior**
5. **Verbose output** (`unshadow-ai -v -c <category> 2>&1`)

### Requesting Features

Use the [Feature Request](https://github.com/avildhq/unshadow-ai/issues/new?template=feature_request.yml) template. Include:

1. What problem the feature solves
2. Your proposed solution with specifics (file paths, data sources)
3. Which platforms it should support

### Submitting Code

1. **Fork** the repository
2. **Create a branch** from `dev` (not `prod`):
   ```bash
   git checkout dev
   git checkout -b feature/my-feature
   ```
3. **Make your changes** following the coding guidelines below
4. **Test** on at least one platform with `-v` flag
5. **Commit** with a clear message:
   ```bash
   git commit -m "Add support for Browser X extensions"
   ```
6. **Push** and open a **Pull Request** against `dev`

> **Important:** PRs should target the `dev` branch. The `prod` branch is protected and only accepts merges from `dev` after review.

## Coding Guidelines

### Adding a New Browser

All Chromium-based browsers share the same collector. To add a new one, simply add an entry to `CHROMIUM_PATHS` in `src/collectors/browsers.py`:

```python
CHROMIUM_PATHS = {
    ...
    "NewBrowser": {
        "windows": "{localappdata}/NewBrowser/User Data",
        "macos": "{library}/Application Support/NewBrowser",
        "linux": "{home}/.config/newbrowser",
    },
}
```

Then update `SUPPORTED_PRODUCTS` in `src/main.py` and the table in the banner.

### Adding a New IDE

For VS Code-based editors, add to `VSCODE_FAMILY` in `src/collectors/ide.py`. For other IDEs, create a new collector class following the `BaseCollector` pattern.

### Adding a New Package Manager

Add a method to `DevPackageCollector` or the appropriate OS-specific collector in `src/collectors/software.py`:

```python
def _collect_newtool(self, current_os: str) -> list[ExtensionInfo]:
    if not shutil.which("newtool"):
        return []
    output = _run_cmd(["newtool", "list", "--json"])
    if not output:
        return []
    # Parse output and return ExtensionInfo list
```

### General Rules

- **No external dependencies** — use Python stdlib only. The tool must build as a single binary.
- **Graceful failure** — if a browser/IDE/package manager isn't installed, log a debug message and skip. Never crash.
- **Sanitize strings** — registry and filesystem data may contain control characters or surrogates. The `ExtensionInfo.__post_init__` handles this.
- **Use `get_ctime_iso()`** for install timestamps when no better source is available.
- **Test with `-v`** — verbose mode should show what's being scanned and any issues.

### Commit Messages

- Use imperative mood: "Add support for X", not "Added support for X"
- Keep the first line under 72 characters
- Reference issue numbers if applicable: "Fix #42: Chrome extensions missing on Windows"

## Branch Strategy

- **`dev`** — active development branch. All PRs target this branch.
- **`prod`** — stable release branch. Protected. Merges from `dev` after review and CI passing.
- **Feature branches** — branch from `dev`, named `feature/description` or `fix/description`.

## Questions?

Open a [Discussion](https://github.com/avildhq/unshadow-ai/discussions) or reach out via issues.
