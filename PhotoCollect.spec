# -*- mode: python ; coding: utf-8 -*-

from importlib.util import find_spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules


rapidocr_root = Path(find_spec('rapidocr_onnxruntime').origin).parent
rapidocr_hiddenimports = collect_submodules('rapidocr_onnxruntime')
rapidocr_datas = [
    (str(rapidocr_root / 'config.yaml'), 'rapidocr_onnxruntime'),
    (str(rapidocr_root / 'models'), 'rapidocr_onnxruntime/models'),
    (str(rapidocr_root / 'ch_ppocr_v2_cls'), 'rapidocr_onnxruntime/ch_ppocr_v2_cls'),
    (str(rapidocr_root / 'ch_ppocr_v3_det'), 'rapidocr_onnxruntime/ch_ppocr_v3_det'),
    (str(rapidocr_root / 'ch_ppocr_v3_rec'), 'rapidocr_onnxruntime/ch_ppocr_v3_rec'),
]

a = Analysis(
    ['con.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets\\app-logo.png', 'assets'),
        ('assets\\app-logo.ico', 'assets'),
        ('tests\\fixtures\\container_sample.jpg', 'tests\\fixtures'),
        *rapidocr_datas,
    ],
    hiddenimports=[*rapidocr_hiddenimports, 'numpy._core._exceptions'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'pandas', 'scipy', 'sqlalchemy', 'matplotlib', 'IPython', 'notebook'],
    noarchive=False,
    optimize=0,
)

# Qt 6 on Windows links against the OS ICU shim (icuuc.dll). A foreign ICU on
# the build machine's PATH, such as Poppler's ICU runtime, can otherwise be
# collected into the bundle and shadow the compatible Windows system DLL.
def is_foreign_icu_binary(entry):
    name = Path(entry[0]).name.lower()
    return name == 'icuuc.dll' or (name.startswith('icudt') and name.endswith('.dll'))


a.binaries = [entry for entry in a.binaries if not is_foreign_icu_binary(entry)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PhotoCollect',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\app-logo.ico'],
)
