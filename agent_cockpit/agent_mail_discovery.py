"""Shared Agent Mail database discovery with legacy-compatible fallback."""
from __future__ import annotations

from pathlib import Path


def agent_mail_db_candidates(
    *,
    home: Path | None = None,
    data_home: Path | None = None,
) -> tuple[Path, Path]:
    """Return the discoverable XDG path followed by the legacy-compatible path."""
    home_root = (Path.home() if home is None else home).expanduser()
    xdg_root = (
        home_root / ".local" / "share"
        if data_home is None
        else data_home.expanduser()
    )
    return (
        xdg_root / "mcp_agent_mail" / "storage.sqlite3",
        home_root / "mcp_agent_mail" / "storage.sqlite3",
    )


def discover_agent_mail_db_path(
    *,
    configured: str | None = None,
    home: Path | None = None,
    data_home: Path | None = None,
) -> Path:
    """Resolve explicit config, then an installed XDG/legacy DB, else legacy default."""
    if configured:
        return Path(configured).expanduser()

    xdg, legacy = agent_mail_db_candidates(
        home=home,
        data_home=data_home,
    )
    return next((path for path in (xdg, legacy) if path.is_file()), legacy)
