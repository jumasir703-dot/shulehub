# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for ShuleHub - offline desktop build (used by build_windows.bat).
#
# Build with build_windows.bat. Output: dist\ShuleHub.exe
#
# IMPORTANT: app.py loads some modules by name at start-up (library, messaging,
# search, dashboard, school_theme ...). PyInstaller cannot see imports made that
# way, so a plain build silently leaves those features out. This spec therefore
# lists EVERY .py file in the project folder as a hidden import, so any module you
# add later is included automatically with no changes needed here.

import glob
import os

from PyInstaller.utils.hooks import collect_all

_HERE = os.path.abspath(SPECPATH)

# Developer-only helpers and scripts that must never be shipped inside the app
# (generate_license.py in particular creates licences).
_SKIP_PREFIXES = ('apply_', 'check_', 'build_', 'generate_', 'test_', 'conftest', 'setup', 'run_')
_project_modules = sorted(
    os.path.splitext(os.path.basename(p))[0]
    for p in glob.glob(os.path.join(_HERE, '*.py'))
    if not os.path.basename(p).startswith(_SKIP_PREFIXES)
)

# Excel/PDF import: pdfminer ships data files and pypdfium2 a native library
_extra_datas, _extra_bins, _extra_hidden = [], [], []
for _pkg in ('pdfplumber', 'pdfminer', 'pypdfium2', 'openpyxl', 'PIL'):
    _d, _b, _h = collect_all(_pkg)
    _extra_datas += _d; _extra_bins += _b; _extra_hidden += _h

# data folders: templates and static always; the others only if they exist
_datas = [('templates', 'templates'), ('static', 'static')]
for _folder in ('migrations',):
    if os.path.isdir(os.path.join(_HERE, _folder)):
        _datas.append((_folder, _folder))

_icon = os.path.join(_HERE, 'static', 'favicon.ico')

a = Analysis(
    ['run_desktop.py'],
    pathex=[_HERE],
    binaries=_extra_bins,
    datas=_datas + _extra_datas,
    hiddenimports=['reportlab.graphics.barcode.common'] + _project_modules + _extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['psycopg2', 'psycopg2-binary'],  # not needed offline (SQLite only)
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
    name='ShuleHub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # no terminal window; errors go to app.log instead (see run_desktop.py)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon if os.path.exists(_icon) else None,
    onefile=True,
)
