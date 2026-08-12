"""O3 R2: log level, privacy, rotation, secure paths, launchd template."""
from __future__ import annotations

import logging
import os
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agent_cockpit import log_config


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
    assert not (tmp_path / "logs" / log_config.APP_LOG_NAME).exists()
    assert logging.getLogger("uvicorn").handlers == []
    assert logging.getLogger("uvicorn").propagate is True
    access = logging.getLogger("uvicorn.access")
    assert access.disabled is True
    assert access.propagate is False


def test_configure_logging_macos_rotating_file_mode_from_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    install = tmp_path / "install"
    install.mkdir()
    name = log_config.configure_logging(platform="darwin", install_dir=install)
    assert name == "INFO"
    log_dir = install / "logs"
    app_log = log_dir / log_config.APP_LOG_NAME
    assert app_log.is_file()
    assert not app_log.is_symlink()
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(app_log.stat().st_mode) == 0o600
    assert app_log.stat().st_uid == os.getuid()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    from logging.handlers import RotatingFileHandler
    assert isinstance(root.handlers[0], RotatingFileHandler)
    assert type(root.handlers[0]).__name__ == "_PrivateRotatingFileHandler"
    assert root.handlers[0].maxBytes == log_config.MAC_MAX_BYTES
    assert root.handlers[0].backupCount == log_config.MAC_BACKUP_COUNT
    logging.getLogger("agent-cockpit").info("hello-o3")
    for h in root.handlers:
        h.flush()
    assert "hello-o3" in app_log.read_text(encoding="utf-8")


def test_configure_logging_invalid_level_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "nope")
    with pytest.raises(log_config.LogConfigError):
        log_config.configure_logging(platform="linux", install_dir=tmp_path)


def test_configure_logging_macos_requires_install_dir():
    with pytest.raises(log_config.LogConfigError):
        log_config.configure_logging(platform="darwin", install_dir=None)


def test_no_cockpit_log_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_LOG_DIR", str(tmp_path / "evil-elsewhere"))
    install = tmp_path / "install"
    install.mkdir()
    log_config.configure_logging(platform="darwin", install_dir=install)
    assert (install / "logs" / log_config.APP_LOG_NAME).is_file()
    assert not (tmp_path / "evil-elsewhere").exists()


