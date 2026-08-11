import xml.etree.ElementTree as ET
from pathlib import Path
import http.server
import os
import shutil
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "install.sh", "upgrade.sh", "uninstall.sh", "doctor.sh", "launchd.sh",
    "agent-mail-launchd.sh", "install-agent-mail-tools.sh",
    "install-agent-mail-hub.sh", "agent-mail-run.sh",
)
SCRIPT_HELPERS = ("install-paths.sh",)
DOCS = ("SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md")


def test_release_files_exist_and_scripts_are_valid():
    for name in SCRIPTS + SCRIPT_HELPERS + DOCS + (
        "agent-cockpit.service", "agent-cockpit.plist", "agent-mail.plist",
        "agent-mail.service",
    ):
        assert (ROOT / name).is_file(), f"missing release file: {name}"
    for name in SCRIPTS:
        path = ROOT / name
        assert path.stat().st_mode & 0o111, f"script is not executable: {name}"
        subprocess.run(["bash", "-n", str(path)], check=True)
    for name in SCRIPT_HELPERS:
        subprocess.run(["bash", "-n", str(ROOT / name)], check=True)
    ET.parse(ROOT / "agent-cockpit.plist")
    ET.parse(ROOT / "agent-mail.plist")


def test_readme_and_ci_have_no_release_placeholders():
    readme = (ROOT / "README.md").read_text()
    workflow = (ROOT / ".github/workflows/test.yml").read_text()

    assert "github.com/YOUR/" not in readme
    assert "github.com/)" not in readme
    assert "agent-mail-dashboard.service" not in readme
    assert "permissions:" in workflow
    assert "timeout-minutes:" in workflow


def test_installers_migrate_legacy_service_name():
    # install.sh 承担全新安装与服务迁移
    install = (ROOT / "install.sh").read_text()
    assert "disable --now agent-mail-dashboard.service" in install
    assert "enable --now agent-cockpit.service" in install
    # upgrade.sh 已退役（Wiki13 J0 fail-closed）：不再承担任何服务迁移职责
    upgrade = (ROOT / "upgrade.sh").read_text()
    assert "upgrade_engine_retired" in upgrade
    assert "agent-mail-dashboard.service" not in upgrade


def test_installers_accept_git_worktrees():
    installer = (ROOT / "install.sh").read_text()
    upgrader = (ROOT / "upgrade.sh").read_text()

    assert 'git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree' in installer
    assert 'git -C "$INSTALL_DIR" rev-parse --is-inside-work-tree' in installer
    assert '! -d "$INSTALL_DIR/.git"' not in installer
    # upgrade.sh 已退役：不再检查 git 安装目录
    assert "upgrade_engine_retired" in upgrader
    assert 'git -C "$INSTALL_DIR" rev-parse' not in upgrader


def test_installers_manage_macos_launch_agent():
    installer = (ROOT / "install.sh").read_text()
    upgrader = (ROOT / "upgrade.sh").read_text()
    uninstaller = (ROOT / "uninstall.sh").read_text()
    launchd = (ROOT / "launchd.sh").read_text()

    assert '"$INSTALL_DIR/launchd.sh" install' in installer
    assert '"$INSTALL_DIR/launchd.sh" uninstall' in uninstaller
    assert 'launchctl bootstrap "$DOMAIN" "$PLIST_PATH"' in launchd
    assert 'launchctl kickstart -k "$SERVICE"' in launchd
    assert '"$cwd" != "$INSTALL_DIR"' in launchd
    assert '"$command" != *server.py*' in launchd
    assert "Agent Cockpit LaunchAgent 正在运行" in (ROOT / "doctor.sh").read_text()
    # upgrade.sh 已退役：不再管理 launchd 重启
    assert "upgrade_engine_retired" in upgrader
    assert '"$INSTALL_DIR/launchd.sh" restart' not in upgrader


