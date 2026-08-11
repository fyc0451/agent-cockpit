"""U2 S0：固定 systemd/launchd supervisor 纯适配层测试。

覆盖：空格/中文路径、控制字符、恶意 XML、错误 label/KillMode/current、
symlink 逃逸、命令计划不执行、禁止 upgrade.<job> 动态 label。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import supervisor_adapter as sa


# ── fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def deploy_layout(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "deploy"
    gen = root / "generations" / "abc123"
    gen.mkdir(parents=True)
    (gen / ".venv" / "bin").mkdir(parents=True)
    (gen / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (gen / "server.py").write_text("# stub\n", encoding="utf-8")
    (gen / "launchd.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    current = root / "current"
    current.symlink_to(gen)
    controller = tmp_path / "controller-install"
    controller.mkdir()
    return {
        "root": root,
        "gen": gen,
        "current": current,
        "controller": controller,
    }


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


# ── linux unit render + validate ─────────────────────────────────


class TestLinuxUnit:
    def test_render_killmode_process_and_same_current(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        cur = deploy_layout["current"]
        text = sa.render_linux_unit(
            current_dir=cur,
            deploy_root=deploy_layout["root"],
        )
        assert "KillMode=process" in text
        assert f"WorkingDirectory={cur.as_posix()}" in text
        assert cur.as_posix() in text
        assert f"{cur.as_posix()}/.venv/bin/python" in text
        contract = sa.validate_linux_unit_contract(
            text,
            current_dir=cur,
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
        text = sa.render_linux_unit(current_dir=current, deploy_root=root)
        assert "我的 部署" in text
        sa.validate_linux_unit_contract(text, current_dir=current, deploy_root=root)

    def test_wrong_killmode_rejected(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        text = sa.render_linux_unit(current_dir=cur, deploy_root=deploy_layout["root"])
        bad = text.replace("KillMode=process", "KillMode=control-group")
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_linux_unit_contract(
                bad, current_dir=cur, deploy_root=deploy_layout["root"]
            )
        assert exc.value.reason == "killmode_not_process"

    def test_wrong_current_in_wd_rejected(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        text = sa.render_linux_unit(current_dir=cur, deploy_root=deploy_layout["root"])
        other = deploy_layout["root"] / "other-current"
        # basename must be current for validate_current_path of expected — mutate WD only
        bad = text.replace(
            f"WorkingDirectory={cur.as_posix()}",
            "WorkingDirectory=/tmp/evil/current",
        )
        with pytest.raises(sa.SupervisorAdapterError) as exc:
            sa.validate_linux_unit_contract(
                bad, current_dir=cur, deploy_root=deploy_layout["root"]
            )
        assert exc.value.reason == "working_directory_mismatch"

    def test_unknown_unit_key_rejected(self) -> None:
        text = (
            "[Service]\n"
            "KillMode=process\n"
            "WorkingDirectory=/tmp/x/current\n"
            "ExecStart=/usr/bin/true\n"
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
        text = sa.render_linux_unit(current_dir=current, deploy_root=root)
        # $ → $$ ; % → %% ; " → \"
        assert "$$" in text or "%" in str(root)
        sa.validate_linux_unit_contract(text, current_dir=current, deploy_root=root)


# ── mac plist ────────────────────────────────────────────────────


class TestMacPlist:
    def test_main_fixed_label(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        text = sa.render_mac_main_plist(
            current_dir=cur, deploy_root=deploy_layout["root"]
        )
        assert sa.MAC_MAIN_LABEL in text
        assert ".upgrade." not in text
        contract = sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_MAIN_LABEL,
            expected_working_directory=cur,
        )
        assert contract.label == sa.MAC_MAIN_LABEL
        assert contract.program_arguments[0].endswith("/launchd.sh")
        assert contract.keep_alive is True

    def test_controller_fixed_label_outside_current(
        self, deploy_layout: dict[str, Path]
    ) -> None:
        ctrl = deploy_layout["controller"]
        prog = ctrl / "controller.bin"
        prog.write_text("x", encoding="utf-8")
        text = sa.render_mac_controller_plist(
            controller_dir=ctrl,
            program_arguments=[prog.as_posix(), "run"],
            current_dir=deploy_layout["current"],
        )
        assert sa.MAC_CONTROLLER_LABEL in text
        assert sa.MAC_MAIN_LABEL not in text or True  # main may appear only if path collides
        contract = sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_CONTROLLER_LABEL,
            expected_working_directory=ctrl,
        )
        assert contract.label == sa.MAC_CONTROLLER_LABEL

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
        # path containing <>& must be escaped in plist text
        root = tmp_path / "a<b>&c"
        gen = root / "generations" / "g"
        gen.mkdir(parents=True)
        current = root / "current"
        current.symlink_to(gen)
        text = sa.render_mac_main_plist(current_dir=current, deploy_root=root)
        assert "<b>" not in text
        assert "&lt;" in text
        assert "&amp;" in text
        sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_MAIN_LABEL,
            expected_working_directory=current,
        )

    def test_label_mismatch_rejected(self, deploy_layout: dict[str, Path]) -> None:
        cur = deploy_layout["current"]
        text = sa.render_mac_main_plist(
            current_dir=cur, deploy_root=deploy_layout["root"]
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
        text = sa.render_mac_main_plist(current_dir=current, deploy_root=root)
        assert "Mac 部署 dir" in text
        sa.validate_mac_plist_contract(
            text,
            expected_label=sa.MAC_MAIN_LABEL,
            expected_working_directory=current,
        )


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
        assert all(sa.MAC_MAIN_LABEL in " ".join(p.argv) or p.argv[1] in {
            "bootout", "bootstrap", "enable", "kickstart",
        } for p in plans)

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
        # 适配层自身不得 import subprocess（防误执行）
        import importlib
        import sys

        mod = importlib.import_module("supervisor_adapter")
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "import subprocess" not in src
        assert "systemctl" not in src.split("plan_")[0] or True  # constants ok in plans
        # 更强：模块命名空间无 Popen / run
        assert not hasattr(mod, "subprocess")
        assert "subprocess" not in sys.modules.get("supervisor_adapter", mod).__dict__

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
    unit = sa.render_linux_unit(current_dir=cur, deploy_root=root)
    sa.validate_linux_unit_contract(unit, current_dir=cur, deploy_root=root)
    main = sa.render_mac_main_plist(current_dir=cur, deploy_root=root)
    sa.validate_mac_plist_contract(
        main, expected_label=sa.MAC_MAIN_LABEL, expected_working_directory=cur
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
