"""Single worker owns polling and control; the UI consumes detached snapshots."""
from datetime import datetime
import json
import hashlib
import os
from pathlib import Path
import queue
import threading
import time
import uuid

from ..combat_core.live import RuntimeMonitor
from ..combat_core.__main__ import replay
from ..combat_core.quest_replay import replay_quests
from ..attack_catalog.catalog import with_attack_names
from ..combat_core.quest_catalog import quest_label
from .text_export import AutoTextExporter, save_text
from .control import RuntimeController
from ..paths import MANIFEST
from ..portable.deployment import GameVersionMismatch

DEFAULT_MANIFEST = MANIFEST


def history_key(report, fallback=None):
    info = (report or {}).get('quest_measurement') or {}
    if info.get('kind') == 'quest' and all(k in info for k in
            ('capture_id', 'world_candidate', 'quest_candidate', 'start_qpc')):
        # Inserting an after-clear interval changes the old ordinal-based ID.
        # Match the actual attempt instead, without rewriting old archives.
        return ('quest', info['capture_id'], info['world_candidate'], info['quest_candidate'], info['start_qpc'])
    return ('interval', info['id']) if info.get('id') else ('file', str(fallback))


def stored_history(directory):
    """Latest immutable JSON revision per quest, including previous GUI runs."""
    latest = {}
    if directory is None:
        return []
    for path in Path(directory).glob('*.json'):
        try:
            report = saved_snapshot(path)['measurement']['report']
            info = report.get('quest_measurement') or {}
            key = history_key(report, path)
            rank = (info.get('rule_version', 0), bool(info.get('stream_finalized')), report.get('counts', {}).get('records', 0),
                    info.get('result_qpc') or 0, path.stat().st_mtime_ns)
            entry = {'run_name': quest_label(report) + (f" #{info['number']}" if info.get('number') else ''),
                'segment_number': '自動' if info else '手動', 'capture_path': str(path),
                'complete': True, 'roles': report.get('roles', {}), 'report': report,
                'display_state': '保存済み', 'history_key': key}
            if key not in latest or rank > latest[key][0]:
                latest[key] = (rank, entry)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return [value[1] for value in sorted(latest.values(), key=lambda value: value[0][-1], reverse=True)]


def saved_snapshot(capture, *, auto_quests=False):
    capture = Path(capture)
    quest_view = None
    if capture.suffix.casefold() == '.json':
        report = json.loads(capture.read_text(encoding='utf-8-sig'))
        if (not isinstance(report, dict) or report.get('schema_version') != 1 or
                not isinstance(report.get('actors'), list) or not isinstance(report.get('timing'), dict) or
                not isinstance(report.get('counts'), dict)):
            raise ValueError('対応する集計レポートではありません。')
        report = with_attack_names(report)
    else:
        if auto_quests:
            result = replay_quests(capture)
            quest_view = result['quest_view']
            report = (quest_view or {}).get('current') or result['capture_report']
        else:
            report = replay(capture)
    return {'schema_version': 1, 'state': 'saved_log', 'effective_recording': False,
        'worker_verified': False, 'heartbeat_fresh': False, 'runtime': None,
        'measurement': {'capture_path': str(Path(capture).resolve()), 'complete': True, 'report': report,
                        'activity': {'active': False, 'reason': 'saved_log'}},
        'identity': {'is_live': False, 'state': report.get('identity_state', {})}, 'history': [],
        'quest_view': quest_view}