def test_agent_mail_launchd_keeps_token_out_of_plist():
    launcher = (ROOT / "agent-mail-launchd.sh").read_text()
    plist = (ROOT / "agent-mail.plist").read_text()

    assert 'HTTP_BEARER_TOKEN="$(load_token)"' in launcher
    assert 'sqlite+aiosqlite:///$REPO_DIR/storage.sqlite3' in launcher
    assert '${XDG_STATE_HOME:-$HOME/.local/state}/mcp-agent-mail/mailbox' in launcher
    assert '$REPO_DIR/mailbox' not in launcher
    assert "HTTP_BEARER_TOKEN" not in plist
    assert "token=" not in plist
    assert "__INSTALL_DIR__/agent-mail-launchd.sh" in plist
    assert "__REPO_DIR__" in plist
    assert "__CLIENT_ENV__" in plist


def test_agent_mail_launchd_restart_waits_for_old_listener(tmp_path):
    install_dir = tmp_path / "agent-cockpit"
    install_dir.mkdir()
    launcher = install_dir / "agent-mail-launchd.sh"
    launcher.write_text((ROOT / "agent-mail-launchd.sh").read_text())
    launcher.chmod(0o755)
    (install_dir / "install-paths.sh").write_text(
        (ROOT / "install-paths.sh").read_text()
    )
    (install_dir / "agent-mail.plist").write_text(
        (ROOT / "agent-mail.plist").read_text()
    )
    repo = tmp_path / "mcp_agent_mail"
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env bash\nexit 0\n")
    python.chmod(0o755)
    client_env = tmp_path / "client.env"
    client_env.write_text("hub=http://127.0.0.1:8765\ntoken=test-token\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "launchctl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "launchctl").chmod(0o755)
    lsof_calls = tmp_path / "lsof-calls"
    (fake_bin / "lsof").write_text(
        "#!/usr/bin/env bash\n"
        f'calls=$(cat "{lsof_calls}" 2>/dev/null || echo 0)\n'
        "calls=$((calls + 1))\n"
        f'printf "%s\\n" "$calls" > "{lsof_calls}"\n'
        'if [ "$calls" -le 2 ]; then echo 123; exit 0; fi\n'
        "exit 1\n"
    )
    (fake_bin / "lsof").chmod(0o755)
    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MCP_AGENT_MAIL_DIR": str(repo),
        "AGENT_MAIL_CLIENT_ENV": str(client_env),
    }

    result = subprocess.run(
        [str(launcher), "restart"], text=True, capture_output=True, env=env,
    )

    assert result.returncode == 0, result.stderr
    assert int(lsof_calls.read_text()) >= 3
    rendered = home / "Library/LaunchAgents/io.github.fyc0451.mcp-agent-mail-local.plist"
    values = [node.text for node in ET.parse(rendered).findall(".//string")]
    assert str(repo) in values
    assert str(client_env) in values


def test_launchd_installer_renders_and_restarts_service(tmp_path):
    install_dir = tmp_path / "agent-cockpit"
    install_dir.mkdir()
    launcher = install_dir / "launchd.sh"
    launcher.write_text((ROOT / "launchd.sh").read_text())
    launcher.chmod(0o755)
    (install_dir / "install-paths.sh").write_text(
        (ROOT / "install-paths.sh").read_text()
    )
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
    (install_dir / "install-paths.sh").write_text(
        (ROOT / "install-paths.sh").read_text()
    )
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
    assert not calls.exists(), "端口预检失败时不得先卸载现有 LaunchAgent"


def test_installer_rejects_relative_custom_path(tmp_path):
    """字符白名单已取消；空格、中文等正常路径允许，仅拒绝相对路径/控制字符。"""
    installer = tmp_path / "install.sh"
    installer.write_text((ROOT / "install.sh").read_text())
    installer.chmod(0o755)
    env = {**os.environ, "AGENT_COCKPIT_DIR": "relative/dir"}

    result = subprocess.run(
        [str(installer)], text=True, capture_output=True, env=env
    )

    assert result.returncode != 0
    assert "绝对路径" in result.stderr


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


