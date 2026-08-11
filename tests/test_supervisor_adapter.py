"""U2 S0 / R1：固定 systemd/launchd supervisor 纯适配层测试。

覆盖：显式 launcher argv（无 .venv/launchd 默认）、空格/中文路径、控制字符、
恶意 XML、错误 label/KillMode/current、launcher 逃出 current、空 argv、
symlink 逃逸、命令计划不执行、禁止 upgrade.<job>、无恒真断言。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import supervisor_adapter as sa


# ── fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def deploy_layout(tmp_path: Path) -> dict[str, Path]:
    """自含 artifact 布局：不依赖 .venv 或源码 launchd.sh。"""
    root = tmp_path / "deploy"
    gen = root / "generations" / "abc123"
    gen.mkdir(parents=True)
    launcher = gen / "bin" / "agent-cockpit"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    current = root / "current"
    current.symlink_to(gen)
    controller = tmp_path / "controller-install"
    controller.mkdir()
    # 字面量 current 下的 launcher 路径（不 resolve 成 generation）
    main_argv = (f"{current.as_posix()}/bin/agent-cockpit", "serve")
    return {
        "root": root,
        "gen": gen,
        "current": current,
        "controller": controller,
        "main_argv": main_argv,
    }


def _main_argv(current: Path, *extra: str) -> tuple[str, ...]:
    return (f"{current.as_posix()}/bin/agent-cockpit", *extra)


# ── path validation ──────────────────────────────────────────────


class TestPathValidation:
    def test_spaces_and_chinese_current_ok(self, tmp_path: Path) -> None:
        root = tmp_path / "部署 根"
        root.mkdir()
        current = root / "current"
        current.mkdir()
        got = sa.validate_current_path(current, deploy_root=root)
        assert got == current
        assert " " in got.parent.name or "部署" in got.parent.name

    def test_relative_rejected(self) -> None:
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_absolute_path("rel/current", role="current")
        assert exc.value.reason == "relative_path"

    def test_control_char_rejected(self) -> None:
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_absolute_path("/tmp/cur\x00rent", role="current")
        assert exc.value.reason in {"control_char_in_path", "nul_in_path"}

    def test_newline_in_path_rejected(self) -> None:
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_absolute_path("/tmp/cur\nrent", role="current")
        assert exc.value.reason == "control_char_in_path"

    def test_parent_segment_rejected(self) -> None:
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_absolute_path("/tmp/a/../b", role="path")
        assert exc.value.reason == "path_parent_segment"

    def test_current_basename_required(self, tmp_path: Path) -> None:
        p = tmp_path / "not-current"
        p.mkdir()
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_current_path(p)
        assert exc.value.reason == "current_name_required"

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "deploy"
        root.mkdir()
        current = root / "current"
        current.symlink_to(outside)
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_current_path(current, deploy_root=root)
        assert exc.value.reason == "symlink_escape"

    def test_controller_inside_current_rejected(self, deploy_layout: dict[str, Path]) -> None:
        bad = deploy_layout["current"] / "nested-controller"
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_controller_path(bad, current=deploy_layout["current"])
        assert exc.value.reason == "controller_inside_current"

    def test_controller_outside_ok(self, deploy_layout: dict[str, Path]) -> None:
        got = sa.validate_controller_path(
            deploy_layout["controller"],
            current=deploy_layout["current"],
        )
        assert got == deploy_layout["controller"]


# ── escaping ─────────────────────────────────────────────────────


class TestEscaping:
    def test_systemd_value_quotes_percent_backslash(self) -> None:
        raw = r'/data/a"b%c\d'
        esc = sa.escape_systemd_value(raw)
        assert r"\"" in esc
        assert "%%" in esc
        assert r"\\" in esc

    def test_systemd_exec_dollars(self) -> None:
        assert sa.escape_systemd_exec_value("/opt/$foo") == r"/opt/$$foo"

    def test_plist_xml_entities(self) -> None:
        raw = 'a&b<c>d"e'
        esc = sa.escape_plist_xml(raw)
        assert "&amp;" in esc
        assert "&lt;" in esc
        assert "&gt;" in esc
        assert "&quot;" in esc
        assert "<" not in esc.replace("&lt;", "")
        assert "&" not in esc.replace("&amp;", "").replace("&lt;", "").replace(
            "&gt;", ""
        ).replace("&quot;", "")


# ── launcher argv ────────────────────────────────────────────────


class TestLauncherArgv:
    def test_empty_argv_rejected(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.normalize_launcher_argv([], current_dir=cur)
        assert exc.value.reason == "empty_program_arguments"
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.render_linux_unit(
                current_dir=cur,
                program_arguments=[],
                deploy_root=deploy_layout["root"],
            )
        assert exc2.value.reason == "empty_program_arguments"
        with pytest.raises(sa.SupervisorAdapterError) as exc3:
            sa.render_mac_main_plist(
                current_dir=cur,
                program_arguments=[],
                deploy_root=deploy_layout["root"],
            )
        assert exc3.value.reason == "empty_program_arguments"

    def test_launcher_outside_current_rejected(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        cur = deploy_layout["current"]
        outside = "/tmp/evil-bin"
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.normalize_launcher_argv([outside], current_dir=cur)
        assert exc.value.reason == "launcher_outside_current"
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.render_linux_unit(
                current_dir=cur,
                program_arguments=[outside, "serve"],
                deploy_root=deploy_layout["root"],
            )
        assert exc2.value.reason == "launcher_outside_current"

    def test_no_default_venv_or_source_launchd(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        """调用方不传 argv 时 API 失败；渲染结果不出现隐式 .venv/launchd.sh。"""
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        unit = sa.render_linux_unit(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        mac = sa.render_mac_main_plist(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        assert ".venv" not in unit
        assert "launchd.sh" not in unit
        assert "server.py" not in unit
        assert ".venv" not in mac
        assert "launchd.sh" not in mac
        assert f"{cur.as_posix()}/bin/agent-cockpit" in unit
        assert f"{cur.as_posix()}/bin/agent-cockpit" in mac

    def test_control_char_in_argv_rejected(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        bad = f"{cur.as_posix()}/bin/agent\ncockpit"
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.normalize_launcher_argv([bad], current_dir=cur)
        assert exc.value.reason == "control_char_in_argument"


# ── linux unit render + validate ─────────────────────────────────


class TestLinuxUnit:
    def test_render_killmode_process_and_same_current(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        text = sa.render_linux_unit(
            current_dir=cur,
            program_arguments=argv,
            deploy_root=deploy_layout["root"],
        )
        assert "KillMode=process" in text
        assert f"WorkingDirectory={cur.as_posix()}" in text
        assert argv[0] in text
        contract = sa.validate_linux_unit_contract(
            text,
            current_dir=cur,
            program_arguments=argv,
            deploy_root=deploy_layout["root"],
        )
        assert contract.kill_mode == "process"
        assert contract.working_directory == cur.as_posix()

    def test_spaces_chinese_path_in_unit(self, tmp_path: Path) -> None:
        root = tmp_path / "我的 部署"
        gen = root / "generations" / "g1"
        gen.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(gen)
        argv = _main_argv(current, "serve")
        text = sa.render_linux_unit(
            current_dir=current, program_arguments=argv, deploy_root=root
        )
        assert "我的 部署" in text
        sa.validate_linux_unit_contract(
            text, current_dir=current, program_arguments=argv, deploy_root=root
        )

    def test_wrong_killmode_rejected(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        text = sa.render_linux_unit(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        bad = text.replace("KillMode=process", "KillMode=control-group")
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_linux_unit_contract(
                bad,
                current_dir=cur,
                program_arguments=argv,
                deploy_root=deploy_layout["root"],
            )
        assert exc.value.reason == "killmode_not_process"

    def test_wrong_current_in_wd_rejected(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        text = sa.render_linux_unit(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        bad = text.replace(
            f"WorkingDirectory={cur.as_posix()}",
            "WorkingDirectory=/tmp/evil/current",
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_linux_unit_contract(
                bad,
                current_dir=cur,
                program_arguments=argv,
                deploy_root=deploy_layout["root"],
            )
        assert exc.value.reason == "working_directory_mismatch"

    def test_argv_mismatch_rejected(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        text = sa.render_linux_unit(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        other = _main_argv(cur, "other-mode")
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_linux_unit_contract(
                text,
                current_dir=cur,
                program_arguments=other,
                deploy_root=deploy_layout["root"],
            )
        assert exc.value.reason == "exec_start_argv_mismatch"

    def test_unknown_unit_key_rejected(self) -> None:
        text = (
            "[Service]\n"
            "KillMode=process\n"
            "WorkingDirectory=/tmp/x/current\n"
            'ExecStart="/usr/bin/true"\n'
            "ExecStop=/bin/evil\n"
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.parse_linux_unit_contract(text)
        assert exc.value.reason == "unit_key_not_allowlisted"

    def test_special_chars_escaped_in_exec(self, tmp_path: Path) -> None:
        root = tmp_path / 'dep$oy%"x'
        gen = root / "generations" / "g"
        gen.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(gen)
        argv = _main_argv(current, "serve")
        text = sa.render_linux_unit(
            current_dir=current, program_arguments=argv, deploy_root=root
        )
        # path 含 $ % " → exec 转义
        assert "$$" in text
        assert "%%" in text
        sa.validate_linux_unit_contract(
            text, current_dir=current, program_arguments=argv, deploy_root=root
        )


# ── mac plist ────────────────────────────────────────────────────


class TestMacPlist:
    def test_main_fixed_label(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        text = sa.render_mac_main_plist(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        assert sa.MAC_MAIN_LABEL in text
        assert ".upgrade." not in text
        assert "launchd.sh" not in text
        contract = sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_MAIN_LABEL,
            expected_working_directory=cur,
            program_arguments=argv,
            current_dir=cur,
            deploy_root=deploy_layout["root"],
        )
        assert contract.label == sa.MAC_MAIN_LABEL
        assert contract.program_arguments == argv
        assert contract.keep_alive is True

    def test_controller_fixed_label_outside_current(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        ctrl = deploy_layout["controller"]
        prog = ctrl / "controller.bin"
        prog.write_text("x", encoding="utf-8")
        argv = (prog.as_posix(), "run")
        text = sa.render_mac_controller_plist(
            controller_dir=ctrl,
            program_arguments=argv,
            current_dir=deploy_layout["current"],
        )
        assert f"<string>{sa.MAC_CONTROLLER_LABEL}</string>" in text
        assert f"<string>{sa.MAC_MAIN_LABEL}</string>" not in text
        contract = sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_CONTROLLER_LABEL,
            expected_working_directory=ctrl,
            program_arguments=argv,
        )
        assert contract.label == sa.MAC_CONTROLLER_LABEL
        assert contract.program_arguments == argv

    def test_forbidden_upgrade_job_label(self) -> None:
        dynamic = "io.github.fyc0451.agent-cockpit.upgrade.abc"
        assert sa.is_forbidden_mac_label(dynamic)
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.assert_fixed_mac_label(dynamic)
        assert exc.value.reason == "forbidden_dynamic_upgrade_label"

    def test_unknown_label_rejected(self) -> None:
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.assert_fixed_mac_label("com.example.evil")
        assert exc.value.reason == "label_not_allowlisted"

    def test_malicious_xml_entity_rejected(self) -> None:
        evil = """<?xml version="1.0"?>
