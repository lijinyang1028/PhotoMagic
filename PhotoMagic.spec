# -*- mode: python ; coding: utf-8 -*-
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

keyring_datas = collect_data_files('keyring')
keyring_hiddenimports = collect_submodules('keyring')

# 钥匙串后端按平台选择，避免在其他系统上引用不存在的模块
if sys.platform == 'win32':
    keyring_backends = ['keyring.backends.Windows']
elif sys.platform == 'darwin':
    keyring_backends = ['keyring.backends.macOS']
else:
    keyring_backends = ['keyring.backends.SecretService',
                        'keyring.backends.libsecret']

# icon / version 是 Windows 专用资源，其他平台不传
icon_files = ['icon.ico'] if sys.platform == 'win32' else None
version_file = 'version_info.txt' if sys.platform == 'win32' else None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('params_schema.json', '.')] + keyring_datas,
    hiddenimports=keyring_backends + keyring_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PhotoMagic',
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
    icon=icon_files,
    version=version_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PhotoMagic',
)