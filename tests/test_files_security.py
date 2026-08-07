"""files.py 安全模型测试:显式白名单、删除保护、原子写。"""
import os
import stat

import pytest
from fastapi.testclient import TestClient

import files
import server


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """隔离环境:home 指向 tmp_path,DB 注册项目置空。

    白名单 = _PROJECT_DIR(真实仓库,测试中不触碰)+ tmp_path 下三个子目录。
    """
    monkeypatch.setattr(files, "_HOME", tmp_path.resolve())
    monkeypatch.setattr(files, "_registered_project_roots", lambda: [])
    return tmp_path


def _mkdirs(p):
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── 路径白名单 ──────────────────────────────────────────────────

def test_resolve_rejects_empty_path():
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            files._resolve(bad)


def test_resolve_rejects_home_outside_whitelist(tmp_path):
    # home 本身不再是根:直接位于 home 下的文件必须拒绝
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    with pytest.raises(ValueError):
        files._resolve(str(secret))
    with pytest.raises(ValueError):
        files._resolve(str(tmp_path))


def test_resolve_rejects_sensitive_and_system_paths():
    with pytest.raises(ValueError):
        files._resolve("/etc/passwd")
    with pytest.raises(ValueError):
        files._resolve("~/.ssh/id_rsa")


def test_resolve_accepts_whitelisted_subdirs(tmp_path):
    for name in ("dashboard-uploads", "dashboard-data", "agent-mail-tools"):
        d = _mkdirs(tmp_path / name)
        f = d / "a.txt"
        f.write_text("hi")
        assert files._resolve(str(f)) == f.resolve()
    # 相对路径基于 home 解析
    assert files._resolve("dashboard-uploads/a.txt") == (tmp_path / "dashboard-uploads" / "a.txt").resolve()


def test_read_file_reports_invalid_utf8_as_validation_error(tmp_path):
    directory = _mkdirs(tmp_path / "dashboard-uploads")
    target = directory / "broken.txt"
    target.write_bytes(b"valid-prefix\xff")

    with pytest.raises(ValueError, match="UTF-8"):
        files.read_file(str(target))


def test_resolve_accepts_registered_project(tmp_path, monkeypatch):
    proj = _mkdirs(tmp_path / "my-proj")
    f = proj / "x.py"
    f.write_text("")
    monkeypatch.setattr(files, "_registered_project_roots", lambda: [proj.resolve()])
    assert files._resolve(str(f)) == f.resolve()
    # 不存在/未注册的目录仍拒绝
    other = _mkdirs(tmp_path / "other")
    with pytest.raises(ValueError):
        files._resolve(str(other / "x.py"))


def test_custom_root_persists_is_grouped_and_can_be_removed(tmp_path):
    custom = _mkdirs(tmp_path / "other-project")

    added = files.add_custom_root(str(custom))

    assert added == {"path": str(custom.resolve()), "added": True}
    assert str(custom.resolve()) in files.allowed_roots()
    assert files.allowed_root_groups()["custom"] == [str(custom.resolve())]
    config = tmp_path / ".config" / "agent-cockpit" / "file-roots.json"
    assert config.is_file()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600

    removed = files.remove_custom_root(str(custom))

    assert removed == {"path": str(custom.resolve()), "removed": True}
    assert files.allowed_root_groups()["custom"] == []


def test_custom_root_rejects_broad_or_missing_directories(tmp_path):
    with pytest.raises(ValueError, match="具体目录"):
        files.add_custom_root("/")
    with pytest.raises(ValueError, match="具体目录"):
        files.add_custom_root(str(tmp_path))
    with pytest.raises(ValueError, match="不存在"):
        files.add_custom_root(str(tmp_path / "missing"))
    sensitive = _mkdirs(tmp_path / ".ssh")
    with pytest.raises(ValueError, match="敏感"):
        files.add_custom_root(str(sensitive))
    with pytest.raises(ValueError, match="系统运行目录"):
        files.add_custom_root("/proc")


