"""Local, lossless concatenation; all inputs must have compatible streams."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def require_ffmpeg() -> str:
    value = shutil.which("ffmpeg")
    if not value:
        raise RuntimeError("ffmpeg is required but was not found in PATH")
    return value


def concat_mp4(inputs: list[str | Path], output: str | Path) -> Path:
    ffmpeg = require_ffmpeg()
    output = Path(output).resolve()
    sources = [Path(p).resolve(strict=True) for p in inputs]
    if not sources or any(not p.is_file() or p == output for p in sources):
        raise ValueError("Use nonempty local input files distinct from the output")
    if any("\n" in str(p) or "\r" in str(p) for p in sources):
        raise ValueError("Newlines in media paths are unsupported")
    output.parent.mkdir(parents=True, exist_ok=True)
    # A unique list avoids concurrent job collisions and escaping handles apostrophes.
    with tempfile.TemporaryDirectory(prefix="video-concat-") as temp:
        listing = Path(temp) / "inputs.txt"
        lines = ["file '" + str(p).replace("'", "'\\''") + "'" for p in sources]
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,pipe",
                        "-i", str(listing), "-c", "copy", str(output)],
                       check=True, timeout=600)
    return output
