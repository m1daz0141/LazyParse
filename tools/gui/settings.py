from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re

from .viewmodel import DEFAULT_COLUMNS, COLUMNS
from .timing import DPS_MODES

STATE = Path(__file__).resolve().parent / 'state'


@dataclass
class Settings:
    geometry: str = '670x275+100+100'
    topmost: bool = True
    opacity: float = 1.0
    players_only: bool = True
    only_me: bool = False
    anonymize: bool = False
    bars: bool = True
    highlight_self: bool = True
    auto_quests: bool = True
    dps_mode: str = 'quest'
    time_decimals: bool = True
    font_size: int = 9
    interval: float = 0.5
    columns: list = field(default_factory=lambda: DEFAULT_COLUMNS.copy())

    @classmethod
    def load(cls, path):
        result = cls()
        try:
            values = json.loads(path.read_text(encoding='utf-8'))
            if values.get('schema_version') != 1:
                return result
            for name in ('topmost', 'players_only', 'only_me', 'anonymize', 'bars', 'highlight_self', 'auto_quests', 'time_decimals'):
                if type(values.get(name)) is bool:
                    setattr(result, name, values[name])
            if values.get('dps_mode') in DPS_MODES:
                result.dps_mode = values['dps_mode']
            if re.fullmatch(r'[0-9]{3,4}x[0-9]{3,4}[+-][0-9]{1,5}[+-][0-9]{1,5}', str(values.get('geometry', ''))):
                result.geometry = values['geometry']
            for name, low, high in [('opacity', 0.3, 1), ('font_size', 8, 16), ('interval', 0.1, 3)]:
                value = values.get(name)
                if type(value) in (int, float) and low <= value <= high:
                    setattr(result, name, int(value) if name == 'font_size' else float(value))
            columns = values.get('columns')
            allowed = {key for key, _, _ in COLUMNS}
            if isinstance(columns, list) and all(isinstance(key, str) for key in columns):
                result.columns = list(dict.fromkeys(['name'] + [key for key in columns if key in allowed]))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return result

    def save(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'schema_version': 1, **asdict(self)}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
