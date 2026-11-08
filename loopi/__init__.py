from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
_version_file = PACKAGE_DIR.parent / "VERSION"
VERSION = _version_file.read_text().strip() if _version_file.exists() else "0.0.0"