def test_rotate_launchd_only_not_app_log(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    log_dir = log_config.ensure_private_log_dir(install / "logs")
    app = log_dir / log_config.APP_LOG_NAME
    app.write_bytes(b"A" * (log_config.LAUNCHD_MAX_BYTES + 10))
    os.chmod(app, 0o600)
    bootstrap = log_dir / log_config.LAUNCHD_STDERR_NAME
    bootstrap.write_bytes(b"B" * (log_config.LAUNCHD_MAX_BYTES + 10))
    os.chmod(bootstrap, 0o600)
    log_config.prepare_macos_log_dir(install)
    # launchd rotated
    assert bootstrap.stat().st_size == 0
    assert (log_dir / f"{log_config.LAUNCHD_STDERR_NAME}.1").exists()
    # app log untouched by prepare (handler thresholds only)
    assert app.stat().st_size == log_config.LAUNCHD_MAX_BYTES + 10
    assert not (log_dir / f"{log_config.APP_LOG_NAME}.1").exists()


def test_rotate_file_if_needed_retention(tmp_path):
    path = tmp_path / "launchd.stderr.log"
    path.write_bytes(b"x" * 100)
    os.chmod(path, 0o600)
    log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    assert path.read_bytes() == b""
    assert (tmp_path / "launchd.stderr.log.1").read_bytes() == b"x" * 100
    for payload in (b"y" * 100, b"z" * 100, b"w" * 100):
        path.write_bytes(payload)
        os.chmod(path, 0o600)
        log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    assert (tmp_path / "launchd.stderr.log.1").exists()
    assert (tmp_path / "launchd.stderr.log.2").exists()
    assert (tmp_path / "launchd.stderr.log.3").exists()
    assert not (tmp_path / "launchd.stderr.log.4").exists()
    assert path.stat().st_size == 0


def test_rejects_symlink_log_dir(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = install / "logs"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(log_config.LogConfigError):
        log_config.ensure_private_log_dir(link)
    with pytest.raises(log_config.LogConfigError):
        log_config.configure_logging(platform="darwin", install_dir=install)


def test_rejects_symlink_app_log_leaf(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    log_dir = log_config.ensure_private_log_dir(install / "logs")
    outside = tmp_path / "secret"
    outside.write_text("leak", encoding="utf-8")
    leaf = log_dir / log_config.APP_LOG_NAME
    leaf.symlink_to(outside)
    with pytest.raises(log_config.LogConfigError):
        log_config.configure_logging(platform="darwin", install_dir=install)


def test_rejects_parent_symlink_in_log_chain(tmp_path):
    real = tmp_path / "real-install"
    real.mkdir()
    linked = tmp_path / "linked-install"
    linked.symlink_to(real, target_is_directory=True)
    # install_dir itself is symlink — default_log_dir uses it; chain check must fail
    with pytest.raises(log_config.LogConfigError):
        log_config.prepare_macos_log_dir(linked)


def test_access_logger_does_not_record_secret_query(tmp_path, monkeypatch):
    """Runtime counterexample: secret query must never reach the app sink."""
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    install = tmp_path / "install"
    install.mkdir()
    log_config.configure_logging(platform="darwin", install_dir=install)
    secret = "token=super-secret-token&q=1"
    access = logging.getLogger("uvicorn.access")
    # Even if something logs a full_path-like line, access is disabled.
    access.info('%s - "%s" %s', "127.0.0.1", f"GET /api/x?{secret} HTTP/1.1", 200)
    logging.getLogger("agent-cockpit").info("benign")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (install / "logs" / log_config.APP_LOG_NAME).read_text(encoding="utf-8")
    assert "super-secret-token" not in text
    assert "benign" in text


def test_server_main_disables_access_log_and_uses_install_dir():
    root = Path(__file__).resolve().parents[1]
    server = (root / "agent_cockpit" / "server.py").read_text(encoding="utf-8")
    assert "access_log=False" in server
    assert "_install_dir = ROOT_DIR" in server
    assert "os.environ.get(\"COCKPIT_LOG_DIR\"" not in server
    assert "os.environ[\"COCKPIT_LOG_DIR\"]" not in server


def test_plist_template_uses_logs_launchd_paths_and_umask():
    root = Path(__file__).resolve().parents[1]
    tree = ET.parse(root / "agent-cockpit.plist")
    plist = tree.getroot()
    values = [node.text for node in plist.findall(".//string")]
    assert "__INSTALL_DIR__/logs/launchd.stdout.log" in values
    assert "__INSTALL_DIR__/logs/launchd.stderr.log" in values
    assert not any(v and v.endswith("agent-cockpit.stdout.log") for v in values if v)
    # launchd Umask: integer 63 == 077 octal (applied before Standard*Path open)
    keys = [k.text for k in plist.findall("./dict/key")]
    assert "Umask" in keys
    # sibling integer after Umask key
    children = list(plist.find("dict"))
    umask_val = None
    for i, node in enumerate(children):
        if node.tag == "key" and node.text == "Umask":
            umask_val = children[i + 1]
            break
    assert umask_val is not None and umask_val.tag == "integer"
    assert umask_val.text == "63"


def test_launchd_sh_no_shell_mkdir_and_sys_path():
    root = Path(__file__).resolve().parents[1]
    text = (root / "launchd.sh").read_text(encoding="utf-8")
    assert "prepare_logs" in text
    assert "sys.path.insert" in text
    assert "mkdir -p -m 700" not in text
    assert "COCKPIT_LOG_DIR" not in text
    assert "sys.path.insert" in text
    prepare_body = text.split("prepare_logs()")[1].split("case ")[0]
    assert "|| true" not in prepare_body
    assert "umask 077" in text
    assert '[[ -L "$INSTALL_DIR/logs" ]]' in text
    assert "Path(sys.argv[1]).resolve()" not in text
    assert "install.is_absolute()" in text or "is_absolute()" in text
    assert "install.is_symlink()" in text or "is_symlink()" in text


def test_arbitrary_cwd_prepare_uses_install_sys_path(tmp_path, monkeypatch):
    """prepare_macos_log_dir import must not depend on process cwd."""
    install = tmp_path / "install"
    install.mkdir()
    # Copy the minimal package so sys.path=[install] works like launchd.sh.
    import shutil

    root = Path(__file__).resolve().parents[1]
    package = install / "agent_cockpit"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="ascii")
    shutil.copy(root / "agent_cockpit" / "log_config.py", package / "log_config.py")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    import runpy
    import subprocess
    import sys

    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(install)!r})\n"
        "from pathlib import Path\n"
        "from agent_cockpit.log_config import prepare_macos_log_dir\n"
        f"prepare_macos_log_dir(Path({str(install)!r}))\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, cwd=tmp_path / "other")
    assert (install / "logs").is_dir()
    assert stat.S_IMODE((install / "logs").stat().st_mode) == 0o700


