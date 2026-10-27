# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from pathlib import Path

spec_root = Path(SPECPATH).resolve()
datas = [(str(spec_root / folder), folder) for folder in ("scripts", "renderer", "prompts", "config")]
mac_ocr = spec_root / ".cache/mac-ocr"
if mac_ocr.is_file():
    datas.append((str(mac_ocr), ".cache"))

hiddenimports = []
hiddenimports += collect_submodules('encodings')
hiddenimports += ['background_worker', 'native_app']


a = Analysis(
    [str(spec_root / 'native.py')],
    pathex=[str(spec_root / 'scripts')],
    binaries=[],
    datas=datas,
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
    icon=[str(spec_root / 'SchemiEsami.icns')],
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
    name='Schemi Esami.app',
    icon=str(spec_root / 'SchemiEsami.icns'),
    bundle_identifier=None,
)
