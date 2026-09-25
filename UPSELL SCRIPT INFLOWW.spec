# -*- mode: python ; coding: utf-8 -*-
#
# onedir, not onefile, and deliberately so.
#
# A onefile build unpacks the whole app into %TEMP%\_MEIxxxxxx on every
# launch and deletes it again on exit. That cleanup kept failing here -
# WebView2 spawns child processes that keep DLLs inside _MEI mapped, and
# they outlive the host - which pops a "Failed to remove temporary
# directory" warning dialog and leaves the folder behind. 206 of them had
# accumulated, 22-59 MB each. The startup watchdog's os._exit() makes it
# fail every time, by design: you cannot unwind cleanly from a deadlocked
# UI thread.
#
# onedir has no extraction step at all, so there is nothing to clean up
# and nothing to warn about - and it starts faster. The app is installed
# into its own folder anyway, so a folder of files costs nothing here.

a = Analysis(
    ['UPSELL_SCRIPT_INFLOWW.py'],
    pathex=[],
    binaries=[],
    datas=[('gui.html', '.')],
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
    name='UPSELL SCRIPT INFLOWW',
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
    name='UPSELL SCRIPT INFLOWW',
)