class Backend:
    def __init__(self, manifest=DEFAULT_MANIFEST, *, root=None, interval=0.5, log_directory=None,
                 monitor=None, controller=None, deployment=None):
        self.monitor = monitor if monitor is not None else RuntimeMonitor(root=root) if root is not None else RuntimeMonitor(manifest=manifest)
        self.controller = controller or RuntimeController()
        self.deployment = deployment
        self.game_directory = None
        self.game_version_error = None
        self.next_game_check = 0
        self.interval = interval
        self.log_directory = log_directory
        self.updates = queue.Queue(maxsize=1)
        self.events = queue.Queue()
        self.requests = queue.Queue()
        self.closed = threading.Event()
        self.thread = None
        self.auto_quests = True
        self.auto_error = None
        self.text_exporter = AutoTextExporter()
        self.text_options = (False, 'quest', False, True)
        self.text_error = None

    def _auto_archive(self, report):
        if self.log_directory is None:
            return None
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]
        path = self.log_directory / f'quest-{digest}.json'
        if not path.exists():
            self.log_directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    stream.write(text)
                try:
                    os.link(temporary, path)  # Publish complete bytes without replacing an existing log.
                except FileExistsError:
                    pass
            finally:
                if temporary.exists():
                    temporary.unlink()
        if path.read_text(encoding='utf-8') != text:
            raise ValueError('自動保存先の内容が一致しません。元の記録は保持しています。')
        return str(path)

    def _poll(self):
        self._watch_game()
        snapshot = self._poll_view()
        snapshot['game_version_error'] = self.game_version_error
        enabled, mode, anonymize, decimals = self.text_options
        entries = {}
        if self.auto_quests:
            reports = [e['report'] for e in snapshot.get('quest_history', [])]
            current = (snapshot.get('quest_view') or {}).get('current')
            if current:
                reports.append(current)
            for report in reports:
                info = report.get('quest_measurement') or {}
                if info.get('kind') == 'quest':
                    entries['quest:' + info['id']] = (report, info.get('end_qpc') is not None)
        else:
            for entry in snapshot.get('history', []):
                if entry.get('report'):
                    entries['capture:' + entry['capture_path']] = (entry['report'], entry.get('complete', False))
            measurement = snapshot.get('capture_measurement') or snapshot.get('measurement')
            if measurement:
                entries['capture:' + measurement['capture_path']] = (measurement['report'], measurement.get('complete', False))
        try:
            paths = self.text_exporter.update(entries, enabled=enabled, dps_mode=mode,
                anonymize=anonymize, time_decimals=decimals)
            if paths:
                self.events.put({'type': 'success', 'message': 'TXTを自動出力しました: ' + str(paths[-1])})
            self.text_error = None
        except (OSError, ValueError) as error:
            message = 'TXT自動出力に失敗しました（元データは保持）: ' + str(error)
            if message != self.text_error:
                self.events.put({'type': 'error', 'message': message})
            self.text_error = message
        snapshot['text_export_error'] = self.text_error
        return snapshot

    def _watch_game(self, *, force=False):
        if self.deployment is None or not self.game_directory:
            return
        now = time.monotonic()
        if not force and now < self.next_game_check:
            return
        self.next_game_check = now + 5
        try:
            self.deployment.verify_game(self.game_directory, force=force)
            self.game_version_error = None
        except (OSError, ValueError) as error:
            message = str(error)
            if message != self.game_version_error:
                self.events.put({'type': 'game_version_error', 'message': message})
            self.game_version_error = message

    def _poll_view(self):
        snapshot = self.monitor.poll()
        if not self.auto_quests:
            return snapshot
        view = snapshot.get('quest_view') or {}
        reports = {}
        for entry in snapshot.get('history', []):
            for report in entry.get('quest_reports', []):
                reports[report['quest_measurement']['id']] = report
        for report in view.get('history', []):
            reports[report['quest_measurement']['id']] = report
        history = []
        failed = False
        for key, report in reports.items():
            try:
                path = self._auto_archive(report)
            except (OSError, ValueError) as error:
                failed = True
                message = '自動保存に失敗しました（生記録は保持）: ' + str(error)
                if message != self.auto_error:
                    self.events.put({'type': 'error', 'message': message})
                self.auto_error = message
                path = None  # Keep the verified in-memory report available.
            info = report['quest_measurement']
            history.append({'report_path': path, 'report': report, 'label':
                (quest_label(report) if info['kind'] == 'quest' else 'クエスト外') + f" #{info['number']}",
                'end_reason': info['end_reason'], 'complete': info['stream_finalized']})
        snapshot['quest_history'] = history
        if not failed and self.auto_error and reports:
            self.auto_error = None
            self.events.put({'type': 'success', 'message': '自動保存を再開しました。'})
        snapshot['auto_save_error'] = self.auto_error
        snapshot['auto_quests_enabled'] = True
        current = view.get('current')
        if current and snapshot.get('measurement'):
            snapshot['capture_measurement'] = snapshot['measurement']
            snapshot['measurement'] = {**snapshot['measurement'], 'report': current,
                'quest_id': current['quest_measurement']['id']}
        return snapshot

    def start(self):
        self.thread = threading.Thread(target=self._run, name='LazyParse-state', daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()

    def submit(self, action, snapshot=None, path=None):
        self.requests.put((action, snapshot, path))

    def _publish(self, snapshot):
        try:
            self.updates.get_nowait()
        except queue.Empty:
            pass
        self.updates.put_nowait(snapshot)

    def _archive(self, snapshot):
        capture = snapshot['measurement']['capture_path']
        follower = next((value for value in self.monitor.segments.values() if str(value.path) == capture), None)
        if follower is None or not follower.complete:
            raise ValueError('記録の保存完了を確認できません。')
        report = (snapshot['measurement']['report'] if snapshot['measurement'].get('quest_id')
                  else follower.report(full=True))
        if self.log_directory is None:
            raise ValueError('ログの保存先が設定されていません。')
        self.log_directory.mkdir(parents=True, exist_ok=True)
        path = self.log_directory / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.json')
        with path.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        return path

    def _execute(self, action, requested, path):
        if action in {'InstallLoader', 'RemoveLoader', 'CheckLoader'}:
            if self.deployment is None:
                raise ValueError('配置用の取得DLLを準備できていません。再ビルドしてください。')
            if action == 'InstallLoader':
                folder = self.deployment.install(path)
                self.game_directory = folder
                self.game_version_error = None
                self.events.put({'type': 'loader_installed', 'folder': folder, 'message': '取得DLLの配置を完了しました。通常のランチャーからPSO2を起動できます。'})
            elif action == 'CheckLoader':
                self.game_directory = path
                try:
                    self.deployment.verify_game(path)
                    self.game_version_error = None
                    if self.deployment.needs_reselection():
                        self.events.put({'type': 'loader_reselect', 'message':
                            'LazyParseの更新を検出しました。PSO2を終了した状態でDLL配置先を選択してください。'})
                        return
                    self.deployment.check(path)
                    self.game_version_error = None
                except GameVersionMismatch as error:
                    if self.game_version_error != str(error):
                        self.events.put({'type': 'game_version_error', 'message': str(error)})
                    self.game_version_error = str(error)
                except (OSError, ValueError) as error:
                    self.events.put({'type': 'loader_missing', 'message': str(error)})
            else:
                self.deployment.remove()
                self.game_directory = None
                self.game_version_error = None
                self.events.put({'type': 'success', 'message': '追加した取得DLLを撤去しました。'})
            return
        if action == 'ExportText':
            report = ((requested or {}).get('measurement') or {}).get('report')
            if report is None and (requested or {}).get('export_source'):
                report = saved_snapshot(requested['export_source'])['measurement']['report']
            if not report:
                raise ValueError('保存する集計がありません。')
            _, mode, anonymize, decimals = self.text_options
            exported = save_text(report, path=path, dps_mode=mode, anonymize=anonymize, time_decimals=decimals)
            self.events.put({'type': 'success', 'message': 'TXTを保存しました: ' + str(exported)})
            return
        if action in {'AutoOn', 'AutoOff'}:
            self.auto_quests = action == 'AutoOn'
            self.events.put({'type': 'success', 'message': 'クエスト自動計測を' +
                             ('有効' if self.auto_quests else '無効') + 'にしました。'})
            return
        if action == 'Open':
            self.events.put({'type': 'opened', 'snapshot': saved_snapshot(path, auto_quests=self.auto_quests)})
            return
        if action in {'Start', 'Next', 'Reset'}:
            if self.deployment is not None and not self.game_directory:
                raise ValueError('DLL配置先の確認が完了していません。「設定」からフォルダーを確認してください。')
            self._watch_game(force=True)
            if self.game_version_error:
                raise ValueError(self.game_version_error)
            if self.deployment is not None and self.deployment.needs_reselection():
                raise ValueError('このLazyParseのDLL配置先を確認していません。「設定」からフォルダーを再選択してください。')
        current = self._poll()
        if requested is None or (current.get('root'), current.get('run_name')) != (requested.get('root'), requested.get('run_name')):
            raise ValueError('操作前に取得先が変わりました。最新の状態でもう一度操作してください。')
        if action in {'Next', 'Reset'}:
            self.controller.invoke(current, 'Stop')
            deadline = time.monotonic() + 5
            stopped = None
            while time.monotonic() < deadline and not self.closed.is_set():
                stopped = self._poll()
                if stopped.get('run_name') != current['run_name']:
                    raise ValueError('保存中にゲームの起動先が変わりました。')
                measurement = stopped.get('measurement')
                if measurement and measurement['complete']:
                    break
                self.closed.wait(0.1)
            else:
                raise ValueError('記録の保存が完了するまで待機しています。状態を確認してください。')
            archive = self._archive(stopped) if action == 'Next' else None
            self._watch_game(force=True)
            if self.game_version_error:
                raise ValueError(self.game_version_error)
            self.controller.invoke(stopped, 'Start')
            self.events.put({'type': 'success', 'message': '保存して次の計測を開始しました。' if archive else '新しい計測を開始しました。',
                             'archive': str(archive) if archive else None})
        else:
            self.controller.invoke(current, action)
            self.events.put({'type': 'success', 'message': {'Start': '記録を開始しました。', 'Stop': '記録を停止しました。',
                                                          'Shutdown': '取得を終了しました。'}[action]})

    def _run(self):
        while not self.closed.is_set():
            try:
                action, snapshot, path = self.requests.get_nowait()
            except queue.Empty:
                pass
            else:
                try:
                    self._execute(action, snapshot, path)
                except (OSError, ValueError, KeyError) as error:
                    self.events.put({'type': 'error', 'message': str(error),
                                     'loader_action': action in {'InstallLoader', 'RemoveLoader', 'CheckLoader'}})
            try:
                self._publish(self._poll())
            except Exception as error:
                self.events.put({'type': 'error', 'message': str(error)})
            self.closed.wait(self.interval)
