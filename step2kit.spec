# -*- mode: python ; coding: utf-8 -*-
import sysconfig
from PyInstaller.utils.hooks import collect_all

# resolved at build time so this spec is not tied to any one machine's Python install path
_site_packages = sysconfig.get_paths()["purelib"]
datas = [(f"{_site_packages}/cadquery_ocp_novtk.libs", "cadquery_ocp_novtk.libs")]
binaries = []
hiddenimports = []
tmp_ret = collect_all('build123d')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('lib3mf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['app_main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['jedi', 'torch', 'torchvision', 'numba', 'llvmlite', 'pandas', 'notebook', 'jupyter', 'jupyter_client', 'jupyter_core', 'ipykernel', 'tensorboard', 'IPython.terminal', 'IPython.core.completer'],
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
    name='Slotify',
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
    icon=['app_icon.ico'],
)
