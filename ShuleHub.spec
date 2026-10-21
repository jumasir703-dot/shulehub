# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for ShuleHub — offline desktop build.
#
# Build with:  pyinstaller --clean ShuleHub.spec
# Run this on EACH target OS separately (Windows -> .exe, macOS -> .app/binary,
# Linux -> ELF binary). PyInstaller cannot cross-compile.

from PyInstaller.utils.hooks import collect_all

# Excel/PDF import: pdfminer ships data files and pypdfium2 a native library
_extra_datas, _extra_bins, _extra_hidden = [], [], []
for _pkg in ('pdfplumber', 'pdfminer', 'pypdfium2', 'openpyxl', 'PIL'):
    _d, _b, _h = collect_all(_pkg)
    _extra_datas += _d; _extra_bins += _b; _extra_hidden += _h

a = Analysis(
    ['run_desktop.py'],
    pathex=[],
    binaries=_extra_bins,
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ] + _extra_datas,
    hiddenimports=[
        'reportlab.graphics.barcode.common',
        'import_files', 'branding', 'permissions', 'exam_flow', 'exam_results', 'demo', 'demo_data',
    ] + _extra_hidden,
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
    icon='assets/shulehub.ico',   # the exe's own icon (replaces the default Python one)
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # no terminal window; errors go to app.log instead (see run_desktop.py)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    onefile=True,
)
