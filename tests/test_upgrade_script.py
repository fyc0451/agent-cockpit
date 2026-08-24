from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo, "-c", "user.name=Upgrade Test", "-c",
        "user.email=upgrade@example.invalid", "commit", "-m", message,
    )
    return _git(repo, "rev-parse", "HEAD")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    install = tmp_path / "agent cockpit"
    state = tmp_path / "state"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "main", str(seed))
    shutil.copy2(ROOT / "upgrade.sh", seed / "upgrade.sh")
    shutil.copy2(ROOT / "install-paths.sh", seed / "install-paths.sh")
    (seed / "install.sh").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$root/INSTALL_FAIL" ]]; then
  exit 42
fi
mkdir -p "$root/.venv/bin"
cat > "$root/.venv/bin/python" <<'PY'
#!/usr/bin/env bash
exit "${FAKE_HEALTH_RC:-0}"
PY
chmod +x "$root/.venv/bin/python"
git -C "$root" rev-parse HEAD > "$root/install.log"
""",
        encoding="utf-8",
    )
    (seed / "upgrade.sh").chmod(0o755)
    (seed / "install.sh").chmod(0o755)
    initial = _commit(seed, "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", "-b", "main", str(remote), str(install))
    return seed, install, state, initial


def _run(install: Path, state: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(extra_env)
    env["XDG_STATE_HOME"] = str(state)
    return subprocess.run(
        ["bash", str(install / "upgrade.sh")], cwd=install, env=env,
        capture_output=True, text=True, timeout=30,
    )


def _push_update(seed: Path, name: str, body: str = "updated\n") -> str:
    (seed / name).write_text(body, encoding="utf-8")
    target = _commit(seed, f"update {name}")
    _git(seed, "push", "origin", "main")
    return target


def test_upgrade_fast_forwards_installs_and_checks_health(tmp_path: Path) -> None:
    seed, install, state, _ = _fixture(tmp_path)
    target = _push_update(seed, "VERSION-MARKER")
    local_note = install / "LOCAL-NOTE"
    local_note.write_text("keep me\n", encoding="utf-8")

    result = _run(install, state)

    assert result.returncode == 0, result.stderr
    assert _git(install, "rev-parse", "HEAD") == target
    assert (install / "install.log").read_text().strip() == target
    assert local_note.read_text(encoding="utf-8") == "keep me\n"
    assert "升级完成" in result.stdout
    assert not (state / "agent-cockpit" / "source-upgrade.lock.d").exists()


def test_upgrade_rejects_tracked_changes_before_fetch(tmp_path: Path) -> None:
    _, install, state, initial = _fixture(tmp_path)
    (install / "install.sh").write_text("changed\n", encoding="utf-8")

    result = _run(install, state)

    assert result.returncode == 1
    assert "未提交的 tracked 修改" in result.stderr
    assert _git(install, "rev-parse", "HEAD") == initial


def test_upgrade_rejects_local_ahead_or_diverged_branch(tmp_path: Path) -> None:
    seed, install, state, _ = _fixture(tmp_path)
    (install / "LOCAL").write_text("local\n", encoding="utf-8")
    _commit(install, "local commit")
    _push_update(seed, "REMOTE")

    result = _run(install, state)

    assert result.returncode == 1
    assert "领先或已与上游分叉" in result.stderr


def test_upgrade_rolls_back_when_new_install_fails(tmp_path: Path) -> None:
    seed, install, state, initial = _fixture(tmp_path)
    _push_update(seed, "INSTALL_FAIL")

    result = _run(install, state)

    assert result.returncode == 1
    assert _git(install, "rev-parse", "HEAD") == initial
    assert (install / "install.log").read_text().strip() == initial
    assert "已回滚并恢复旧版本" in result.stderr


def test_upgrade_rolls_back_when_health_check_fails(tmp_path: Path) -> None:
    seed, install, state, initial = _fixture(tmp_path)
    _push_update(seed, "VERSION-MARKER")

    result = _run(install, state, FAKE_HEALTH_RC="1")

    assert result.returncode == 1
    assert _git(install, "rev-parse", "HEAD") == initial
    assert "rollback_failed" in result.stderr


def test_upgrade_is_noop_when_already_current(tmp_path: Path) -> None:
    _, install, state, initial = _fixture(tmp_path)

    result = _run(install, state)

    assert result.returncode == 0, result.stderr
    assert _git(install, "rev-parse", "HEAD") == initial
    assert "已是最新版本" in result.stdout
    assert not (install / "install.log").exists()


def test_upgrade_rejects_live_concurrent_owner(tmp_path: Path) -> None:
    seed, install, state, _ = _fixture(tmp_path)
    _push_update(seed, "VERSION-MARKER")
    lock = state / "agent-cockpit" / "source-upgrade.lock.d"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"pid={os.getpid()}\n", encoding="utf-8")

    result = _run(install, state)

    assert result.returncode == 1
    assert "已有升级任务进行中" in result.stderr


def test_upgrade_rejects_symlinked_state_root_without_touching_target(
    tmp_path: Path,
) -> None:
    seed, install, state, _ = _fixture(tmp_path)
    _push_update(seed, "VERSION-MARKER")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "owner"
    sentinel.write_text("do not remove\n", encoding="utf-8")
    state.mkdir()
    (state / "agent-cockpit").symlink_to(external, target_is_directory=True)

    result = _run(install, state)

    assert result.returncode == 1
    assert "状态目录不得为符号链接" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "do not remove\n"


def test_upgrade_does_not_clean_unknown_stale_lock_content(tmp_path: Path) -> None:
    seed, install, state, _ = _fixture(tmp_path)
    _push_update(seed, "VERSION-MARKER")
    lock = state / "agent-cockpit" / "source-upgrade.lock.d"
    lock.mkdir(parents=True)
    (lock / "owner").write_text("pid=999999999\n", encoding="utf-8")
    unknown = lock / "foreign"
    unknown.write_text("keep\n", encoding="utf-8")

    result = _run(install, state)

    assert result.returncode == 1
    assert "锁目录包含未知内容" in result.stderr
    assert unknown.read_text(encoding="utf-8") == "keep\n"
    assert (lock / "owner").is_file()
