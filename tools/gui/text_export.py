"""Human-readable local exports. No capture control or original-log mutations."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from statistics import pstdev
import uuid

from ..combat_core.quest_catalog import quest_label
from .timing import with_dps_mode
from .viewmodel import actor_rows, attack_rows, display_elapsed, number, percentage
from ..paths import LOGS

EXPORT_DIRECTORY = LOGS
JST = timezone(timedelta(hours=9))


def measurement_date(report):
    spans = report.get('timing_modes', {}).get('quest', report.get('timing', {})).get('spans', [])
    filetime = next((s.get('first_damage_filetime') for s in spans if s.get('first_damage_filetime')), None)
    filetime = filetime or report.get('recording_time', {}).get('first_event_filetime')
    try:
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(filetime) // 10) if filetime else None
    except (ValueError, TypeError, OverflowError):
        return None


def clean_line(value):
    return ''.join(c if ord(c) >= 32 else ' ' for c in str(value))


def filename(report, *, now=None):
    stamp = (measurement_date(report) or now or datetime.now(JST)).astimezone(JST)
    label = quest_label(report)
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', label).rstrip(' .')[:90] or 'Unknown'
    kind = f'({label})' if (report.get('quest_measurement') or {}).get('kind') == 'quest' else 'Manual'
    return f'LazyParse_{kind}_{stamp:%Y-%m-%d_%H-%M-%S}.txt'


def render_text(report, *, dps_mode='quest', anonymize=False, time_decimals=True):
    report = with_dps_mode(report, dps_mode)
    rows = actor_rows(report, players_only=True, anonymize=anonymize)
    elapsed = display_elapsed(report)
    if elapsed is None:
        duration = '—'
    elif time_decimals:
        ticks = round(elapsed * 100)
        duration = f'{ticks // 6000:02d}:{ticks % 6000 / 100:05.2f}'
    else:
        duration = f'{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}'
    date = measurement_date(report)
    date_text = date.astimezone(JST).strftime('%Y年%m月%d日 %H:%M:%S JST') if date else '計測日時不明'
    total = sum(row['damage_dealt'] for row in rows)
    lines = [f'{date_text} | {duration} | TotalDamage : {total:,} | TotalDPS : {number(total / elapsed if elapsed else None, 1)}'
             f" | 標準偏差 : {number(pstdev(row['damage_dealt'] for row in rows) if rows else None, 2)}",
             f"Quest : {quest_label(report)} | DPS方式 : {'クエスト中' if dps_mode == 'quest' else 'ダメージ計測'}", '']
    for row in rows:
        lines.append(f"{clean_line(row['name'])} | {row['share']:.2f}% | {row['damage_dealt']:,} dmg | {row['damage_taken']:,} dmgd"
            f" | {number(row['dps'], 1)} DPS | JA: {percentage(row['ja'])} | Critical: {percentage(row['crit'])}"
            f" | Max: {row['max_hit']:,} ({clean_line(row['attack'])})")
    for row in rows:
        lines.extend(['', '', f"[ {clean_line(row['name'])} - {row['share']:.2f}% - {row['damage_dealt']:,} dmg ]", ''])
        for attack in attack_rows(report, row):
            share = attack['damage_dealt'] * 100 / row['damage_dealt'] if row['damage_dealt'] else 0
            average = attack['damage_dealt'] / attack['hits_dealt'] if attack['hits_dealt'] else None
            label = attack['attack_name'] or f"不明 (0x{attack['attack_id']:08X})"
            lines.extend([f"{share:05.2f}%\t| {clean_line(label)} - {attack['damage_dealt']:,} dmg"
                f" - JA : {percentage(attack['ja'])} - Critical : {percentage(attack['crit'])}",
                f"\t|   {attack['hits_dealt']:,} hits - {number(attack['min_hit'])} min, {number(average)} avg, {attack['max_hit']:,} max"
                f" | ID: 0x{attack['attack_id']:08X}"])
    if not rows:
        lines.append('プレイヤーに帰属したダメージはありません。')
    unknown = sum(a['damage_dealt'] for a in report.get('actors', []) if a['kind'] not in {'player', 'enemy'})
    if unknown:
        lines.extend(['', f'プレイヤーへの帰属未確定 : {unknown:,} dmg（上記TotalDamageには含めません）'])
    info = report.get('quest_measurement') or {}
    instances = sorted({a['map_id'] for a in report.get('actors', []) if a.get('map_id')})
    lines.extend(['', f"Quest ID: {info.get('quest_candidate', '—')} | World ID: {info.get('world_candidate', '—')}",
                  'Instance IDs: ' + (', '.join(map(str, instances)) or '—')])
    flags = report.get('quality', {}).get('flags', [])
    if flags:
        lines.append('Quality: ' + ', '.join(flags))
    return '\n'.join(lines) + '\n'


def _publish(path, text, *, replace=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8-sig', newline='\r\n') as stream:
            stream.write(text)
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_text(report, *, directory=EXPORT_DIRECTORY, path=None, **options):
    text = render_text(report, **options)
    if path is not None:
        path = Path(path)
        _publish(path, text)
        return path
    path = Path(directory) / filename(report)
    for index in range(10000):
        target = path if index == 0 else path.with_stem(path.stem + f'_{index + 1}')
        try:
            _publish(target, text)
            return target
        except FileExistsError:
            continue
    raise OSError('同じ日時のファイルが多すぎます。別の保存先を指定してください。')


class AutoTextExporter:
    """Watch from enable time; export closed measurements, update late arrivals.

    The index contains only local keys and owned-file hashes. A user-edited file
    is never replaced. Old completed history is not dumped on enable/restart.
    """
    def __init__(self, directory=EXPORT_DIRECTORY):
        self.directory = Path(directory)
        self.index_path = self.directory / '.auto-text-index.json'
        self.enabled = False
        self.watched = set()
        self.index = {}
        self.dirty = False
        try:
            data = json.loads(self.index_path.read_text(encoding='utf-8-sig'))
            if data.get('schema_version') == 1 and isinstance(data.get('files'), dict):
                self.index = data['files']
        except (OSError, ValueError, AttributeError):
            pass

    def update(self, entries, *, enabled=False, **options):
        if not enabled:
            self.enabled = False
            self.watched.clear()
            return []
        # entries: key -> (report, closed). Outside-quest intervals are omitted.
        if not self.enabled:
            self.enabled = True
            self.watched = {key for key, (_, closed) in entries.items() if not closed}
            return []
        paths = []
        for key, (report, closed) in entries.items():
            if not closed:
                self.watched.add(key)
                continue
            if key not in self.watched or not report.get('counts', {}).get('records'):
                continue
            token = hashlib.sha256(key.encode('utf-8')).hexdigest()
            previous = self.index.get(token, {})
            if not isinstance(previous, dict):
                previous = {}
            # Later menu changes apply to future exports. Late data keeps the
            # DPS/anonymization settings chosen when this measurement closed.
            selected_options = previous.get('options', options)
            if (not isinstance(selected_options, dict) or
                    set(selected_options) - {'dps_mode', 'anonymize', 'time_decimals'}):
                selected_options = options
            text = render_text(report, **selected_options)
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            name = previous.get('name', '')
            target = self.directory / name
            owned = bool(name and Path(name).name == name and name.endswith('.txt') and target.is_file()
                         and not target.is_symlink())
            if owned:
                content = target.read_text(encoding='utf-8-sig')
                owned = hashlib.sha256(content.encode('utf-8')).hexdigest() == previous.get('sha256')
            if owned and previous.get('sha256') == digest:
                continue
            if owned:
                _publish(target, text, replace=True)
            else:
                target = save_text(report, directory=self.directory, **selected_options)
            self.index[token] = {'name': target.name, 'sha256': digest, 'options': dict(selected_options)}
            self.dirty = True
            paths.append(target)
        if self.dirty:
            # UTF-8 BOM is accepted by the index reader too.
            _publish(self.index_path, json.dumps({'schema_version': 1, 'files': self.index}, ensure_ascii=False, indent=2) + '\n', replace=True)
            self.dirty = False
        return paths
