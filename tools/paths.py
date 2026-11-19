"""Portable writable paths; bundled resources are read-only defaults."""
from pathlib import Path
import sys

BUNDLE = Path(__file__).resolve().parents[1]
HOME = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else BUNDLE
CONFIG = HOME / 'config'
LOGS = HOME / 'logs'
HISTORY = LOGS / 'history'
MANIFEST = CONFIG / 'capture-build.json'


def copy_missing(source, target):
    if not source.is_file() or target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open('xb') as stream:
            stream.write(source.read_bytes())
    except FileExistsError:
        pass


def initialize():
    """Seed only absent files. Existing edits and original recordings survive."""
    CONFIG.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    # Windows opens either spelling, but retains the existing directory entry.
    legacy_quests = next((p for p in CONFIG.iterdir() if p.name == 'Quests.csv'), None)
    if legacy_quests is not None and legacy_quests.resolve() == (CONFIG / 'quests.csv').resolve():
        temporary = CONFIG / '.quests-case-rename'
        if not temporary.exists():
            legacy_quests.rename(temporary)
            try:
                temporary.rename(CONFIG / 'quests.csv')
            except OSError:
                temporary.rename(legacy_quests)
                raise
    seeds = {
        'quests.csv': BUNDLE / 'quests.csv',
        'skills.csv': BUNDLE / 'tools/attack_catalog/skills.csv',
        'skills_exclusions.csv': BUNDLE / 'tools/attack_catalog/skills_exclusions.csv',
        'ja_exclusions.csv': BUNDLE / 'tools/combat_core/data/ja_exclusions.csv',
        'preferences.json': BUNDLE / 'tools/gui/state/preferences.json',
    }
    legacy_ja = BUNDLE / 'tools/combat_core/ja_exclusions.local.csv'
    if legacy_ja.is_file():
        seeds['ja_exclusions.csv'] = legacy_ja
    for name, source in seeds.items():
        copy_missing(source, CONFIG / name)
    if not (CONFIG / 'preferences.json').exists():
        (CONFIG / 'preferences.json').write_text('{"schema_version": 1}\n', encoding='utf-8')
    # Windows preserves the old spelling even when opened as "logs". A
    # two-step rename changes only directory spelling, retaining all contents.
    old_logs = next((p for p in HOME.iterdir() if p.name == 'Logs' and p.is_dir()), None)
    if old_logs is not None and old_logs.resolve() == LOGS.resolve():
        temporary = HOME / '.lazyparse-logs-rename'
        if not temporary.exists():
            old_logs.rename(temporary)
            try:
                temporary.rename(LOGS)
            except OSError:
                temporary.rename(old_logs)
                raise
    for source in (BUNDLE / 'tools/gui/state/logs').glob('*.json'):
        copy_missing(source, HISTORY / source.name)
    for source in (BUNDLE / 'licenses').rglob('*'):
        if source.is_file():
            copy_missing(source, CONFIG / 'licenses' / source.relative_to(BUNDLE / 'licenses'))

