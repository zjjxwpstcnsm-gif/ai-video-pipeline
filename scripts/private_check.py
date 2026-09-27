#!/usr/bin/env python3
"""Read private config, never execute its code or publish its contents."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


class CheckError(RuntimeError):
    pass


def repository_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise CheckError("Invalid repository selection")
    return value


def clean_environment(home: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if k in os.environ}
    env.update(HOME=str(home), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def run_quiet(args: list[str], *, cwd: Path, env: dict[str, str], input_text: str | None = None) -> None:
    result = subprocess.run(args, cwd=cwd, env=env, input=input_text, text=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=600, check=False)
    if result.returncode:
        raise CheckError("Private operation failed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    repo = repository_name(os.environ.get("ASSETS_REPOSITORY", ""))
    token = os.environ.pop("ASSETS_PAT", "")
    if not token:
        raise CheckError("Missing read-only credential")
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ai-video-pipeline"})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
        meta = json.load(response)
    if meta.get("private") is not True:
        raise CheckError("Assets repository must remain private")
    branch = meta.get("default_branch", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch) or ".." in branch:
        raise CheckError("Invalid assets branch")
    del req
    with tempfile.TemporaryDirectory(prefix="private-video-check-") as temp:
        home = Path(temp)
        workspace = home / "workspace"
        askpass = home / "askpass.sh"
        askpass.write_text('#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" "x-access-token";;\n*) printf "%s\\n" "$ASSETS_PAT";;\nesac\n')
        askpass.chmod(0o700)
        env = clean_environment(home)
        env.update(ASSETS_PAT=token, GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0",
                   GIT_CONFIG_NOSYSTEM="1", GIT_LFS_SKIP_SMUDGE="1")
        git = ["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
               "-c", "http.followRedirects=false", "-c", "protocol.file.allow=never",
               "-c", "protocol.ext.allow=never"]
        run_quiet(git + ["clone", "--quiet", "--filter=blob:none", "--no-checkout", "--depth=1",
                        "--single-branch", "--branch", branch, f"https://github.com/{repo}.git", str(workspace)],
                  cwd=home, env=env)
        run_quiet(git + ["sparse-checkout", "set", "--no-cone", "--stdin"], cwd=workspace,
                  env=env, input_text="*.yaml\n*.yml\n*.json\n")
        run_quiet(git + ["checkout", "--quiet", "--force", branch], cwd=workspace, env=env)
        env.clear()
        token = ""
        askpass.unlink()
        # -I ignores private cwd/PYTHONPATH/sitecustomize. Only the installed public
        # package runs. No private Python, shell, workflows or pip metadata execute.
        run_quiet([sys.executable, "-I", "-m", "ai_video.metadata", str(workspace)],
                  cwd=home, env=clean_environment(home))
    print("Private metadata syntax check passed. No generation, uploads or private logs.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Private check failed. Check Environment secrets and validate locally; details are suppressed.", file=sys.stderr)
        raise SystemExit(1)
