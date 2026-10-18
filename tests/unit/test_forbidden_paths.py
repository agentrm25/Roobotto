import pytest

import check_forbidden_paths as guard


@pytest.mark.parametrize(
    "path",
    [
        "take1.wav",
        "corpus/audio/TAKE.FLAC",
        "docs/demo.mp3",
        "a/b/c.m4a",
        "local/songs/x.yaml",
        "recordings/2026-10-17/notes.txt",
        "captures/bench.log",
    ],
)
def test_rejects(path: str) -> None:
    assert len(guard.violations([path])) == 1


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "src/roobotto/__init__.py",
        "corpus/take1.yaml",
        "docs/local-setup.md",
        "examples/local/rig.yaml",
        "tests/unit/test_wav_header.py",
    ],
)
def test_allows(path: str) -> None:
    assert guard.violations([path]) == []


def test_reports_every_offender() -> None:
    assert len(guard.violations(["a.wav", "ok.md", "local/x"])) == 2


def test_repository_is_clean() -> None:
    assert guard.violations(guard.tracked_files()) == []
