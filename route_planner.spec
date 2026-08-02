# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — works for both Mac and Windows.
# Build with: pyinstaller route_planner.spec

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('templates', 'templates'),   # Flask HTML templates
        ('EULA.txt', '.'),            # Licence agreement
        ('license_keys.py', '.'),     # Embedded key hashes
        ('credentials.json', '.'),    # Bundled Google OAuth credentials
    ],
    hiddenimports=[
        'googleapiclient',
        'googleapiclient.discovery',
        'googleapiclient.http',
        'google.auth',
        'google.auth.transport.requests',
        'google_auth_oauthlib',
        'google_auth_oauthlib.flow',
        'google.oauth2',
        'google.oauth2.credentials',
        'flask',
        'jinja2',
        'werkzeug',
        'googlemaps',
        'docx',
        'docx.oxml',
        'docx.oxml.ns',
        'docx.shared',
        'docx.enum.text',
        'lxml',
        'lxml.etree',
        'pkg_resources.py2_warn',
        'reportlab',
        'reportlab.platypus',
        'reportlab.lib.pagesizes',
        'reportlab.lib.styles',
        'reportlab.lib.units',
        'reportlab.lib.colors',
        'reportlab.lib.enums',
        'reportlab.graphics',
        'reportlab.pdfbase',
        'reportlab.pdfbase.ttfonts',
        'reportlab.pdfbase.pdfmetrics',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PyQt5', 'PySide6', 'PyQt6', 'PySide2',
        'tkinter', '_tkinter',
        'matplotlib', 'numpy', 'pandas', 'scipy',
        'IPython', 'jupyter', 'notebook', 'nbformat',
        'sphinx', 'docutils', 'pytest', 'black',
        'zmq', 'psutil',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Route Planner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no terminal window — set to True to show logs
    icon=None,              # replace with 'icon.icns' (Mac) or 'icon.ico' (Windows)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Route Planner',
)

# Mac-only: wrap in a .app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Route Planner.app',
        icon=None,              # replace with path to 'icon.icns'
        bundle_identifier='com.routeplanner.app',
        info_plist={
            'NSHighResolutionCapable': True,
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            'LSUIElement': False,
        },
    )
