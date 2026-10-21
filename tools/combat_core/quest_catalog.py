"""Editable quest labels only; names never change measurement boundaries."""
import csv
from functools import lru_cache
from pathlib import Path
from ..paths import CONFIG, BUNDLE

QUESTS_CSV = CONFIG / 'quests.csv'


class QuestCatalog:
    def __init__(self, entries=None, error=None):
        self.entries = entries or {}
        self.error = error

    @classmethod
    def load(cls, path=QUESTS_CSV):
        if Path(path) == QUESTS_CSV and not Path(path).exists():
            path = BUNDLE / 'quests.csv'
        try:
            entries = {}
            with Path(path).open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream, strict=True)
                if not {'quest_id', 'name'} <= set(reader.fieldnames or []):
                    raise ValueError('quest_id,nameの列が必要です。')
                for row in reader:
                    value, name = row['quest_id'].strip(), row['name'].strip()
                    if value.startswith('#') or not value and not name:
                        continue
                    if not value.isascii() or not value.isdecimal() or not name:
                        raise ValueError(f'{reader.line_num}行: IDと名前を確認してください。')
                    quest = int(value)
                    if not 0 <= quest <= 0xffffffff or any(ord(c) < 32 for c in name):
                        raise ValueError(f'{reader.line_num}行: IDまたは名前が不正です。')
                    if quest in entries and entries[quest] != name:
                        raise ValueError(f'{reader.line_num}行: ID {quest}の名前が重複しています。')
                    entries[quest] = name
            return cls(entries)
        except (OSError, ValueError, AttributeError, TypeError, csv.Error) as error:
            return cls(error=str(error))

    def label(self, quest):
        return self.entries.get(quest) or f'不明クエスト（ID: {quest if quest is not None else "—"}）'


@lru_cache(maxsize=1)
def get_quest_catalog():
    return QuestCatalog.load()


def quest_label(report):
    info = report.get('quest_measurement') or {}
    return get_quest_catalog().label(info.get('quest_candidate')) if info.get('kind') == 'quest' else 'Manual'
