#!/usr/bin/env python3
"""Source allowlist and basic secret checks; review content before every publish."""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ALLOWED = frozenset('''README.md AGENTS.md docs/AGENT_SYSTEM_INSTRUCTIONS.md pyproject.toml .gitignore
.github/workflows/ci.yml .github/workflows/private-check.yml .github/workflows/produce.yml
scripts/produce.py requests/current.txt
scripts/public_boundary.py scripts/private_check.py scripts/concat_video.py
src/ai_video/__init__.py src/ai_video/metadata.py
src/ai_video/director/__init__.py src/ai_video/director/project.py
src/ai_video/director/continuity.py src/ai_video/media/__init__.py
src/ai_video/media/ffmpeg.py tests/test_pipeline.py'''.split())
SECRET = re.compile(
    r'github_pat_[A-Za-z0-9_]{16,}|gh[pousr]_[A-Za-z0-9]{16,}'
    r'|sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'
    r'|data:(?:image|audio|video)/[^;]+;base64,[A-Za-z0-9+/]{64,}', re.I)


def inspect(name: str, data: bytes) -> None:
    p = PurePosixPath(name)
    if name not in ALLOWED or p.is_absolute() or '..' in p.parts or str(p) != name:
        raise ValueError('Unapproved public path')
    if len(data) > 512_000 or b'\0' in data:
        raise ValueError('Binary or oversized file')
    text = data.decode('utf-8')
    if SECRET.search(text):
        raise ValueError('Possible secret or embedded media')
    if name.endswith('.py'):
        ast.parse(text)


def audit(root: Path) -> int:
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).split(b'\0')
    names = [n.decode('utf-8') for n in names if n]
    if not names:
        raise ValueError('Empty public tree')
    for name in names:
        p = root / name
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unsafe public file')
        inspect(name, p.read_bytes())
    return len(names)


if __name__ == '__main__':
    try:
        print(f'Public boundary passed: {audit(Path.cwd())} files')
    except Exception:
        print('Public boundary failed; inspect locally before publishing.', file=sys.stderr)
        raise SystemExit(1)
