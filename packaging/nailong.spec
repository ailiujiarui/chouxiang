# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Nailong desktop pet.

Build with::

    .venv\\Scripts\\pyinstaller packaging/nailong.spec --noconfirm

Output: ``dist/Nailong/Nailong.exe`` (one directory; move the whole folder).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH)
REPO_ROOT = SPEC_DIR.parent
SRC = REPO_ROOT / "src"

hiddenimports: list[str] = []
for package in (
    "langgraph",
    "langchain_core",
    "langchain_protocol",
    "langsmith",
    "orjson",
    "pydantic",
    "httpx",
    "anyio",
):
    hiddenimports += collect_submodules(package)

analysis = Analysis(
    [str(SPEC_DIR / "nailong_desktop.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "streamlit",
        "typer",
        "rich",
        "radon",
        "hypothesis",
        "docker",
        "git",
        "GitPython",
        "uvicorn",
        "fastapi",
        "starlette",
        "sqlalchemy",
        "litellm",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Nailong",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="Nailong",
)