def test_agent_mail_is_documented_and_diagnosed_as_required():
    readme = (ROOT / "README.md").read_text()
    doctor = (ROOT / "doctor.sh").read_text()

    assert "Agent Mail" in readme
    assert "Agent Mail 为必需基础设施" in readme
    assert "XDG_DATA_HOME" in doctor
    assert 'fail "缺少 Agent Mail 数据库' in doctor
    assert 'fail "缺少 ~/.agent-mail/client.env' in doctor


def test_agent_mail_helpers_are_packaged_and_safely_linked():
    tools = ROOT / "agent-mail-tools"
    for name in (
        "am-register", "am-retire", "am-init-project", "mail-send", "mail-recv",
        "mail-identity-inject", "mail-hook-check",
    ):
        path = tools / name
        assert path.is_file(), f"missing Agent Mail helper: {name}"
        assert path.stat().st_mode & 0o111, f"Agent Mail helper is not executable: {name}"
    assert (tools / "am_common.py").is_file()
    assert '"$INSTALL_DIR/install-agent-mail-tools.sh" "$INSTALL_DIR"' in (ROOT / "install.sh").read_text()
    # upgrade.sh 已退役（Wiki13 J0 fail-closed）：不再安装 Agent Mail 工具
    upgrade = (ROOT / "upgrade.sh").read_text()
    assert "upgrade_engine_retired" in upgrade
    assert "install-agent-mail-tools.sh" not in upgrade
    linker = (ROOT / "install-agent-mail-tools.sh").read_text()
    assert '[[ -f "$target" && ! -L "$target" ]]' in linker
    assert "readlink -f" not in linker


