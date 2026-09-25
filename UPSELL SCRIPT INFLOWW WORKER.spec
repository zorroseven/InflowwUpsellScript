# -*- mode: python ; coding: utf-8 -*-
#
# onedir, for the reasons in the GUI spec plus one that matters more
# here: this executable is launched once per model. As a onefile build
# it unpacked ~26 MB into a fresh %TEMP%\_MEIxxxxxx every single time -
# 40 extractions for a 40-model run, each one paid for in startup time
# and each one a chance to leave another orphaned folder behind. onedir
# unpacks nothing.
#
# It lives in its own subfolder (installed as <app>\worker\) because two
# onedir builds cannot share a directory - both would want _internal.

a = Analysis(
    ['upsell_worker.py'],
    pathex=[],
    binaries=[],
    datas=[],
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
    [],
    exclude_binaries=True,
    name='UPSELL SCRIPT INFLOWW WORKER',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='UPSELL SCRIPT INFLOWW WORKER',
)
