"""Benchmark source identity for a checkout or an explicitly recorded source archive."""
import json
from pathlib import Path
import subprocess


def source_revision(source: Path) -> str:
    if (source / ".git").exists():
        if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"], text=True).strip():
            raise ValueError("Benchmark source has tracked edits")
        return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    return json.loads((source / "source-revision.json").read_text())["commit"]
