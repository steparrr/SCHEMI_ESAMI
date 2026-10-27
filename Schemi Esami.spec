# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('encodings')
hiddenimports += ['background_worker', 'native_app']


a = Analysis(
    ['/Users/Administrator/Documents/Codex/2026-10-12/nel/work/schemi-esami-rescue/native.py'],
    pathex=['/Users/Administrator/Documents/Codex/2026-10-12/nel/work/schemi-esami-rescue/scripts'],
    binaries=[],
    datas=[('/Users/Administrator/Documents/Codex/2026-10-12/nel/work/schemi-esami-rescue/scripts', 'scripts'), ('/Users/Administrator/Documents/Codex/2026-10-12/nel/work/schemi-esami-rescue/.cache/mac-ocr', '.cache'), ('/Users/Administrator/Documents/Codex/2026-10-12/nel/work/schemi-esami-rescue/renderer', 'renderer'), ('/Users/Administrator/Documents/Codex/2026-10-12/nel/work/schemi-esami-rescue/prompts', 'prompts'), ('/Users/Administrator/Documents/Codex/2026-10-12/nel/work/schemi-esami-rescue/config', 'config')],
    hiddenimports=hiddenimports,
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
    name='Schemi Esami',
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
    icon=['/Users/Administrator/Documents/Codex/2026-10-12/nel/work/schemi-esami-rescue/SchemiEsami.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Schemi Esami',
)
app = BUNDLE(
    coll,
    name='Schemi Esami 2026.10.3.app',
    icon='/Users/Administrator/Documents/Codex/2026-10-12/nel/work/schemi-esami-rescue/SchemiEsami.icns',
    bundle_identifier=None,
)
