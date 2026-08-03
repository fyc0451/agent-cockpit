import xml.etree.ElementTree as ET
from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "install.sh", "upgrade.sh", "uninstall.sh", "doctor.sh", "launchd.sh",
    "install-agent-mail-tools.sh",
)
DOCS = ("SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md")


def test_release_files_exist_and_scripts_are_valid():
    for name in SCRIPTS + DOCS + ("agent-cockpit.service", "agent-cockpit.plist"):
        assert (ROOT / name).is_file(), f"missing release file: {name}"
    for name in SCRIPTS:
        path = ROOT / name
        assert path.stat().st_mode & 0o111, f"script is not executable: {name}"
        subprocess.run(["bash", "-n", str(path)], check=True)
    ET.parse(ROOT / "agent-cockpit.plist")


def test_readme_and_ci_have_no_release_placeholders():
    readme = (ROOT / "README.md").read_text()
    workflow = (ROOT / ".github/workflows/test.yml").read_text()

    assert "github.com/YOUR/" not in readme
    assert "github.com/)" not in readme
    assert "agent-mail-dashboard.service" not in readme
    assert "permissions:" in workflow
    assert "timeout-minutes:" in workflow


def test_installers_migrate_legacy_service_name():
    for name in ("install.sh", "upgrade.sh"):
        script = (ROOT / name).read_text()
        assert "disable --now agent-mail-dashboard.service" in script
        assert "enable --now agent-cockpit.service" in script


def test_installers_manage_macos_launch_agent():
    installer = (ROOT / "install.sh").read_text()
    upgrader = (ROOT / "upgrade.sh").read_text()
    uninstaller = (ROOT / "uninstall.sh").read_text()
    launchd = (ROOT / "launchd.sh").read_text()

    assert '"$INSTALL_DIR/launchd.sh" install' in installer
    assert '"$INSTALL_DIR/launchd.sh" restart' in upgrader
    assert '"$INSTALL_DIR/launchd.sh" uninstall' in uninstaller
    assert 'launchctl bootstrap "$DOMAIN" "$PLIST_PATH"' in launchd
    assert 'launchctl kickstart -k "$SERVICE"' in launchd
    assert '"$cwd" != "$INSTALL_DIR"' in launchd
    assert '"$command" != *server.py*' in launchd
    assert "Agent Cockpit LaunchAgent 正在运行" in (ROOT / "doctor.sh").read_text()


