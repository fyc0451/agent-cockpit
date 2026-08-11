"""O3: log level gate, macOS rotation, launchd template paths."""
from __future__ import annotations

import logging
import os
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import log_config


def test_resolve_level_default_and_valid(monkeypatch):
    monkeypatch.delenv("COCKPIT_LOG_LEVEL", raising=False)
    name, level = log_config.resolve_level(None)
    assert name == "INFO"
    assert level == logging.INFO
    assert log_config.resolve_level("debug") == ("DEBUG", logging.DEBUG)
    assert log_config.resolve_level(" WARNING ") == ("WARNING", logging.WARNING)


@pytest.mark.parametrize("bad", ["verbose", "TRACE", "1", "INFOX", "debug-info"])
def test_resolve_level_rejects_invalid(bad):
    with pytest.raises(log_config.LogConfigError) as exc:
        log_config.resolve_level(bad)
    assert "COCKPIT_LOG_LEVEL 非法" in str(exc.value)


def test_resolve_level_blank_falls_back_to_default():
    assert log_config.resolve_level("")[0] == "INFO"
    assert log_config.resolve_level("   ")[0] == "INFO"


def test_configure_logging_linux_uses_stderr_only(tmp_path, monkeypatch):
    monkeypatch.delenv("COCKPIT_LOG_LEVEL", raising=False)
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "WARNING")
    name = log_config.configure_logging(platform="linux", install_dir=tmp_path)
    assert name == "WARNING"
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], logging.StreamHandler)
    # No app log file on Linux.
    assert not (tmp_path / "logs" / log_config.APP_LOG_NAME).exists()
    # Uvicorn shares root — no private handlers.
    assert logging.getLogger("uvicorn").handlers == []
    assert logging.getLogger("uvicorn").propagate is True
    assert logging.getLogger("agent-cockpit").propagate is True


def test_configure_logging_macos_rotating_file_and_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    log_dir = tmp_path / "logs"
    name = log_config.configure_logging(
        platform="darwin", log_dir=log_dir, install_dir=tmp_path
    )
    assert name == "INFO"
    app_log = log_dir / log_config.APP_LOG_NAME
    assert app_log.is_file()
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(app_log.stat().st_mode) == 0o600
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], logging.handlers.RotatingFileHandler)
    logging.getLogger("agent-cockpit").info("hello-o3")
    for h in root.handlers:
        h.flush()
    assert "hello-o3" in app_log.read_text(encoding="utf-8")
    # No second stream handler on macOS (launchd has separate bootstrap files).
    assert not any(
        type(h) is logging.StreamHandler and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )


def test_configure_logging_invalid_level_fails_closed(monkeypatch):
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "nope")
    with pytest.raises(log_config.LogConfigError):
        log_config.configure_logging(platform="linux")


def test_rotate_file_if_needed_retention(tmp_path):
    path = tmp_path / "launchd.stderr.log"
    path.write_bytes(b"x" * 100)
    # First rollover
    log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    assert path.read_bytes() == b""
    assert (tmp_path / "launchd.stderr.log.1").read_bytes() == b"x" * 100
    # Fill and roll again twice
    path.write_bytes(b"y" * 100)
    log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    path.write_bytes(b"z" * 100)
    log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    path.write_bytes(b"w" * 100)
    log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    assert (tmp_path / "launchd.stderr.log.1").exists()
    assert (tmp_path / "launchd.stderr.log.2").exists()
    assert (tmp_path / "launchd.stderr.log.3").exists()
    assert not (tmp_path / "launchd.stderr.log.4").exists()
    assert path.stat().st_size == 0


def test_prepare_macos_log_dir_special_paths(tmp_path):
    install = tmp_path / "inst with spaces" / "dir"
    install.mkdir(parents=True)
    log_dir = log_config.prepare_macos_log_dir(install)
    assert log_dir == install / "logs"
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    # Symlink log dir rejected
    bad_root = tmp_path / "bad"
    bad_root.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = bad_root / "logs"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(log_config.LogConfigError):
        log_config.ensure_private_log_dir(link)


def test_plist_template_uses_logs_launchd_paths():
    root = Path(__file__).resolve().parents[1]
    plist = ET.parse(root / "agent-cockpit.plist")
    values = [node.text for node in plist.findall(".//string")]
    assert "__INSTALL_DIR__/logs/launchd.stdout.log" in values
    assert "__INSTALL_DIR__/logs/launchd.stderr.log" in values
    assert not any(
        v and v.endswith("agent-cockpit.stdout.log") for v in values if v
    )


def test_launchd_sh_prepares_logs_and_exports_dir():
    root = Path(__file__).resolve().parents[1]
    text = (root / "launchd.sh").read_text(encoding="utf-8")
    assert "prepare_logs" in text
    assert "COCKPIT_LOG_DIR" in text
    assert "prepare_macos_log_dir" in text
    assert 'mkdir -p -m 700 "$INSTALL_DIR/logs"' in text


def test_exception_handler_does_not_log_query_or_body():
    """Regression: unhandled handler only logs method + path (no query/body)."""
    root = Path(__file__).resolve().parents[1]
    server = (root / "server.py").read_text(encoding="utf-8")
    assert 'logger.exception(\n        "unhandled exception method=%s path=%s"' in server
    assert "request.url.query" not in server
    assert "await request.body" not in server.split("unhandled exception")[1][:400]
