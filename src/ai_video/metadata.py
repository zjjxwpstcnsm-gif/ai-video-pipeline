"""Read-only YAML/JSON syntax check. Not the legacy production validator."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOTS = ("assets", "projects", "profiles", "configs", "workflows")
MAX_BYTES = 2_000_000
MAX_FILES = 10_000
MAX_TOTAL = 50_000_000


class MetadataError(ValueError):
    pass


def parse_document(path: Path) -> None:
    data = path.read_bytes()
    if len(data) > MAX_BYTES or b"\0" in data:
        raise MetadataError("Invalid metadata size or encoding")
    text = data.decode("utf-8-sig")
    if path.suffix == ".json":
        value = json.loads(text)
    else:
        # Bound aliases before parsing to reject expansion abuse in config input.
        if sum(isinstance(t, yaml.tokens.AliasToken) for t in yaml.scan(text)) > 32:
            raise MetadataError("Too many YAML aliases")
        value = yaml.safe_load(text)
    if value is not None and not isinstance(value, (dict, list)):
        raise MetadataError("Metadata root must be an object or array")


def validate_metadata(root: Path) -> int:
    root = root.resolve(strict=True)
    total = count = 0
    for name in ROOTS:
        base = root / name
        if base.is_symlink():
            raise MetadataError("Symlinks require local review")
        if not base.exists():
            continue
        if not base.is_dir():
            raise MetadataError("Metadata root is not a directory")
        for path in base.rglob("*"):
            if path.is_symlink():
                raise MetadataError("Symlinks require local review")
            if path.suffix.lower() not in {".yaml", ".yml", ".json"} or not path.is_file():
                continue
            if not path.resolve().is_relative_to(root):
                raise MetadataError("Path escapes the workspace")
            size = path.stat().st_size
            count += 1
            total += size
            if count > MAX_FILES or total > MAX_TOTAL or size > MAX_BYTES:
                raise MetadataError("Metadata check size limit exceeded")
            parse_document(path)
    if not count:
        raise MetadataError("No metadata found")
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    try:
        validate_metadata(args.workspace)
    except Exception:
        print("Metadata syntax check failed; inspect locally. Private details are suppressed.", file=sys.stderr)
        return 1
    print("Metadata syntax check passed. Production rules and media bytes were not checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
