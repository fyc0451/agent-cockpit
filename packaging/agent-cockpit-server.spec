from pathlib import Path


project_root = Path(SPECPATH).resolve().parent

analysis = Analysis(
    [str(project_root / "server.py")],
    pathex=[str(project_root), str(project_root / "agent-mail-tools")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "agent_mail_commands.am_init_project",
        "agent_mail_commands.am_register",
        "agent_mail_commands.am_retire",
        "agent_mail_commands.mail_identity_inject",
        "agent_mail_commands.mail_recv",
        "agent_mail_commands.mail_send",
        "agent_mail_commands.task_report",
        "agent_cockpit.maintenance_cli",
        "uvicorn.lifespan.on",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="agent-cockpit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="agent-cockpit",
    contents_directory="_internal",
)
