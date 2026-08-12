from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cockpit import github_release_auth


TOKEN = "github_pat_" + "a" * 32


def _write_token(home: Path, payload: bytes = (TOKEN + "\n").encode()) -> Path:
    path = home / ".config" / "agent-cockpit" / "github-release.token"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def test_missing_token_is_valid_for_public_releases(tmp_path: Path) -> None:
    assert github_release_auth.load_github_release_token(home=tmp_path) is None


def test_loads_exact_private_token_without_exposing_path(tmp_path: Path) -> None:
    _write_token(tmp_path)
    assert github_release_auth.load_github_release_token(home=tmp_path) == TOKEN


@pytest.mark.parametrize("damage", ["mode", "symlink", "hardlink", "invalid"])
def test_rejects_unsafe_or_invalid_token_file(tmp_path: Path, damage: str) -> None:
    path = _write_token(tmp_path)
    if damage == "mode":
        path.chmod(0o644)
    elif damage == "symlink":
        target = tmp_path / "outside"
        target.write_text(TOKEN)
        path.unlink()
        path.symlink_to(target)
    elif damage == "hardlink":
        os.link(path, tmp_path / "outside")
    else:
        path.write_text("not a token\n")

    with pytest.raises(github_release_auth.GitHubReleaseAuthError) as exc:
        github_release_auth.load_github_release_token(home=tmp_path)
    assert str(path) not in str(exc.value)


def test_environment_override_requires_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(
        github_release_auth.GitHubReleaseAuthError,
        match="github_token_path_invalid",
    ):
        github_release_auth.load_github_release_token(
            environ={github_release_auth.TOKEN_FILE_ENV: "relative"},
            home=tmp_path,
        )
