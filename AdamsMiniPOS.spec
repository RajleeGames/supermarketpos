# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\user\\worldlink-projects\\OnlineRetailPOS\\db.sqlite3', '.'), ('C:\\Users\\user\\worldlink-projects\\OnlineRetailPOS\\onlineretailpos\\static', 'onlineretailpos/static'), ('C:\\Users\\user\\worldlink-projects\\OnlineRetailPOS\\onlineretailpos\\templates', 'onlineretailpos/templates')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AdamsMiniPOS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
