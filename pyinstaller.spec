# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    'pyvisa-py',
    'pyvisa_py',
    'pyvisa',
    'pyvisa.ctwrapper',
    'pyvisa.ctwrapper.functions',
    'pyvisa.ctwrapper.highlevel',
    'pyvisa.ctwrapper.types',
    'pyvisa.resources',
    'pyvisa.resources.serial',
    'pyvisa.resources.usb',
    'pyvisa.resources.gpib',
    'pyvisa.resources.tcpip',
]

collect_all_libs = [
  'pyvisa',
  'pyvisa_py',
  'pyvisa-py',
  'tdr_plots',
  'numpy',
  'serial',
  'tkinter',
  'PIL',
  'matplotlib',
  'mplcursors',
  'matplotlib.backends.backend_tkagg'
]

for lib in collect_all_libs:
  tmp_ret = collect_all(lib)
  datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    ['bin/cli.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='monitor_tdr',
    debug=False,
    docs_from_dispatcher=False,
    bootloader_ignore_signals=False,
    strip=False,
    #strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