def test_exception_handler_does_not_log_query_or_body():
    root = Path(__file__).resolve().parents[1]
    server = (root / "agent_cockpit" / "server.py").read_text(encoding="utf-8")
    assert 'logger.exception(\n        "unhandled exception method=%s path=%s"' in server
    chunk = server.split("unhandled exception")[1][:500]
    assert "request.url.query" not in chunk
    assert "await request.body" not in chunk


def test_rotate_rejects_backup_symlinks_including_broken(tmp_path):
    path = tmp_path / "launchd.stderr.log"
    path.write_bytes(b"x" * 100)
    os.chmod(path, 0o600)
    # broken symlink in .1 slot
    (tmp_path / "launchd.stderr.log.1").symlink_to(tmp_path / "missing-target")
    with pytest.raises(log_config.LogConfigError):
        log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    # original must remain (fail closed, no silent keep-and-rotate)
    assert path.read_bytes() == b"x" * 100


def test_rotate_rejects_hardlinked_base_or_backup(tmp_path):
    path = tmp_path / "launchd.stderr.log"
    path.write_bytes(b"x" * 100)
    os.chmod(path, 0o600)
    link = tmp_path / "hard-alias"
    os.link(path, link)
    assert path.stat().st_nlink == 2
    with pytest.raises(log_config.LogConfigError) as exc:
        log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    assert "硬链接" in str(exc.value)
    assert path.read_bytes() == b"x" * 100
    # backup hardlink
    path2 = tmp_path / "launchd.stdout.log"
    path2.write_bytes(b"y" * 100)
    os.chmod(path2, 0o600)
    b1 = tmp_path / "launchd.stdout.log.1"
    b1.write_bytes(b"old")
    os.chmod(b1, 0o600)
    os.link(b1, tmp_path / "b1-alias")
    with pytest.raises(log_config.LogConfigError):
        log_config.rotate_file_if_needed(path2, max_bytes=50, backup_count=3)
    assert path2.read_bytes() == b"y" * 100
    assert b1.read_bytes() == b"old"


def test_rotate_rejects_existing_directory_base(tmp_path):
    path = tmp_path / "launchd.stderr.log"
    path.mkdir()
    with pytest.raises(log_config.LogConfigError) as exc:
        log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    assert "普通文件" in str(exc.value)
    assert path.is_dir()


def test_rotate_tightens_retained_backups_when_under_threshold(tmp_path):
    path = tmp_path / "launchd.stderr.log"
    path.write_bytes(b"small")
    os.chmod(path, 0o644)
    b1 = tmp_path / "launchd.stderr.log.1"
    b1.write_bytes(b"backup1")
    os.chmod(b1, 0o644)
    b2 = tmp_path / "launchd.stderr.log.2"
    b2.write_bytes(b"backup2")
    os.chmod(b2, 0o640)
    log_config.rotate_file_if_needed(path, max_bytes=10_000, backup_count=3)
    assert path.read_bytes() == b"small"
    assert b1.read_bytes() == b"backup1"
    assert b2.read_bytes() == b"backup2"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(b1.stat().st_mode) == 0o600
    assert stat.S_IMODE(b2.stat().st_mode) == 0o600