def test_only_custom_roots_can_be_removed():
    with pytest.raises(ValueError, match="不是自定义目录"):
        files.remove_custom_root(str(files._PROJECT_DIR))


def test_read_file_outside_whitelist_rejected(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=1")
    with pytest.raises(ValueError):
        files.read_file(str(secret))


def test_custom_root_api_adds_and_removes_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    custom = _mkdirs(tmp_path / "api-project")
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    added = client.post(
        "/api/files/roots", headers=headers, json={"path": str(custom)}
    )

    assert added.status_code == 200
    assert added.json()["path"] == str(custom.resolve())
    # 接口响应对 /tmp 做展示过滤;增删效果用底层分组验证
    assert str(custom.resolve()) in files.allowed_root_groups()["custom"]

    removed = client.delete(
        "/api/files/roots", headers=headers, params={"path": str(custom)}
    )

    assert removed.status_code == 200
    assert str(custom.resolve()) not in files.allowed_root_groups()["custom"]


# ── 删除保护 ────────────────────────────────────────────────────

def test_delete_allowed_root_forbidden(tmp_path):
    for name in ("dashboard-uploads", "dashboard-data", "agent-mail-tools"):
        d = _mkdirs(tmp_path / name)
        with pytest.raises(ValueError, match="根目录"):
            files.delete_file(str(d))
        assert d.is_dir()


def test_delete_nonempty_dir_uses_rmdir_only(tmp_path):
    up = _mkdirs(tmp_path / "dashboard-uploads")
    d = _mkdirs(up / "nonempty")
    (d / "f.txt").write_text("x")
    with pytest.raises(ValueError):
        files.delete_file(str(d))
    # 非空目录及其内容必须原样保留(不得递归删除)
    assert (d / "f.txt").is_file()


def test_delete_empty_dir_and_file(tmp_path):
    up = _mkdirs(tmp_path / "dashboard-uploads")
    d = _mkdirs(up / "empty")
    assert files.delete_file(str(d))["type"] == "dir"
    assert not d.exists()
    f = up / "f.txt"
    f.write_text("x")
    assert files.delete_file(str(f))["type"] == "file"
    assert not f.exists()


# ── 原子写 ──────────────────────────────────────────────────────

def test_write_preserves_mode_and_content(tmp_path):
    up = _mkdirs(tmp_path / "dashboard-uploads")
    f = up / "m.txt"
    f.write_text("old")
    os.chmod(f, 0o640)
    files.write_file(str(f), "new-content")
    assert f.read_text() == "new-content"
    assert stat.S_IMODE(f.stat().st_mode) == 0o640


def test_write_requires_create_for_new_file(tmp_path):
    up = _mkdirs(tmp_path / "dashboard-uploads")
    f = up / "new.txt"
    with pytest.raises(ValueError):
        files.write_file(str(f), "x")
    files.write_file(str(f), "x", create=True)
    assert f.read_text() == "x"


def test_write_leaves_no_tmp_files(tmp_path):
    up = _mkdirs(tmp_path / "dashboard-uploads")
    f = up / "clean.txt"
    files.write_file(str(f), "x", create=True)
    assert not list(up.glob("*.dash-tmp"))
    assert not list(up.glob(".*.dash-tmp"))


def test_write_binary_rejected(tmp_path):
    up = _mkdirs(tmp_path / "dashboard-uploads")
    f = up / "b.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    with pytest.raises(ValueError):
        files.write_file(str(f), "x")


def test_write_outside_whitelist_rejected(tmp_path):
    f = tmp_path / "evil.sh"
    f.write_text("#!/bin/sh\n")
    with pytest.raises(ValueError):
        files.write_file(str(f), "pwned")
    assert f.read_text() == "#!/bin/sh\n"


# ── symlink 删除语义 ────────────────────────────────────────────

def test_delete_symlink_to_whitelisted_target_removes_link_only(tmp_path):
    """link → 白名单内目标:只删 link,目标文件保留。"""
    up = _mkdirs(tmp_path / "dashboard-uploads")
    target = up / "real.txt"
    target.write_text("keep me")
    link = up / "link.txt"
    link.symlink_to(target)
    files.delete_file(str(link))
    assert not link.exists() and not link.is_symlink()
    assert target.read_text() == "keep me"


def test_delete_symlink_to_outside_target_removes_link_only(tmp_path):
    """link → 白名单外目标:允许删 link 本身,目标绝不受影响。"""
    up = _mkdirs(tmp_path / "dashboard-uploads")
    outside = tmp_path / "secret.txt"
    outside.write_text("untouchable")
    link = up / "evil-link"
    link.symlink_to(outside)
    files.delete_file(str(link))
    assert not link.is_symlink()
    assert outside.read_text() == "untouchable"


def test_delete_parent_symlink_escape_rejected(tmp_path):
    """父目录 symlink 逃逸:uploads/linkdir → 白名单外,删其下文件必须拒绝。"""
    up = _mkdirs(tmp_path / "dashboard-uploads")
    outside_dir = _mkdirs(tmp_path / "outside")
    (outside_dir / "victim.txt").write_text("x")
    linkdir = up / "linkdir"
    linkdir.symlink_to(outside_dir)
    with pytest.raises(ValueError):
        files.delete_file(str(linkdir / "victim.txt"))
    assert (outside_dir / "victim.txt").is_file()


# ── 文件名搜索 ──────────────────────────────────────────────────

def test_search_files_recursively_by_name(tmp_path):
    root = _mkdirs(tmp_path / "dashboard-data")
    nested = _mkdirs(root / "nested")
    (root / "Alpha.txt").write_text("a")
    (nested / "my-alpha.py").write_text("b")
    _mkdirs(root / "alpha-dir")
    (nested / "other.txt").write_text("c")

    result = files.search_files(str(root), "ALPHA")

    assert result["path"] == str(root.resolve())
    assert result["query"] == "ALPHA"
    assert result["truncated"] is False
    assert {(r["relative"], r["type"]) for r in result["results"]} == {
        ("Alpha.txt", "file"),
        ("alpha-dir", "dir"),
        ("nested/my-alpha.py", "file"),
    }


def test_search_files_respects_limits_and_skips_internal_dirs(tmp_path):
    root = _mkdirs(tmp_path / "dashboard-data")
    for name in ("match-a.txt", "match-b.txt", "match-c.txt"):
        (root / name).write_text(name)
    hidden = _mkdirs(root / ".git")
    (hidden / "match-secret.txt").write_text("secret")
    outside = _mkdirs(tmp_path / "outside")
    (outside / "match-outside.txt").write_text("secret")
    (root / "match-link").symlink_to(outside)

    result = files.search_files(str(root), "match", limit=2)

    assert len(result["results"]) == 2
    assert result["truncated"] is True
    assert all(".git" not in r["relative"] for r in result["results"])
    assert all("match-link" not in r["relative"] for r in result["results"])
    assert files.search_files(str(root), "outside")["results"] == []


def test_search_files_rejects_invalid_scope_and_query(tmp_path):
    allowed = _mkdirs(tmp_path / "dashboard-data")
    outside = _mkdirs(tmp_path / "outside")

    with pytest.raises(ValueError, match="关键词"):
        files.search_files(str(allowed), "   ")
    with pytest.raises(ValueError, match="过长"):
        files.search_files(str(allowed), "x" * 129)
    with pytest.raises(ValueError, match="允许范围"):
        files.search_files(str(outside), "x")
    with pytest.raises(ValueError, match="范围"):
        files.search_files(str(allowed), "x", limit=0)


def test_search_endpoint_returns_authenticated_results(tmp_path, monkeypatch):
    root = _mkdirs(tmp_path / "dashboard-data")
    (root / "needle.txt").write_text("found")
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)

    response = TestClient(server.app).get(
        "/api/files/search",
        params={"path": str(root), "q": "needle"},
        headers={"authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["relative"] == "needle.txt"


def test_files_roots_hides_tmp_and_internal_worktrees(monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    monkeypatch.setattr(
        files, "allowed_root_groups",
        lambda: {
            "system": ["/home/u/app"],
            "projects": [
                "/tmp",
                "/home/u/repo",
                "/tmp/pytest-of-fyc/pytest-1/test_x0/proj",
                "/home/u/.repo-cockpit-worktrees",
                "/home/u/.repo-cockpit-worktrees/repo/1-codex",
            ],
            "custom": ["/home/u/extra"],
        },
    )
    client = TestClient(server.app)
    response = client.get(
        "/api/files/roots", headers={"authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    groups = response.json()["groups"]
    assert groups["projects"] == ["/home/u/repo"]
    assert groups["system"] == ["/home/u/app"]
    assert groups["custom"] == ["/home/u/extra"]
    assert "/tmp/pytest-of-fyc/pytest-1/test_x0/proj" not in response.json()["roots"]
    assert "/tmp" not in response.json()["roots"]
    assert "/home/u/.repo-cockpit-worktrees" not in response.json()["roots"]


# ── 媒体预览与目录打包下载 ──────────────────────────────────────

def test_preview_path_allows_media_and_rejects_svg(tmp_path):
    root = _mkdirs(tmp_path / "dashboard-uploads" / "media")
    img = root / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    svg = root / "b.svg"
    svg.write_text("<svg><script>alert(1)</script></svg>")

    assert files.preview_path(str(img)) == img.resolve()
    with pytest.raises(ValueError):
        files.preview_path(str(svg))
    with pytest.raises(ValueError):
        files.preview_path(str(root / "missing.txt"))


def test_zip_dir_skips_git_and_symlink(tmp_path):
    root = _mkdirs(tmp_path / "dashboard-uploads" / "pack")
    (root / "keep.txt").write_text("hello")
    _mkdirs(root / ".git" / "objects")
    (root / ".git" / "config").write_text("secret")
    sub = _mkdirs(root / "sub")
    (sub / "n.md").write_text("# n")
    (root / "link").symlink_to(root / "keep.txt")

    archive = files.zip_dir(str(root))
    try:
        import zipfile
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            assert f"{root.name}/keep.txt" in names
            assert f"{root.name}/sub/n.md" in names
            assert not any(".git" in n.split("/") for n in names)
            assert not any(n.endswith("/link") for n in names)
    finally:
        archive.unlink()

    with pytest.raises(ValueError):
        files.zip_dir(str(root / "keep.txt"))


def test_raw_and_download_dir_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "COCKPIT_TOKEN", "secret", raising=False)
    root = _mkdirs(tmp_path / "dashboard-uploads" / "dl")
    (root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "doc.txt").write_text("hi")
    client = TestClient(server.app)
    headers = {"authorization": "Bearer secret"}

    raw = client.get(f"/api/files/raw?path={root}/pic.png", headers=headers)
    assert raw.status_code == 200
    assert raw.content.startswith(b"\x89PNG")
    assert "attachment" not in raw.headers.get("content-disposition", "")
    assert raw.headers["x-content-type-options"] == "nosniff"
    assert raw.headers["cache-control"] == "private, no-store"

    bad = client.get(f"/api/files/raw?path={root}/doc.txt", headers=headers)
    assert bad.status_code == 400

    zipped = client.get(f"/api/files/download-dir?path={root}", headers=headers)
    assert zipped.status_code == 200
    assert zipped.headers["content-type"] == "application/zip"
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as zf:
        assert f"{root.name}/doc.txt" in zf.namelist()
