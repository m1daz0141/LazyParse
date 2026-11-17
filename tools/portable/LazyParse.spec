from pathlib import Path
import importlib.metadata
import sys

root = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(root))
from tools.version import VERSION
from PyInstaller.utils.win32.versioninfo import VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct
version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=tuple(map(int, VERSION.split('.'))) + (0,),
                      prodvers=tuple(map(int, VERSION.split('.'))) + (0,),
                      mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('041104B0', [
        StringStruct('ProductName', 'LazyParse'), StringStruct('FileDescription', 'LazyParse'),
        StringStruct('FileVersion', VERSION), StringStruct('ProductVersion', VERSION),
        StringStruct('OriginalFilename', 'LazyParse.exe')])]),
        VarFileInfo([VarStruct('Translation', [0x411, 1200])])])
resources = [
    ('tools/gui/assets/icon.png', 'tools/gui/assets'),
    ('quests.csv', '.'),
    ('tools/attack_catalog/skills.csv', 'tools/attack_catalog'),
    ('tools/attack_catalog/data/skills_ja.csv', 'tools/attack_catalog/data'),
    ('tools/attack_catalog/data/source.json', 'tools/attack_catalog/data'),
    ('tools/combat_core/data/ja_exclusions.csv', 'tools/combat_core/data'),
    ('tools/portable/bin/build.json', 'tools/portable/bin'),
    ('tools/portable/bin/Capture.dll', 'tools/portable/bin'),
    ('tools/portable/bin/Loader.dll', 'tools/portable/bin'),
    ('License', 'licenses/LazyParse'),
    ('COPYRIGHT', 'licenses/LazyParse'),
    ('THIRD_PARTY_NOTICES', 'licenses'),
]
if (root / 'tools/combat_core/ja_exclusions.local.csv').is_file():
    resources.append(('tools/combat_core/ja_exclusions.local.csv', 'tools/combat_core'))
resources.append((str(Path(sys.base_prefix) / 'LICENSE.txt'), 'licenses/Python'))
for license_file in (Path(sys.base_prefix) / 'tcl').glob('*/license.terms'):
    resources.append((str(license_file), 'licenses/' + license_file.parent.name))
distribution = importlib.metadata.distribution('pyinstaller')
for file in distribution.files:
    if str(file).replace('\\', '/').endswith('.dist-info/licenses/COPYING.txt'):
        resources.append((str(distribution.locate_file(file)), 'licenses/PyInstaller'))

distribution = importlib.metadata.distribution('Pillow')
for file in distribution.files:
    if '.dist-info/licenses/' in str(file).replace('\\', '/'):
        resources.append((str(distribution.locate_file(file)), 'licenses/Pillow'))
a = Analysis([str(root / 'tools/portable/entry.py')], pathex=[str(root)],
    binaries=[], datas=[(str(root / source), destination) for source, destination in resources],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['tools.gui.dev_smoke', 'tools.gui.dev_capture'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='LazyParse', version=version,
    icon=str(root / 'tools/gui/assets/icon.ico'), debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None)
