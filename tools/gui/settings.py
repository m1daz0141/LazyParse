from dataclasses import asdict, dataclass, field
import json
import re

from .viewmodel import DEFAULT_COLUMNS, COLUMNS
from .timing import DPS_MODES
from ..paths import CONFIG
from .appearance import DEFAULT_BACKGROUND, DEFAULT_ACCENT, valid_color, BACKGROUND_NAME

STATE = CONFIG
COLUMN_WIDTH_RANGE = (1, 5000)


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
    time_decimals: bool = False
    auto_text_export: bool = True
    game_directory: str = ''
    theme_background: str = DEFAULT_BACKGROUND
    theme_accent: str = DEFAULT_ACCENT
    background_image: str = ''
    font_size: int = 9
    interval: float = 0.5
    columns: list = field(default_factory=lambda: DEFAULT_COLUMNS.copy())
    # Relative widths of resized columns; others use the built-in COLUMNS widths.
    column_widths: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path):
        result = cls()
        try:
            values = json.loads(path.read_text(encoding='utf-8'))
            if values.get('schema_version') != 1:
                return result
            for name in ('topmost', 'players_only', 'only_me', 'anonymize', 'bars', 'highlight_self', 'auto_quests', 'time_decimals', 'auto_text_export'):
                if type(values.get(name)) is bool:
                    setattr(result, name, values[name])
            if values.get('dps_mode') in DPS_MODES:
                result.dps_mode = values['dps_mode']
            if isinstance(values.get('game_directory'), str):
                result.game_directory = values['game_directory']
            for name in ('theme_background', 'theme_accent'):
                if valid_color(values.get(name)):
                    setattr(result, name, values[name].lower())
            name = values.get('background_image')
            if isinstance(name, str) and (not name or BACKGROUND_NAME.fullmatch(name)):
                result.background_image = name
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
            widths = values.get('column_widths')
            if isinstance(widths, dict):
                result.column_widths = {key: float(value) for key, value in widths.items()
                    if key in allowed and type(value) in (int, float) and COLUMN_WIDTH_RANGE[0] <= value <= COLUMN_WIDTH_RANGE[1]}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return result

    def save(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'schema_version': 1, **asdict(self)}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
