# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Force Django settings module for the analysis and runtime
os.environ["DJANGO_SETTINGS_MODULE"] = "onlineretailpos.settings.settings"

hiddenimports = []
hiddenimports += collect_submodules("django")
hiddenimports += collect_submodules("inventory")
hiddenimports += collect_submodules("transaction")
hiddenimports += collect_submodules("cart")
hiddenimports += ["onlineretailpos.urls", "onlineretailpos.wsgi"]

datas = []
# Include your whole project package
datas += [("onlineretailpos", "onlineretailpos")]
# Include templates/static if they live outside app dirs
# (Keep these even if empty; won't harm)
datas += [("templates", "templates")] if os.path.isdir("templates") else []
datas += [("static", "static")] if os.path.isdir("static") else []

# Include django data files
datas += collect_data_files("django")

a = Analysis(
    ["run_app.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AdamsMiniPOS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # no console window
)
