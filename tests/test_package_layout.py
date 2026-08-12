from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROOT_PYTHON_ENTRYPOINTS = {
    "cockpit-upgrade-worker.py",
    "release_lane.py",
    "server.py",
    "source_native_migrate.py",
}


def test_python_implementations_live_in_application_package() -> None:
    assert {path.name for path in ROOT.glob("*.py")} == ROOT_PYTHON_ENTRYPOINTS
    assert (ROOT / "agent_cockpit" / "__init__.py").is_file()