def test_app_handler_rollover_forces_0600_under_umask_022(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    install = tmp_path / "install"
    install.mkdir()
    # Simulate permissive umask for new files created during rollover.
    old_umask = os.umask(0o022)
    try:
        log_config.configure_logging(platform="darwin", install_dir=install)
        app = install / "logs" / log_config.APP_LOG_NAME
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        handler.maxBytes = 64
        handler.backupCount = 2
        logger = logging.getLogger("agent-cockpit")
        for _ in range(20):
            logger.info("x" * 40)
            for h in logging.getLogger().handlers:
                h.flush()
        assert app.is_file()
        assert stat.S_IMODE(app.stat().st_mode) == 0o600
        assert app.stat().st_nlink == 1
        # All retained app backups must be exact 0600 / nlink1 after rollover.
        found_backup = False
        for index in range(1, handler.backupCount + 1):
            rotated = install / "logs" / f"{log_config.APP_LOG_NAME}.{index}"
            if not rotated.exists():
                continue
            found_backup = True
            assert rotated.is_file() and not rotated.is_symlink()
            assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
            assert rotated.stat().st_nlink == 1
        assert found_backup, "expected at least one rotated backup under small maxBytes"
    finally:
        os.umask(old_umask)


def test_prepare_app_log_rejects_hardlink_without_changing_alias_mode(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    install = tmp_path / "install"
    install.mkdir()
    log_dir = log_config.ensure_private_log_dir(install / "logs")
    app = log_dir / log_config.APP_LOG_NAME
    app.write_bytes(b"data")
    os.chmod(app, 0o644)
    alias = log_dir / "alias"
    os.link(app, alias)
    before_mode = stat.S_IMODE(alias.stat().st_mode)
    assert before_mode == 0o644
    with pytest.raises(log_config.LogConfigError) as exc:
        log_config.configure_logging(platform="darwin", install_dir=install)
    assert "硬链接" in str(exc.value)
    # Must not fchmod the shared inode before rejecting.
    assert stat.S_IMODE(alias.stat().st_mode) == before_mode == 0o644
    assert stat.S_IMODE(app.stat().st_mode) == 0o644


def test_app_rollover_precheck_rejects_hardlinked_backup_zero_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    install = tmp_path / "install"
    install.mkdir()
    log_config.configure_logging(platform="darwin", install_dir=install)
    log_dir = install / "logs"
    app = log_dir / log_config.APP_LOG_NAME
    base_bytes = app.read_bytes()
    slot1 = log_dir / f"{log_config.APP_LOG_NAME}.1"
    slot1.write_bytes(b"backup-one")
    os.chmod(slot1, 0o600)
    os.link(slot1, log_dir / "slot1-alias")
    handler = logging.getLogger().handlers[0]
    with pytest.raises(log_config.LogConfigError):
        handler.doRollover()
    assert app.read_bytes() == base_bytes
    assert slot1.read_bytes() == b"backup-one"
    assert slot1.stat().st_nlink == 2


def test_app_rollover_precheck_rejects_directory_slot_with_valid_sibling(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    install = tmp_path / "install"
    install.mkdir()
    log_config.configure_logging(platform="darwin", install_dir=install)
    log_dir = install / "logs"
    app = log_dir / log_config.APP_LOG_NAME
    base_bytes = app.read_bytes()
    slot1 = log_dir / f"{log_config.APP_LOG_NAME}.1"
    slot1.write_bytes(b"valid-backup")
    os.chmod(slot1, 0o600)
    slot2 = log_dir / f"{log_config.APP_LOG_NAME}.2"
    slot2.mkdir()
    handler = logging.getLogger().handlers[0]
    with pytest.raises(log_config.LogConfigError):
        handler.doRollover()
    assert app.read_bytes() == base_bytes
    assert slot1.read_bytes() == b"valid-backup"
    assert slot2.is_dir()
    assert not (log_dir / f"{log_config.APP_LOG_NAME}.3").exists()


def test_configure_rejects_preexisting_unsafe_app_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    install = tmp_path / "install"
    install.mkdir()
    log_dir = log_config.ensure_private_log_dir(install / "logs")
    app = log_dir / log_config.APP_LOG_NAME
    app.write_bytes(b"ok")
    os.chmod(app, 0o600)
    bad = log_dir / f"{log_config.APP_LOG_NAME}.1"
    bad.mkdir()
    with pytest.raises(log_config.LogConfigError):
        log_config.configure_logging(platform="darwin", install_dir=install)


def test_configure_illegal_backup_does_not_mutate_existing_base(tmp_path, monkeypatch):
    """Existing base 0644+bytes + illegal .1/.2: base mode/bytes and all slots unchanged."""
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    install = tmp_path / "install"
    install.mkdir()
    log_dir = log_config.ensure_private_log_dir(install / "logs")
    app = log_dir / log_config.APP_LOG_NAME
    base_bytes = b"keep-these-bytes"
    app.write_bytes(base_bytes)
    os.chmod(app, 0o644)
    slot1 = log_dir / f"{log_config.APP_LOG_NAME}.1"
    slot1.write_bytes(b"slot1-ok")
    os.chmod(slot1, 0o640)
    slot2 = log_dir / f"{log_config.APP_LOG_NAME}.2"
    slot2.mkdir()  # illegal
    before = {
        "base": app.read_bytes(),
        "base_mode": stat.S_IMODE(app.stat().st_mode),
        "s1": slot1.read_bytes(),
        "s1_mode": stat.S_IMODE(slot1.stat().st_mode),
        "s2_dir": slot2.is_dir(),
    }
    with pytest.raises(log_config.LogConfigError):
        log_config.configure_logging(platform="darwin", install_dir=install)
    assert app.read_bytes() == before["base"] == base_bytes
    assert stat.S_IMODE(app.stat().st_mode) == before["base_mode"] == 0o644
    assert slot1.read_bytes() == before["s1"] == b"slot1-ok"
    assert stat.S_IMODE(slot1.stat().st_mode) == before["s1_mode"] == 0o640
    assert slot2.is_dir() is True


def test_configure_illegal_backup_does_not_create_missing_base(tmp_path, monkeypatch):
    """Missing base + illegal backup: must not create base."""
    monkeypatch.setenv("COCKPIT_LOG_LEVEL", "INFO")
    install = tmp_path / "install"
    install.mkdir()
    log_dir = log_config.ensure_private_log_dir(install / "logs")
    app = log_dir / log_config.APP_LOG_NAME
    assert not app.exists()
    bad = log_dir / f"{log_config.APP_LOG_NAME}.1"
    bad.symlink_to(tmp_path / "missing-target")
    with pytest.raises(log_config.LogConfigError):
        log_config.configure_logging(platform="darwin", install_dir=install)
    assert not app.exists()
    assert bad.is_symlink()


def test_rotate_rejects_directory_slot_with_existing_backup_unchanged(tmp_path):
    """Directory in a slot + another valid backup: fail closed, zero mutation."""
    path = tmp_path / "launchd.stderr.log"
    base_bytes = b"BASE" * 30
    path.write_bytes(base_bytes)
    os.chmod(path, 0o600)
    slot1 = tmp_path / "launchd.stderr.log.1"
    slot1_bytes = b"OLD1-CONTENT"
    slot1.write_bytes(slot1_bytes)
    os.chmod(slot1, 0o644)  # mode may be tightened only after full precheck passes
    slot2 = tmp_path / "launchd.stderr.log.2"
    slot2.mkdir()  # directory/FIFO/socket class: not a regular file
    before = {
        "base": path.read_bytes(),
        "s1": slot1.read_bytes(),
        "s2_is_dir": slot2.is_dir(),
        "s1_mode": stat.S_IMODE(slot1.stat().st_mode),
        "base_mode": stat.S_IMODE(path.stat().st_mode),
    }
    with pytest.raises(log_config.LogConfigError) as exc:
        log_config.rotate_file_if_needed(path, max_bytes=50, backup_count=3)
    assert "普通文件" in str(exc.value) or "符号链接" in str(exc.value) or "owner" in str(
        exc.value
    )
    assert path.read_bytes() == before["base"] == base_bytes
    assert slot1.read_bytes() == before["s1"] == slot1_bytes
    assert slot2.is_dir() is True
    assert stat.S_IMODE(slot1.stat().st_mode) == before["s1_mode"]
    assert stat.S_IMODE(path.stat().st_mode) == before["base_mode"]
    assert not (tmp_path / "launchd.stderr.log.3").exists()


def test_prepare_rejects_relative_and_symlink_install(tmp_path):
    with pytest.raises(log_config.LogConfigError):
        log_config.prepare_macos_log_dir("relative-install")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(log_config.LogConfigError):
        log_config.prepare_macos_log_dir(link)