def test_launchd_installer_renders_and_restarts_service(tmp_path):
    install_dir = tmp_path / "agent-cockpit"
    install_dir.mkdir()
    launcher = install_dir / "launchd.sh"
    launcher.write_text((ROOT / "launchd.sh").read_text())
    launcher.chmod(0o755)
    (install_dir / "agent-cockpit.plist").write_text(
        (ROOT / "agent-cockpit.plist").read_text()
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "launchctl-calls"
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{calls}"\n'
    )
    fake_launchctl.chmod(0o755)
    fake_lsof = fake_bin / "lsof"
    fake_lsof.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_lsof.chmod(0o755)
    home = tmp_path / "home"
    env = {**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    result = subprocess.run(
        [str(launcher), "restart"], text=True, capture_output=True, env=env
    )

    assert result.returncode == 0, result.stderr
    plist = home / "Library/LaunchAgents/io.github.fyc0451.agent-cockpit.plist"
    assert plist.is_file()
    assert str(install_dir) in plist.read_text()
    recorded = calls.read_text()
    assert "bootstrap" in recorded
    assert "kickstart -k" in recorded


def test_launchd_installer_refuses_unrelated_port_listener(tmp_path):
    install_dir = tmp_path / "agent-cockpit"
    install_dir.mkdir()
    launcher = install_dir / "launchd.sh"
    launcher.write_text((ROOT / "launchd.sh").read_text())
    launcher.chmod(0o755)
    (install_dir / "agent-cockpit.plist").write_text(
        (ROOT / "agent-cockpit.plist").read_text()
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_launchctl.chmod(0o755)
    fake_lsof = fake_bin / "lsof"
    fake_lsof.write_text(
        '#!/usr/bin/env bash\n'
        'if [[ "$*" == *"-tiTCP:"* ]]; then echo 12345; else printf "p12345\\nn/other/project\\n"; fi\n'
    )
    fake_lsof.chmod(0o755)
    fake_ps = fake_bin / "ps"
    fake_ps.write_text("#!/usr/bin/env bash\necho 'python server.py'\n")
    fake_ps.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [str(launcher), "restart"], text=True, capture_output=True, env=env
    )

    assert result.returncode != 0
    assert "已被其他进程占用" in result.stderr


def test_installer_rejects_unsafe_custom_path(tmp_path):
    installer = tmp_path / "install.sh"
    installer.write_text((ROOT / "install.sh").read_text())
    installer.chmod(0o755)
    env = {**os.environ, "AGENT_COCKPIT_DIR": str(tmp_path / "bad&path")}

    result = subprocess.run(
        [str(installer)], text=True, capture_output=True, env=env
    )

    assert result.returncode != 0
    assert "安装路径" in result.stderr


def test_doctor_treats_quoted_empty_token_as_empty(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('COCKPIT_HOST="0.0.0.0"\nCOCKPIT_TOKEN=""\n')
    env = {**os.environ, "HOME": str(tmp_path), "AGENT_COCKPIT_ENV": str(env_file)}

    result = subprocess.run(
        [str(ROOT / "doctor.sh")], text=True, capture_output=True, env=env
    )

    assert "非回环监听必须设置 COCKPIT_TOKEN" in result.stdout


def test_doctor_rejects_export_syntax_not_supported_by_systemd(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("export COCKPIT_HOST=0.0.0.0\nexport COCKPIT_TOKEN=secret\n")
    env = {**os.environ, "HOME": str(tmp_path), "AGENT_COCKPIT_ENV": str(env_file)}

    result = subprocess.run(
        [str(ROOT / "doctor.sh")], text=True, capture_output=True, env=env
    )

    assert ".env 不能使用 export" in result.stdout


def test_agent_mail_is_documented_and_diagnosed_as_optional():
    readme = (ROOT / "README.md").read_text()
    doctor = (ROOT / "doctor.sh").read_text()

    assert "Agent Mail" in readme
    assert "optional" in readme.lower() or "可选" in readme
    assert "XDG_DATA_HOME" in doctor
    assert 'warn "缺少 Agent Mail 数据库' in doctor
    assert 'fail "缺少 Agent Mail 数据库' not in doctor


def test_agent_mail_helpers_are_packaged_and_safely_linked():
    tools = ROOT / "agent-mail-tools"
    for name in ("am-register", "am-init-project", "mail-send", "mail-recv"):
        path = tools / name
        assert path.is_file(), f"missing Agent Mail helper: {name}"
        assert path.stat().st_mode & 0o111, f"Agent Mail helper is not executable: {name}"
    assert (tools / "am_common.py").is_file()
    for name in ("install.sh", "upgrade.sh"):
        script = (ROOT / name).read_text()
        assert '"$INSTALL_DIR/install-agent-mail-tools.sh" "$INSTALL_DIR"' in script
    linker = (ROOT / "install-agent-mail-tools.sh").read_text()
    assert '[[ -f "$target" && ! -L "$target" ]]' in linker


def test_agent_mail_tool_linker_preserves_user_paths_and_updates_legacy(tmp_path):
    install_dir = tmp_path / "install"
    tools = install_dir / "agent-mail-tools"
    tools.mkdir(parents=True)
    for name in ("am-register", "am-init-project", "mail-send", "mail-recv"):
        path = tools / name
        path.write_text("#!/usr/bin/env bash\n")
        path.chmod(0o755)
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    legacy_dir = home / "agent-mail-tools"
    custom_dir = tmp_path / "custom"
    bin_dir.mkdir(parents=True)
    legacy_dir.mkdir(parents=True)
    custom_dir.mkdir()
    ordinary = bin_dir / "mail-send"
    ordinary.write_text("keep\n")
    custom = custom_dir / "mail-recv"
    custom.write_text("custom\n")
    (bin_dir / "mail-recv").symlink_to(custom)
    legacy = legacy_dir / "am-init-project"
    legacy.write_text("legacy\n")
    (bin_dir / "am-init-project").symlink_to(legacy)

    result = subprocess.run(
        [str(ROOT / "install-agent-mail-tools.sh"), str(install_dir)],
        env={**os.environ, "HOME": str(home)}, text=True, capture_output=True,
    )

    assert result.returncode == 0
    assert ordinary.read_text() == "keep\n"
    assert (bin_dir / "mail-recv").resolve() == custom
    assert (bin_dir / "am-init-project").resolve() == tools / "am-init-project"
    assert (bin_dir / "am-register").resolve() == tools / "am-register"


def test_doctor_detects_pending_herdr_onboarding():
    doctor = (ROOT / "doctor.sh").read_text()

    assert "HERDR_CONFIG_PATH" in doctor
    assert "herdr 首次配置未完成；请先运行 herdr 完成向导" in doctor


def test_web_push_runtime_dependency_and_worker_are_packaged():
    requirements = (ROOT / "requirements.txt").read_text()

    assert "pywebpush==" in requirements
    assert (ROOT / "static" / "sw.js").is_file()
    assert (ROOT / "static" / "manifest.webmanifest").is_file()
