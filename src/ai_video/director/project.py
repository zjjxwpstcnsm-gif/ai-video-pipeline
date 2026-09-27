from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class Shot:
    shot_id: str
    duration: int
    prompt: str
    mode: str
    aspect_ratio: str
    first_frame: str | None
    last_frame: str | None
    reference_images: list[str]
    raw: dict[str, Any]


def load_yaml(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be an object: {path}")
    return data


def load_shot(path: str | Path, project_defaults: dict[str, Any] | None = None) -> Shot:
    raw = load_yaml(path)
    defaults = project_defaults or {}
    prompt = raw.get("prompt") or ""
    prompt_file = raw.get("prompt_file")
    if prompt_file:
        p = Path(path).parent.parent / str(prompt_file)
        prompt = p.read_text(encoding="utf-8")
    return Shot(
        shot_id=str(raw.get("shot_id") or Path(path).stem),
        duration=int(raw.get("duration") or defaults.get("shot_seconds") or 5),
        prompt=str(prompt),
        mode=str(raw.get("mode") or "text"),
        aspect_ratio=str(raw.get("aspect_ratio") or defaults.get("aspect_ratio") or "9:16"),
        first_frame=raw.get("first_frame"),
        last_frame=raw.get("last_frame"),
        reference_images=list(raw.get("reference_images") or []),
        raw=raw,
    )
