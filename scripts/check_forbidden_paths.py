"""Reject audio files and private-data paths in the repository.

AGENTS.md rules 9-10 and ADR-008: audio is never committed, and local/, recordings/ and
captures/ hold private data. CI runs this on every tracked file.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from pathlib import PurePosixPath

AUDIO_EXTENSIONS = frozenset(
    {
        ".wav", ".wave", ".aif", ".aiff", ".aifc", ".flac", ".mp3", ".ogg", ".oga",
        ".opus", ".m4a", ".aac", ".wma", ".caf", ".w64", ".rf64", ".ape", ".wv",
    }
)  # fmt: skip
FORBIDDEN_DIRS = frozenset({"local", "recordings", "captures"})


def violations(paths: Iterable[str]) -> list[str]:
    """Return one message per path that must not be committed."""
    found: list[str] = []
    for raw in paths:
        path = PurePosixPath(raw)
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            found.append(f"{raw}: audio file")
        elif path.parts and path.parts[0] in FORBIDDEN_DIRS:
            found.append(f"{raw}: private directory '{path.parts[0]}/'")
    return found


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return [p for p in out.split("\0") if p]


def main() -> int:
    bad = violations(tracked_files())
    if bad:
        print("Forbidden files are tracked by git:", file=sys.stderr)
        for line in bad:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