<!DOCTYPE plist [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<plist version="1.0"><dict>
  <key>Label</key><string>&xxe;</string>
</dict></plist>
"""
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.parse_mac_plist_contract(evil)
        assert exc.value.reason in {"plist_entity_forbidden", "plist_xml_invalid"}

    def test_malicious_raw_angle_brackets_escaped_on_render(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "a<b>&c"
        gen = root / "generations" / "g"
        gen.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(gen)
        argv = _main_argv(current)
        text = sa.render_mac_main_plist(
            current_dir=current, program_arguments=argv, deploy_root=root
        )
        assert "<b>" not in text
        assert "&lt;" in text
        assert "&amp;" in text
        sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_MAIN_LABEL,
            expected_working_directory=current,
            program_arguments=argv,
            current_dir=current,
            deploy_root=root,
        )

    def test_label_mismatch_rejected(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        text = sa.render_mac_main_plist(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_mac_plist_contract(
                text,
                expected_label=sa.MAC_CONTROLLER_LABEL,
                expected_working_directory=cur,
            )
        assert exc.value.reason == "label_mismatch"

    def test_spaces_chinese_mac_path(self, tmp_path: Path) -> None:
        root = tmp_path / "Mac 部署 dir"
        gen = root / "generations" / "g"
        gen.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(gen)
        argv = _main_argv(current, "serve")
        text = sa.render_mac_main_plist(
            current_dir=current, program_arguments=argv, deploy_root=root
        )
        assert "Mac 部署 dir" in text
        sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_MAIN_LABEL,
            expected_working_directory=current,
            program_arguments=argv,
            current_dir=current,
            deploy_root=root,
        )

    def test_main_requires_launcher_for_validate(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        text = sa.render_mac_main_plist(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_mac_plist_contract(
                text,
                expected_label=sa.MAC_MAIN_LABEL,
                expected_working_directory=cur,
            )
        assert exc.value.reason == "main_launcher_required"


# ── command plans (no execution) ─────────────────────────────────


class TestCommandPlans:
    def test_linux_plan_allowlisted_only(self, tmp_path: Path) -> None:
        unit = tmp_path / "agent-cockpit.service"
        plans = sa.plan_linux_install_unit(unit_path=unit)
        tools = sa.planned_argv_tools(plans)
        assert tools == frozenset({"systemctl"})
        assert all(p.argv[1] == "--user" for p in plans)
        assert any(p.argv[-1] == sa.LINUX_UNIT_NAME for p in plans)

    def test_linux_wrong_unit_name_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.plan_linux_restart(unit_name="evil.service")
        assert exc.value.reason == "unit_name_not_allowlisted"

    def test_mac_bootstrap_fixed_label(self, tmp_path: Path) -> None:
        plist = tmp_path / f"{sa.MAC_MAIN_LABEL}.plist"
        plans = sa.plan_mac_bootstrap(label=sa.MAC_MAIN_LABEL, plist_path=plist, uid=501)
        tools = sa.planned_argv_tools(plans)
        assert tools == frozenset({"launchctl"})
        assert any("bootstrap" in p.argv for p in plans)
        joined = " ".join(" ".join(p.argv) for p in plans)
        assert sa.MAC_MAIN_LABEL in joined
        assert all(
            p.argv[0] == "launchctl"
            and p.argv[1] in {"bootout", "bootstrap", "enable", "kickstart"}
            for p in plans
        )

    def test_mac_plan_rejects_upgrade_label(self, tmp_path: Path) -> None:
        plist = tmp_path / "x.plist"
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.plan_mac_bootstrap(
                label="io.github.fyc0451.agent-cockpit.upgrade.job1",
                plist_path=plist,
                uid=501,
            )
        assert exc.value.reason == "forbidden_dynamic_upgrade_label"

    def test_module_never_imports_subprocess_for_supervisor(self) -> None:
        import importlib

        mod = importlib.import_module("supervisor_adapter")
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "import subprocess" not in src
        assert "from subprocess" not in src
        assert "os.system" not in src
        assert "Popen" not in src
        assert not hasattr(mod, "subprocess")
        # systemctl/launchctl 仅作为 PlannedCommand 数据字符串出现
        assert "def execute" not in src
        assert "subprocess.run" not in src

    def test_plans_are_data_not_executed(self, tmp_path: Path, monkeypatch) -> None:
        """即使 PATH 上有假 systemctl，plan API 也不得调用它。"""
        calls: list[list[str]] = []

        def boom(*_a, **_k):
            calls.append(["called"])
            raise AssertionError("plan must not execute")

        monkeypatch.setattr(subprocess, "run", boom)
        monkeypatch.setattr(subprocess, "Popen", boom)
        sa.plan_linux_restart()
        sa.plan_mac_bootstrap(
            label=sa.MAC_CONTROLLER_LABEL,
            plist_path=tmp_path / "c.plist",
            uid=1000,
        )
        assert calls == []


# ── end-to-end pure pipeline ─────────────────────────────────────


def test_render_validate_plan_pipeline(deploy_layout: dict[str, Path]) -> None:
    cur = deploy_layout["current"]
    root = deploy_layout["root"]
    argv = deploy_layout["main_argv"]
    unit = sa.render_linux_unit(
        current_dir=cur, program_arguments=argv, deploy_root=root
    )
    sa.validate_linux_unit_contract(
        unit, current_dir=cur, program_arguments=argv, deploy_root=root
    )
    main = sa.render_mac_main_plist(
        current_dir=cur, program_arguments=argv, deploy_root=root
    )
    sa.validate_mac_plist_contract(
        main,
        expected_label=sa.MAC_MAIN_LABEL,
        expected_working_directory=cur,
        program_arguments=argv,
        current_dir=cur,
        deploy_root=root,
    )
    ctrl_bin = deploy_layout["controller"] / "run.sh"
    ctrl_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    ctrl = sa.render_mac_controller_plist(
        controller_dir=deploy_layout["controller"],
        program_arguments=[ctrl_bin.as_posix()],
        current_dir=cur,
    )
    sa.validate_mac_plist_contract(
        ctrl,
        expected_label=sa.MAC_CONTROLLER_LABEL,
        expected_working_directory=deploy_layout["controller"],
        program_arguments=[ctrl_bin.as_posix()],
    )
    plans = (
        sa.plan_linux_install_unit(unit_path=root / "agent-cockpit.service")
        + sa.plan_mac_bootstrap(
            label=sa.MAC_MAIN_LABEL,
            plist_path=root / f"{sa.MAC_MAIN_LABEL}.plist",
            uid=1000,
        )
        + sa.plan_mac_bootstrap(
            label=sa.MAC_CONTROLLER_LABEL,
            plist_path=deploy_layout["controller"] / f"{sa.MAC_CONTROLLER_LABEL}.plist",
            uid=1000,
        )
    )
    tools = sa.planned_argv_tools(plans)
    assert tools <= frozenset({"systemctl", "launchctl"})
    assert not any(".upgrade." in " ".join(p.argv) for p in plans)


def test_no_tautology_or_true_in_adapter_module() -> None:
    """源码不得含恒真 `or True` 断言残留。"""
    src = Path(sa.__file__).read_text(encoding="utf-8")
    needle = "or" + " True"
    assert needle not in src


# ── S0 R2：四项 exact contract 反例 ──────────────────────────────


class TestR2ContractGaps:
    """OpenCode BLOCK 四项：先可执行反例，再由实现关闭。"""

    def test_b1_controller_rejects_resolved_generation_path(
        self, tmp_path: Path
    ) -> None:
        """B1：current → generations/g1 时，generation 内路径不得冒充 release 外 controller。"""
        root = tmp_path / "deploy"
        gen = root / "generations" / "g1"
        gen.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(gen)
        # 字面不在 current/ 下，但 resolve 后落在 generation 内
        fake_controller = gen / "nested-controller"
        fake_controller.mkdir()
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_controller_path(fake_controller, current=current)
        assert exc.value.reason == "controller_inside_current"
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.render_mac_controller_plist(
                controller_dir=fake_controller,
                program_arguments=[(fake_controller / "bin").as_posix()],
                current_dir=current,
            )
        assert exc2.value.reason == "controller_inside_current"
        # 真正 release 外仍可通过
        outside = tmp_path / "controller-install"
        outside.mkdir()
        assert sa.validate_controller_path(outside, current=current) == outside

    def test_b2_launcher_rejects_subdir_symlink_escape(
        self, tmp_path: Path
    ) -> None:
        """B2：argv[0] 字面在 current 下，但 current/bin → 外部 时 resolve 逃逸须拒绝。"""
        root = tmp_path / "deploy"
        gen = root / "generations" / "g1"
        gen.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(gen)
        evil = tmp_path / "evil-bin"
        evil.mkdir()
        (evil / "agent-cockpit").write_text("#!/bin/sh\n", encoding="utf-8")
        # current/bin 字面在 current 下，实为指向外部的 symlink
        bin_link = current / "bin"
        # current 是 symlink，写到 gen/bin 作为链接目标更稳：在 gen 上建 bin → evil
        (gen / "bin").symlink_to(evil)
        assert bin_link.is_symlink() or (gen / "bin").is_symlink()
        literal_launcher = f"{current.as_posix()}/bin/agent-cockpit"
        assert literal_launcher.startswith(current.as_posix() + "/")
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.normalize_launcher_argv(
                [literal_launcher],
                current_dir=current,
                deploy_root=root,
            )
        assert exc.value.reason == "launcher_symlink_escape"
        # 合法：artifact 内真实文件，字面 current/... 且 resolve 在 generation 内
        real_bin = gen / "realbin"
        real_bin.mkdir()
        (real_bin / "agent-cockpit").write_text("x", encoding="utf-8")
        ok = f"{current.as_posix()}/realbin/agent-cockpit"
        got = sa.normalize_launcher_argv(
            [ok, "serve"], current_dir=current, deploy_root=root
        )
        assert got[0] == ok  # 输出保持 current 字面，不 resolve 成 generation

    def test_b3_systemd_rejects_wrong_section_and_duplicate_keys(self) -> None:
        """B3：KillMode/ExecStart 等仅允许在 [Service]；重复键拒绝。"""
        misplaced = (
            "[Unit]\n"
            "Description=x\n"
            "KillMode=process\n"
            "[Service]\n"
            "WorkingDirectory=/tmp/x/current\n"
            'ExecStart="/tmp/x/current/bin/app"\n'
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.parse_linux_unit_contract(misplaced)
        assert exc.value.reason == "unit_key_wrong_section"

        duplicate = (
            "[Service]\n"
            "KillMode=process\n"
            "WorkingDirectory=/tmp/x/current\n"
            'ExecStart="/tmp/x/current/bin/app"\n'
            "KillMode=control-group\n"
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.parse_linux_unit_contract(duplicate)
        assert exc2.value.reason == "unit_duplicate_key"

        # 合法 section 布局仍可通过 parse
        good = (
            "[Unit]\n"
            "Description=ok\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            "KillMode=process\n"
            "WorkingDirectory=/tmp/x/current\n"
            'ExecStart="/tmp/x/current/bin/app"\n'
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        c = sa.parse_linux_unit_contract(good)
        assert c.kill_mode == "process"

    def test_b4_macos_rejects_program_key_and_unknown_top_level(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        """B4：顶层键精确 allowlist；Program 可覆盖 ProgramArguments，必须拒绝。"""
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        base = sa.render_mac_main_plist(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        # 注入 Program 键（launchd 会优先于 ProgramArguments）
        evil = base.replace(
            "  <key>ProgramArguments</key>\n",
            "  <key>Program</key>\n"
            "  <string>/tmp/evil-override</string>\n"
            "  <key>ProgramArguments</key>\n",
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.parse_mac_plist_contract(evil)
        assert exc.value.reason == "plist_program_key_forbidden"

        unknown = base.replace(
            "  <key>ThrottleInterval</key>\n",
            "  <key>UserName</key>\n"
            "  <string>root</string>\n"
            "  <key>ThrottleInterval</key>\n",
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.parse_mac_plist_contract(unknown)
        assert exc2.value.reason == "plist_key_not_allowlisted"

        # 干净渲染仍可解析
        sa.validate_mac_plist_contract(
            base,
            expected_label=sa.MAC_MAIN_LABEL,
            expected_working_directory=cur,
            program_arguments=argv,
            current_dir=cur,
            deploy_root=deploy_layout["root"],
        )