def test_agent_mail_tool_linker_preserves_user_paths_and_updates_legacy(tmp_path):
    install_dir = tmp_path / "install"
    tools = install_dir / "agent-mail-tools"
    tools.mkdir(parents=True)
    for name in (
        "am-register", "am-retire", "am-init-project", "mail-send", "mail-recv",
        "mail-identity-inject", "mail-hook-check",
    ):
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
    legacy_hook = legacy_dir / "mail-identity-inject"
    legacy_hook.write_text(
        "#!/usr/bin/env bash\n# mail-identity-inject — SessionStart hook: legacy\n"
    )
    legacy_hook.chmod(0o755)
    old_install = home / "agent-cockpit" / "agent-mail-tools"
    old_install.mkdir(parents=True)
    old_hook = old_install / "mail-identity-inject"
    old_hook.write_text("legacy hook\n")
    (bin_dir / "mail-identity-inject").symlink_to(old_hook)

    result = subprocess.run(
        [str(ROOT / "install-agent-mail-tools.sh"), str(install_dir)],
        env={**os.environ, "HOME": str(home)}, text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert ordinary.read_text() == "keep\n"
    assert (bin_dir / "mail-recv").resolve() == custom
    assert (bin_dir / "am-init-project").resolve() == tools / "am-init-project"
    assert (bin_dir / "am-register").resolve() == tools / "am-register"
    assert (bin_dir / "mail-identity-inject").resolve() == tools / "mail-identity-inject"
    assert legacy_hook.resolve() == tools / "mail-identity-inject"
    assert (legacy_dir / "mail-identity-inject.pre-cockpit").is_file()


def test_doctor_detects_pending_herdr_onboarding():
    doctor = (ROOT / "doctor.sh").read_text()

    assert "HERDR_CONFIG_PATH" in doctor
    assert "herdr 首次配置未完成；请先运行 herdr 完成向导" in doctor


def test_web_push_runtime_dependency_and_worker_are_packaged():
    requirements = (ROOT / "requirements.txt").read_text()

    assert "pywebpush==" in requirements
    assert (ROOT / "static" / "sw.js").is_file()
    assert (ROOT / "static" / "manifest.webmanifest").is_file()


def test_installers_allow_spaces_and_unicode_in_install_path():
    """安装目录不得再被字符白名单限制；systemd/launchd 模板替换必须转义。"""
    install = (ROOT / "install.sh").read_text()
    upgrade = (ROOT / "upgrade.sh").read_text()
    launchd = (ROOT / "launchd.sh").read_text()
    helpers = (ROOT / "install-paths.sh").read_text()
    for name, text in (("install.sh", install), ("launchd.sh", launchd)):
        assert "[[:alnum:]" not in text, f"{name} 仍限制路径字符集"
        assert "install-paths.sh" in text, f"{name} 未复用路径编码"
    # upgrade.sh 已退役（Wiki13 J0 fail-closed）：不再承担安装/路径编码职责
    assert "[[:alnum:]" not in upgrade
    assert "upgrade_engine_retired" in upgrade
    assert "ac_validate_install_dir" not in upgrade
    for name in ("agent-mail-launchd.sh", "install-agent-mail-hub.sh", "agent-mail-run.sh"):
        text = (ROOT / name).read_text()
        assert "[[:alnum:]]" not in text, f"{name} 仍限制路径字符集"
        assert "install-paths.sh" in text, f"{name} 未复用路径编码"
    assert "[[:cntrl:]]" in install
    assert "ac_validate_install_dir" in launchd
    assert "ac_validate_install_dir" in (ROOT / "agent-mail-launchd.sh").read_text()
    assert "ac_escape_systemd_value" in helpers
    assert "ac_escape_systemd_exec_value" in helpers
    assert "ac_escape_plist_value" in helpers
    assert "ac_client_env_loopback_hub" in helpers
    service = (ROOT / "agent-cockpit.service").read_text()
    assert "WorkingDirectory=__INSTALL_DIR__" in service
    assert 'ExecStart=/usr/bin/env "__INSTALL_EXEC_DIR__/.venv/bin/python" server.py' in service
    # 本地 Hub 服务不硬编码端口/凭据：端口与 token 从 client.env 严格解析。
    mail_service = (ROOT / "agent-mail.service").read_text()
    assert "8765" not in mail_service
    assert "HTTP_BEARER_TOKEN" not in mail_service
    assert "agent-mail-run.sh" in mail_service
    assert "__REPO_EXEC_DIR__" in mail_service
    assert "__CLIENT_ENV_EXEC__" in mail_service
    hub_installer = (ROOT / "install-agent-mail-hub.sh").read_text()
    assert "HTTP_PORT=8765" not in (ROOT / "agent-mail-launchd.sh").read_text()
    assert "ac_client_env_loopback_hub" in (ROOT / "agent-mail-run.sh").read_text()
    assert "ac_client_env_loopback_hub" in (ROOT / "agent-mail-launchd.sh").read_text()
    assert "ac_client_env_loopback_hub" in hub_installer
    # upgrade.sh 已退役：不再解析 venv python（安装职责归 install.sh）
    assert 'PYTHON_BIN="${PYTHON_BIN:-$INSTALL_DIR/.venv/bin/python}"' not in upgrade
    assert "upgrade_engine_retired" in upgrade


def test_sed_and_plist_escaping_with_special_path(tmp_path):
    """端到端：特殊路径渲染 unit/plist 后仍表示同一条原始路径。"""
    tricky_path = tmp_path / '我的 项目&x<y>"q\\z%u$FOO'
    tricky = str(tricky_path)
    result = subprocess.run(
        [
            "bash", "-c", r'''
source "$1"
systemd_dir="$(ac_escape_systemd_value "$4")"
systemd_exec_dir="$(ac_escape_systemd_exec_value "$4")"
sed \
  -e "s|__INSTALL_EXEC_DIR__|$(ac_escape_sed_replacement "$systemd_exec_dir")|g" \
  -e "s|__INSTALL_DIR__|$(ac_escape_sed_replacement "$systemd_dir")|g" "$2"
printf '\n__PLIST__\n'
plist_dir="$(ac_escape_plist_value "$4")"
sed "s|__INSTALL_DIR__|$(ac_escape_sed_replacement "$plist_dir")|g" "$3"
''',
            "_",
            str(ROOT / "install-paths.sh"),
            str(ROOT / "agent-cockpit.service"),
            str(ROOT / "agent-cockpit.plist"),
            tricky,
        ],
        capture_output=True, text=True, check=True,
    )
    unit, plist_xml = result.stdout.split("\n__PLIST__\n", 1)
    systemd_dir = tricky.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    systemd_exec_dir = systemd_dir.replace("$", "$$")
    assert f"WorkingDirectory={systemd_dir}" in unit
    assert f'ExecStart=/usr/bin/env "{systemd_exec_dir}/.venv/bin/python" server.py' in unit

    plist = ET.fromstring(plist_xml)
    values = [node.text for node in plist.findall(".//string")]
    assert f"{tricky}/launchd.sh" in values
    assert tricky in values
    assert f"{tricky}/logs/launchd.stdout.log" in values
    assert f"{tricky}/logs/launchd.stderr.log" in values

    if shutil.which("systemd-analyze"):
        python_bin = tricky_path / ".venv" / "bin" / "python"
        python_bin.parent.mkdir(parents=True)
        python_bin.symlink_to("/bin/true")
        rendered = tmp_path / "rendered-agent-cockpit.service"
        rendered.write_text(unit)
        verified = subprocess.run(
            ["systemd-analyze", "verify", "--man=no", str(rendered)],
            capture_output=True, text=True,
        )
        assert verified.returncode == 0, verified.stderr


def test_install_path_rejects_control_characters():
    result = subprocess.run(
        [
            "bash", "-c", 'source "$1"; ac_validate_install_dir "$2"', "_",
            str(ROOT / "install-paths.sh"), "/tmp/with\ttab",
        ],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "控制字符" in result.stderr


def _run_bash(snippet, *args, env=None):
    return subprocess.run(
        ["bash", "-c", snippet, "_", *[str(a) for a in args]],
        capture_output=True, text=True, env=env,
    )


def test_agent_mail_loopback_port_strict_parsing(tmp_path):
    """阻断3：端口必须从 client.env 的 loopback hub URL 严格解析。"""
    cases_ok = {
        "hub=http://127.0.0.1:8765\ntoken=t\n": "127.0.0.1 8765",
        "hub=http://127.0.0.1:18765\ntoken=t\n": "127.0.0.1 18765",
        "hub=http://localhost:9000\ntoken=t\n": "localhost 9000",
        "hub=http://127.0.0.1\ntoken=t\n": "127.0.0.1 8765",
        "hub=http://localhost:65535\ntoken=t\n": "localhost 65535",
    }
    cases_bad = [
        "hub=http://10.0.0.5:8765\ntoken=t\n",   # 非 loopback
        "hub=https://127.0.0.1:8765\ntoken=t\n",  # 非 http
        "hub=http://evil.example:8765\ntoken=t\n",
        "hub=http://127.0.0.1:0\ntoken=t\n",
        "hub=http://127.0.0.1:65536\ntoken=t\n",
        "hub=http://127.0.0.1:99999\ntoken=t\n",
        "token=t\n",                               # 缺 hub=
        "",
    ]
    for content, expected in cases_ok.items():
        assert _loopback_probe(content, tmp_path) == (0, expected), content
    for content in cases_bad:
        rc, _ = _loopback_probe(content, tmp_path)
        assert rc != 0, content


def _loopback_probe(content, tmp_path):
    env_file = tmp_path / "client.env"
    env_file.write_text(content)
    result = _run_bash(
        'source "{0}"; ac_client_env_loopback_hub "{1}"'.format(
            ROOT / "install-paths.sh", env_file),
    )
    return result.returncode, result.stdout.strip()


def test_agent_mail_service_rendering_with_special_path(tmp_path):
    """阻断2：agent-mail.service 渲染必须按 systemd/sed 规则转义特殊路径。"""
    tricky_install = tmp_path / '我的 安装&dir"q\\z%u$FOO'
    tricky_install.mkdir()
    # 渲染函数从 install_dir 读取模板，特殊路径目录内也要能找到模板。
    shutil.copy(ROOT / "agent-mail.service", tricky_install / "agent-mail.service")
    tricky_repo = tmp_path / '仓 库&repo'
    tricky_client = tmp_path / '配置 目录&x' / "client.env"
    unit_path = tmp_path / "agent-mail.service"
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; amh_render_systemd_unit "$2" "$3" "$4" "$5"',
            "_",
            str(ROOT / "install-agent-mail-hub.sh"),
            str(unit_path), str(tricky_install), str(tricky_repo), str(tricky_client),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    unit = unit_path.read_text()
    exec_dir = (str(tricky_install).replace("\\", "\\\\").replace('"', '\\"')
                .replace("%", "%%").replace("$", "$$"))
    repo_exec = (str(tricky_repo).replace("\\", "\\\\").replace('"', '\\"')
                 .replace("%", "%%").replace("$", "$$"))
    client_exec = (str(tricky_client).replace("\\", "\\\\").replace('"', '\\"')
                   .replace("%", "%%").replace("$", "$$"))
    assert (
        f'ExecStart=/usr/bin/env "{exec_dir}/agent-mail-run.sh" '
        f'"{repo_exec}" "{client_exec}"'
    ) in unit
    assert "Environment=MCP_AGENT_MAIL_DIR" not in unit
    if shutil.which("systemd-analyze"):
        verified = subprocess.run(
            ["systemd-analyze", "verify", "--man=no", str(unit_path)],
            capture_output=True, text=True,
        )
        assert verified.returncode == 0, verified.stderr
        assert "Invalid environment assignment" not in verified.stderr


def test_agent_mail_launchd_persists_custom_runtime_paths(tmp_path):
    install_dir = tmp_path / "Agent Cockpit 中文"
    install_dir.mkdir()
    for name in ("agent-mail-launchd.sh", "install-paths.sh", "agent-mail.plist"):
        shutil.copy(ROOT / name, install_dir / name)
    (install_dir / "agent-mail-launchd.sh").chmod(0o755)
    repo = tmp_path / "Hub 仓库"
    client_env = tmp_path / "配置 目录" / "client.env"
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    result = subprocess.run(
        [str(install_dir / "agent-mail-launchd.sh"), "render-plist"],
        capture_output=True, text=True,
        env={
            **os.environ, "HOME": str(home),
            "MCP_AGENT_MAIL_DIR": str(repo),
            "AGENT_MAIL_CLIENT_ENV": str(client_env),
        },
    )

    assert result.returncode == 0, result.stderr
    rendered = home / "Library/LaunchAgents/io.github.fyc0451.mcp-agent-mail-local.plist"
    values = [node.text for node in ET.parse(rendered).findall(".//string")]
    assert str(repo) in values
    assert str(client_env) in values


def test_agent_mail_hub_installer_rejects_invalid_generated_port(tmp_path):
    client_env = tmp_path / "client.env"
    result = subprocess.run(
        ["bash", str(ROOT / "install-agent-mail-hub.sh")],
        capture_output=True, text=True,
        env={
            **os.environ,
            "AGENT_MAIL_CLIENT_ENV": str(client_env),
            "AGENT_MAIL_HUB_PORT": "99999",
            "PYTHON_BIN": sys.executable,
        },
        timeout=30,
    )

    assert result.returncode != 0
    assert "1-65535" in result.stderr
    assert not client_env.exists()


def test_agent_mail_hub_installer_rejects_relative_client_env(tmp_path):
    result = subprocess.run(
        ["bash", str(ROOT / "install-agent-mail-hub.sh")],
        capture_output=True, text=True, cwd=tmp_path,
        env={
            **os.environ,
            "AGENT_MAIL_CLIENT_ENV": "relative-client.env",
            "PYTHON_BIN": sys.executable,
        },
        timeout=30,
    )

    assert result.returncode != 0
    assert "绝对路径" in result.stderr
    assert not (tmp_path / "relative-client.env").exists()


def test_agent_mail_hub_installer_refuses_foreign_config(tmp_path):
    """阻断4a：探活失败且 client.env 非本脚本生成（或指向远程）→ 拒绝且不改文件。"""
    foreign = tmp_path / "client.env"
    original = "hub=http://127.0.0.1:1\ntoken=keepme\n"
    foreign.write_text(original)
    env = {**os.environ, "AGENT_MAIL_CLIENT_ENV": str(foreign),
           "PYTHON_BIN": sys.executable}
    result = subprocess.run(
        ["bash", str(ROOT / "install-agent-mail-hub.sh")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode != 0
    assert "不覆盖" in result.stderr
    assert foreign.read_text() == original

    remote = tmp_path / "client-remote.env"
    remote.write_text(
        "# generated by agent-cockpit install-agent-mail-hub.sh\n"
        "hub=http://10.18.160.11:8765\ntoken=keepme\n")
    env["AGENT_MAIL_CLIENT_ENV"] = str(remote)
    result = subprocess.run(
        ["bash", str(ROOT / "install-agent-mail-hub.sh")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode != 0
    assert "不覆盖" in result.stderr


def test_agent_mail_hub_installer_self_heals_managed_config(tmp_path):
    """阻断4b：本脚本生成的配置探活失败 → 保留 token 自愈，不永久拒绝。"""
    marker = "# generated by agent-cockpit install-agent-mail-hub.sh"
    client_env = tmp_path / "client.env"
    client_env.write_text(f"{marker}\nhub=http://127.0.0.1:1\ntoken=keepme\n")

    # 预置假仓库目录（空格+中文路径），跳过 clone/venv/pip。
    repo = tmp_path / "我的 仓库"
    (repo / ".git").mkdir(parents=True)
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.symlink_to(sys.executable)
    pkg_root = tmp_path / "pypkgs"
    (pkg_root / "mcp_agent_mail").mkdir(parents=True)
    (pkg_root / "mcp_agent_mail" / "__init__.py").write_text("")

    env = {**os.environ,
           "AGENT_MAIL_CLIENT_ENV": str(client_env),
           "MCP_AGENT_MAIL_DIR": str(repo),
           "AGENT_MAIL_NO_SERVICE": "1",
           "PYTHON_BIN": sys.executable,
           "PYTHONPATH": str(pkg_root)}
    result = subprocess.run(
        ["bash", str(ROOT / "install-agent-mail-hub.sh")],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "自愈" in result.stdout
    assert client_env.read_text() == f"{marker}\nhub=http://127.0.0.1:1\ntoken=keepme\n"


def _probe_fake_hub(tmp_path, response_body):
    body = response_body.encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        client_env = tmp_path / "client.env"
        client_env.write_text(f"hub=http://127.0.0.1:{port}\ntoken=tok\n")
        env = {**os.environ, "AGENT_MAIL_CLIENT_ENV": str(client_env),
               "PYTHON_BIN": sys.executable}
        result = subprocess.run(
            ["bash", str(ROOT / "install-agent-mail-hub.sh")],
            capture_output=True, text=True, env=env, timeout=60,
        )
        return result, port
    finally:
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()


def test_agent_mail_hub_installer_reuses_running_hub_on_custom_port(tmp_path):
    """阻断3端到端：非默认端口的真实 MCP Hub 被探活识别并复用。"""
    body = (
        '{"jsonrpc":"2.0","id":1,"result":'
        '{"protocolVersion":"2025-03-26","capabilities":{}}}'
    )
    result, port = _probe_fake_hub(tmp_path, body)

    assert result.returncode == 0, result.stderr
    assert "复用已有" in result.stdout
    assert f"127.0.0.1:{port}" in result.stdout


def test_agent_mail_hub_probe_rejects_unrelated_http_200(tmp_path):
    result, _ = _probe_fake_hub(tmp_path, '{"ok":true}')

    assert result.returncode != 0
    assert "不覆盖" in result.stderr


# ── U1a：VERSION / Release workflow 门禁 ─────────────────────────

def test_version_file_is_exactly_0_2_0():
    text = (ROOT / "VERSION").read_text(encoding="utf-8")
    assert text.strip() == "0.2.0"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines == ["0.2.0"]


def test_release_workflow_gates_tag_and_checks():
    path = ROOT / ".github" / "workflows" / "release.yml"
    assert path.is_file()
    raw = path.read_text(encoding="utf-8")
    assert "Verify tag matches VERSION" in raw
    assert "GITHUB_REF_NAME" in raw
    assert "python-version" in raw and "3.12" in raw
    assert "pytest -q" in raw
    assert "ast.parse" in raw or "compile(source" in raw
    assert "node --check" in raw
    assert "contents: write" in raw
    assert "--generate-notes" in raw
    assert "tags:" in raw
    assert '"agent-cockpit-v*"' in raw
    assert 'expected="agent-cockpit-v$(tr -d \'[:space:]\' < VERSION)"' in raw
    assert '\n      - "v*"' not in raw
    # tag commit 必须是 origin/main 祖先
    assert "merge-base --is-ancestor" in raw
    assert "origin/main" in raw
    assert "GITHUB_SHA" in raw
    assert "fetch-depth: 0" in raw


def test_release_workflow_pins_actions_to_full_commit_sha():
    """contents:write job 内所有 uses 必须钉完整 40 位 SHA，并保留版本注释。"""
    import re

    raw = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    # 兼容 "- uses:" 与 "name: ...\n  uses:" 两种写法
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)\s*$", raw, flags=re.M)
    assert uses, "release.yml 应至少有一个 uses"
    sha_re = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
    for ref in uses:
        assert sha_re.fullmatch(ref), f"uses 必须钉完整 commit SHA: {ref}"
        # 禁止可变 tag / 短 SHA
        assert "@v" not in ref
        assert not re.search(r"@[0-9a-f]{1,39}$", ref)
    # 五个已知 action 均出现，且带版本注释（# actions/...@vN 或行前注释）
    required = (
        "actions/checkout@",
        "actions/setup-python@",
        "actions/setup-node@",
        "actions/upload-artifact@",
        "actions/download-artifact@",
    )
    for prefix in required:
        assert any(u.startswith(prefix) for u in uses), f"缺少 {prefix}"
    assert "# actions/checkout@v4" in raw
    assert "# actions/setup-python@v5" in raw
    assert "# actions/setup-node@v4" in raw
    assert "# actions/upload-artifact@v4" in raw
    assert "# actions/download-artifact@v4" in raw


def test_release_main_ancestor_gate_accepts_main_commit_rejects_side_branch(tmp_path):
    """契约：仅 origin/main 历史祖先可发版；侧支 tip 应被 merge-base 拒绝。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (repo / "f.txt").write_text("a\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-m", "base")
    main_sha = git("rev-parse", "HEAD").stdout.strip()

    git("checkout", "-b", "side")
    (repo / "f.txt").write_text("b\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-m", "side")
    side_sha = git("rev-parse", "HEAD").stdout.strip()
    git("checkout", "main")

    # 模拟 origin/main
    git("update-ref", "refs/remotes/origin/main", main_sha)

    ok = subprocess.run(
        ["git", "merge-base", "--is-ancestor", main_sha, "origin/main"],
        cwd=repo,
    )
    assert ok.returncode == 0

    bad = subprocess.run(
        ["git", "merge-base", "--is-ancestor", side_sha, "origin/main"],
        cwd=repo,
    )
    assert bad.returncode != 0


def test_api_version_not_in_public_paths():
    import server

    assert "/api/version" not in server.PUBLIC_PATHS
