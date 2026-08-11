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
            sa.validate_controller_path(
                bad,
                current=deploy_layout["current"],
                deploy_root=deploy_layout["root"],
            )
        assert exc.value.reason == "controller_inside_current"

    def test_controller_outside_ok(self, deploy_layout: dict[str, Path]) -> None:
        got = sa.validate_controller_path(
            deploy_layout["controller"],
            current=deploy_layout["current"],
            deploy_root=deploy_layout["root"],
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
    def test_fixed_server_launcher_requires_exact_literal_argv0(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        assert sa.normalize_fixed_server_launcher_argv(
            deploy_layout["main_argv"],
            current_dir=deploy_layout["current"],
            deploy_root=deploy_layout["root"],
        ) == deploy_layout["main_argv"]

    @pytest.mark.parametrize(
        "argv0",
        [
            "agent-cockpit",
            "/usr/bin/env",
            "/usr/bin/python3",
            "{current}/server.py",
            "{current}/launchd.sh",
            "{current}/.venv/bin/python",
            "{generation}/bin/agent-cockpit",
        ],
    )
    def test_fixed_server_launcher_never_guesses_or_accepts_alternates(
        self, deploy_layout: dict[str, Path], argv0: str
    ) -> None:
        value = argv0.format(
            current=deploy_layout["current"].as_posix(),
            generation=deploy_layout["gen"].as_posix(),
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.normalize_fixed_server_launcher_argv(
                (value, "serve"),
                current_dir=deploy_layout["current"],
                deploy_root=deploy_layout["root"],
            )
        assert exc.value.reason in {
            "relative_path",
            "launcher_outside_current",
            "fixed_launcher_argv0_mismatch",
        }

    def test_fixed_server_launcher_rejects_post_promotion_leaf_symlink(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        launcher = deploy_layout["gen"] / "bin" / "agent-cockpit"
        replacement = launcher.with_name("replacement")
        launcher.rename(replacement)
        launcher.symlink_to(replacement.name)

        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.normalize_fixed_server_launcher_argv(
                deploy_layout["main_argv"],
                current_dir=deploy_layout["current"],
                deploy_root=deploy_layout["root"],
            )
        assert exc.value.reason == "fixed_launcher_resolve_mismatch"

    def test_existing_launcher_normalizer_compatibility_is_unchanged(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        legacy = (
            f"{deploy_layout['current'].as_posix()}/server.py",
            "serve",
        )
        assert sa.normalize_launcher_argv(
            legacy,
            current_dir=deploy_layout["current"],
            deploy_root=deploy_layout["root"],
        ) == legacy

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
        # R4：KillMode 同时是固定安全字段；优先 linux_fixed_field_mismatch
        assert exc.value.reason in {
            "killmode_not_process",
            "linux_fixed_field_mismatch",
        }

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
            deploy_root=deploy_layout["root"],
        )
        assert f"<string>{sa.MAC_CONTROLLER_LABEL}</string>" in text
        assert f"<string>{sa.MAC_MAIN_LABEL}</string>" not in text
        contract = sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_CONTROLLER_LABEL,
            expected_working_directory=ctrl,
            program_arguments=argv,
            current_dir=deploy_layout["current"],
            deploy_root=deploy_layout["root"],
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
        deploy_root=root,
    )
    sa.validate_mac_plist_contract(
        ctrl,
        expected_label=sa.MAC_CONTROLLER_LABEL,
        expected_working_directory=deploy_layout["controller"],
        program_arguments=[ctrl_bin.as_posix()],
        current_dir=cur,
        deploy_root=root,
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
            sa.validate_controller_path(
                fake_controller, current=current, deploy_root=root
            )
        assert exc.value.reason == "controller_inside_current"
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.render_mac_controller_plist(
                controller_dir=fake_controller,
                program_arguments=[(fake_controller / "bin").as_posix()],
                current_dir=current,
                deploy_root=root,
            )
        assert exc2.value.reason == "controller_inside_current"
        # 真正 release 外仍可通过
        outside = tmp_path / "controller-install"
        outside.mkdir()
        assert (
            sa.validate_controller_path(outside, current=current, deploy_root=root)
            == outside
        )

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


# ── S0 R3：inactive generation / repeated section / Mac exact ────


class TestR3ContractGaps:
    def test_r3_inactive_generation_and_release_tree_rejected(
        self, tmp_path: Path
    ) -> None:
        """R3-1：拒全部 deploy/generations/**（含 inactive），controller 须在 release tree 外。"""
        root = tmp_path / "deploy"
        active = root / "generations" / "g-active"
        inactive = root / "generations" / "g-old"
        active.mkdir(parents=True)
        inactive.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(active)
        # inactive generation 内 — 不在 current.resolve 下，但在 generations/**
        fake = inactive / "controller-lookalike"
        fake.mkdir()
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_controller_path(fake, current=current, deploy_root=root)
        assert exc.value.reason == "controller_inside_generations"
        # release tree 内其它路径（非 generations）也拒
        under_release = root / "misc-controller"
        under_release.mkdir()
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.validate_controller_path(under_release, current=current, deploy_root=root)
        assert exc2.value.reason == "controller_inside_release_tree"
        # release 外 OK
        outside = tmp_path / "controller-install"
        outside.mkdir()
        assert (
            sa.validate_controller_path(outside, current=current, deploy_root=root)
            == outside
        )
        # R4：禁止省略 deploy_root（TypeError）
        with pytest.raises(TypeError):
            sa.validate_controller_path(fake, current=current)  # type: ignore[call-arg]

    def test_r3_repeated_service_section_rejected(self) -> None:
        """R3-2：重复 [Service]/[Unit]/[Install] section 不得拼接合同。"""
        stitched = (
            "[Unit]\n"
            "Description=a\n"
            "[Service]\n"
            "KillMode=process\n"
            "WorkingDirectory=/tmp/x/current\n"
            'ExecStart="/tmp/x/current/bin/app"\n'
            "[Service]\n"
            "KillMode=control-group\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.parse_linux_unit_contract(stitched)
        assert exc.value.reason == "unit_duplicate_section"
        # 重复 Unit 同样拒绝
        dup_unit = (
            "[Unit]\nDescription=a\n"
            "[Unit]\nDescription=b\n"
            "[Service]\n"
            "KillMode=process\n"
            "WorkingDirectory=/tmp/x/current\n"
            'ExecStart="/tmp/x/current/bin/app"\n'
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.parse_linux_unit_contract(dup_unit)
        assert exc2.value.reason == "unit_duplicate_section"

    def test_r3_mac_exact_fields_and_same_origin_logs(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        """R3-3：禁止 bool 强转；RunAtLoad/KeepAlive/Throttle/logs exact；拒 /tmp/evil。"""
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        base = sa.render_mac_main_plist(
            current_dir=cur, program_arguments=argv, deploy_root=deploy_layout["root"]
        )
        # 缺失 RunAtLoad 不得被 bool(None)=False 悄悄通过 parse 后 validate
        missing = base.replace(
            "  <key>RunAtLoad</key>\n  <true/>\n",
            "",
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.parse_mac_plist_contract(missing)
        assert exc.value.reason == "plist_field_type"

        # KeepAlive 为 false — type ok 但 exact value 失败
        ka_false = base.replace(
            "  <key>KeepAlive</key>\n  <true/>\n",
            "  <key>KeepAlive</key>\n  <false/>\n",
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.validate_mac_plist_contract(
                ka_false,
                expected_label=sa.MAC_MAIN_LABEL,
                expected_working_directory=cur,
                program_arguments=argv,
                current_dir=cur,
                deploy_root=deploy_layout["root"],
            )
        assert exc2.value.reason == "plist_keep_alive_not_true"

        # ThrottleInterval 非 3
        thr = base.replace(
            "  <key>ThrottleInterval</key>\n  <integer>3</integer>\n",
            "  <key>ThrottleInterval</key>\n  <integer>99</integer>\n",
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc3:
            sa.validate_mac_plist_contract(
                thr,
                expected_label=sa.MAC_MAIN_LABEL,
                expected_working_directory=cur,
                program_arguments=argv,
                current_dir=cur,
                deploy_root=deploy_layout["root"],
            )
        assert exc3.value.reason == "plist_throttle_interval_not_3"

        # 日志路径逃到 /tmp/evil
        evil_log = base.replace(
            f"{cur.as_posix()}/agent-cockpit.stdout.log",
            "/tmp/evil.stdout.log",
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc4:
            sa.validate_mac_plist_contract(
                evil_log,
                expected_label=sa.MAC_MAIN_LABEL,
                expected_working_directory=cur,
                program_arguments=argv,
                current_dir=cur,
                deploy_root=deploy_layout["root"],
            )
        assert exc4.value.reason == "plist_stdout_path_mismatch"

        # 干净主 LA + controller 仍通过
        sa.validate_mac_plist_contract(
            base,
            expected_label=sa.MAC_MAIN_LABEL,
            expected_working_directory=cur,
            program_arguments=argv,
            current_dir=cur,
            deploy_root=deploy_layout["root"],
        )
        ctrl = deploy_layout["controller"]
        prog = (ctrl / "run.sh").as_posix()
        Path(prog).write_text("#!/bin/sh\n", encoding="utf-8")
        ctrl_plist = sa.render_mac_controller_plist(
            controller_dir=ctrl,
            program_arguments=[prog],
            current_dir=cur,
            deploy_root=deploy_layout["root"],
        )
        sa.validate_mac_plist_contract(
            ctrl_plist,
            expected_label=sa.MAC_CONTROLLER_LABEL,
            expected_working_directory=ctrl,
            program_arguments=[prog],
            current_dir=cur,
            deploy_root=deploy_layout["root"],
        )


# ── S0 R4：canonical deploy / controller argv / fixed fields ────


class TestR4ContractGaps:
    def test_r4_b1_requires_canonical_current_and_deploy_root(
        self, tmp_path: Path
    ) -> None:
        """B1：必须显式 deploy_root+字面 {root}/current；禁止省略/伪造。"""
        root = tmp_path / "deploy"
        gen = root / "generations" / "g1"
        gen.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(gen)
        outside = tmp_path / "controller-install"
        outside.mkdir()
        # 省略任一侧 → TypeError
        with pytest.raises(TypeError):
            sa.validate_controller_path(outside, current=current)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            sa.validate_controller_path(outside, deploy_root=root)  # type: ignore[call-arg]
        # 伪造 deploy_root（与 current 父目录不一致）
        forged = tmp_path / "forged-root"
        forged.mkdir()
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_controller_path(
                outside, current=current, deploy_root=forged
            )
        assert exc.value.reason in {
            "current_outside_deploy_root",
            "current_deploy_root_mismatch",
        }
        # 伪造 current 非 root/current
        other = root / "not-current"
        other.mkdir()
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.require_canonical_current_and_deploy(
                current=other, deploy_root=root
            )
        assert exc2.value.reason == "current_name_required"
        # 合法
        assert (
            sa.validate_controller_path(outside, current=current, deploy_root=root)
            == outside
        )

    def test_r4_b2_controller_argv_must_stay_in_controller_dir(
        self, tmp_path: Path
    ) -> None:
        """B2：argv[0] 字面+resolve 均在 controller_dir 内且 release tree 外。"""
        root = tmp_path / "deploy"
        gen = root / "generations" / "g1"
        gen.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(gen)
        ctrl = tmp_path / "controller-install"
        ctrl.mkdir()
        evil = tmp_path / "evil"
        evil.mkdir()
        (evil / "bin").write_text("x", encoding="utf-8")
        # 字面指向 controller 外
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.normalize_controller_argv(
                [evil.as_posix() + "/bin"],
                controller_dir=ctrl,
                current=current,
                deploy_root=root,
            )
        assert exc.value.reason == "controller_argv_outside_controller"
        # controller 内 symlink 逃到外部
        link = ctrl / "jump"
        link.symlink_to(evil)
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.normalize_controller_argv(
                [f"{ctrl.as_posix()}/jump/bin"],
                controller_dir=ctrl,
                current=current,
                deploy_root=root,
            )
        assert exc2.value.reason == "controller_argv_symlink_escape"
        # 合法：controller 内真实文件
        real = ctrl / "run.sh"
        real.write_text("#!/bin/sh\n", encoding="utf-8")
        got = sa.normalize_controller_argv(
            [real.as_posix(), "run"],
            controller_dir=ctrl,
            current=current,
            deploy_root=root,
        )
        assert got[0] == real.as_posix()

    def test_r4_b3_fixed_mac_log_names_and_linux_fixed_fields(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        """B3：Mac 日志名固定；Linux Type/Restart/NoNewPrivileges/UMask 精确。"""
        cur = deploy_layout["current"]
        root = deploy_layout["root"]
        argv = deploy_layout["main_argv"]
        unit = sa.render_linux_unit(
            current_dir=cur, program_arguments=argv, deploy_root=root
        )
        sa.validate_linux_unit_contract(
            unit, current_dir=cur, program_arguments=argv, deploy_root=root
        )
        # 篡改 Type
        bad_type = unit.replace("Type=simple", "Type=oneshot")
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_linux_unit_contract(
                bad_type, current_dir=cur, program_arguments=argv, deploy_root=root
            )
        assert exc.value.reason == "linux_fixed_field_mismatch"
        # 篡改 NoNewPrivileges
        bad_priv = unit.replace("NoNewPrivileges=true", "NoNewPrivileges=false")
        with pytest.raises(sa.SupervisorAdapterError) as exc2:
            sa.validate_linux_unit_contract(
                bad_priv, current_dir=cur, program_arguments=argv, deploy_root=root
            )
        assert exc2.value.reason == "linux_fixed_field_mismatch"
        # Mac：日志名被改成 caller 自定义
        main = sa.render_mac_main_plist(
            current_dir=cur, program_arguments=argv, deploy_root=root
        )
        evil = main.replace(
            sa.MAC_MAIN_STDOUT_NAME, "custom.out.log"
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc3:
            sa.validate_mac_plist_contract(
                evil,
                expected_label=sa.MAC_MAIN_LABEL,
                expected_working_directory=cur,
                program_arguments=argv,
                current_dir=cur,
                deploy_root=root,
            )
        assert exc3.value.reason == "plist_stdout_path_mismatch"
        # render 签名无 stdout_name 参数
        import inspect
        sig = inspect.signature(sa.render_mac_controller_plist)
        assert "stdout_name" not in sig.parameters
        assert "stderr_name" not in sig.parameters


def _handcraft_controller_plist(
    *,
    working_directory: str,
    program_arguments: tuple[str, ...],
) -> str:
    """构造表面合规的 controller plist（不经 render，用于 validator 反例）。"""
    args_xml = "\n".join(
        f"    <string>{sa.escape_plist_xml(a)}</string>" for a in program_arguments
    )
    wd = sa.escape_plist_xml(working_directory)
    stdout = sa.escape_plist_xml(f"{working_directory}/{sa.MAC_CONTROLLER_STDOUT_NAME}")
    stderr = sa.escape_plist_xml(f"{working_directory}/{sa.MAC_CONTROLLER_STDERR_NAME}")
    label = sa.escape_plist_xml(sa.MAC_CONTROLLER_LABEL)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{label}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{args_xml}\n"
        "  </array>\n"
        "  <key>WorkingDirectory</key>\n"
        f"  <string>{wd}</string>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <true/>\n"
        "  <key>ThrottleInterval</key>\n"
        "  <integer>3</integer>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{stdout}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{stderr}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


# ── S0 R5：controller validator 无条件 canonical + argv 边界 ────


class TestR5ControllerValidatorGaps:
    def test_r5_omitted_context_must_not_accept_tmp_evil_controller(
        self, tmp_path: Path
    ) -> None:
        """Terra BLOCK：controller label + /tmp/evil 在省略上下文时不得通过。"""
        evil = Path("/tmp") / f"evil-controller-s0r5-{tmp_path.name}"
        evil.mkdir(parents=True, exist_ok=True)
        bin_path = evil / "run.sh"
        bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
        text = _handcraft_controller_plist(
            working_directory=evil.as_posix(),
            program_arguments=(bin_path.as_posix(),),
        )
        # 省略 program_arguments / current_dir / deploy_root —— 旧实现错误 ACCEPT
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_mac_plist_contract(
                text,
                expected_label=sa.MAC_CONTROLLER_LABEL,
                expected_working_directory=evil,
            )
        assert exc.value.reason == "controller_canonical_required"

    def test_r5_controller_requires_canonical_even_with_expected_argv(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        ctrl = deploy_layout["controller"]
        prog = (ctrl / "run.sh").as_posix()
        Path(prog).write_text("#!/bin/sh\n", encoding="utf-8")
        text = sa.render_mac_controller_plist(
            controller_dir=ctrl,
            program_arguments=[prog],
            current_dir=deploy_layout["current"],
            deploy_root=deploy_layout["root"],
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_mac_plist_contract(
                text,
                expected_label=sa.MAC_CONTROLLER_LABEL,
                expected_working_directory=ctrl,
                program_arguments=[prog],
                # 故意省略 current/deploy
            )
        assert exc.value.reason == "controller_canonical_required"

    def test_r5_validator_rejects_argv_outside_controller_dir(
        self, deploy_layout: dict[str, Path], tmp_path: Path
    ) -> None:
        ctrl = deploy_layout["controller"]
        outside = tmp_path / "not-controller" / "bin"
        outside.parent.mkdir()
        outside.write_text("x", encoding="utf-8")
        text = _handcraft_controller_plist(
            working_directory=ctrl.as_posix(),
            program_arguments=(outside.as_posix(),),
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_mac_plist_contract(
                text,
                expected_label=sa.MAC_CONTROLLER_LABEL,
                expected_working_directory=ctrl,
                current_dir=deploy_layout["current"],
                deploy_root=deploy_layout["root"],
            )
        assert exc.value.reason == "controller_argv_outside_controller"

    def test_r5_validator_rejects_controller_wd_inside_release_tree(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        root = deploy_layout["root"]
        cur = deploy_layout["current"]
        # 落在 release tree 内的伪 controller
        inside = root / "fake-controller"
        inside.mkdir()
        prog = inside / "run.sh"
        prog.write_text("#!/bin/sh\n", encoding="utf-8")
        text = _handcraft_controller_plist(
            working_directory=inside.as_posix(),
            program_arguments=(prog.as_posix(),),
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_mac_plist_contract(
                text,
                expected_label=sa.MAC_CONTROLLER_LABEL,
                expected_working_directory=inside,
                current_dir=cur,
                deploy_root=root,
            )
        assert exc.value.reason == "controller_inside_release_tree"

    def test_r5_validator_rejects_nested_symlink_argv_escape(
        self, deploy_layout: dict[str, Path], tmp_path: Path
    ) -> None:
        ctrl = deploy_layout["controller"]
        evil = tmp_path / "escape-target"
        evil.mkdir()
        (evil / "payload").write_text("x", encoding="utf-8")
        link = ctrl / "jump"
        link.symlink_to(evil)
        text = _handcraft_controller_plist(
            working_directory=ctrl.as_posix(),
            program_arguments=(f"{ctrl.as_posix()}/jump/payload",),
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_mac_plist_contract(
                text,
                expected_label=sa.MAC_CONTROLLER_LABEL,
                expected_working_directory=ctrl,
                current_dir=deploy_layout["current"],
                deploy_root=deploy_layout["root"],
            )
        assert exc.value.reason == "controller_argv_symlink_escape"

    def test_r5_main_plist_contract_still_requires_launcher_only(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        """主 app plist 不因 R5 改成 controller 语义。"""
        cur = deploy_layout["current"]
        argv = deploy_layout["main_argv"]
        text = sa.render_mac_main_plist(
            current_dir=cur,
            program_arguments=argv,
            deploy_root=deploy_layout["root"],
        )
        sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_MAIN_LABEL,
            expected_working_directory=cur,
            program_arguments=argv,
            current_dir=cur,
            deploy_root=deploy_layout["root"],
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_mac_plist_contract(
                text,
                expected_label=sa.MAC_MAIN_LABEL,
                expected_working_directory=cur,
            )
        assert exc.value.reason == "main_launcher_required"
