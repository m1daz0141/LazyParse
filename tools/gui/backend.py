"""Single worker owns polling and control; the UI consumes detached snapshots."""
from datetime import datetime
import json
import hashlib
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from ..combat_core.live import RuntimeMonitor
from ..combat_core.__main__ import replay
from ..combat_core.quest_replay import replay_quests
from ..attack_catalog.catalog import with_attack_names

NATIVE = Path(__file__).resolve().parents[1] / 'native_capture'
DEFAULT_MANIFEST = NATIVE / 'bin/autostart-build.json'


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


class RuntimeController:
    def __init__(self, expected_process='pso2'):
        self.expected_process = expected_process

    def invoke(self, snapshot, action):
        if not snapshot.get('worker_verified') or not snapshot.get('heartbeat_fresh') or snapshot.get('state') == 'shutdown':
            raise ValueError('取得ワーカーを確認できません。最新の状態を確認してください。')
        shell = shutil.which('pwsh')
        if shell is None:
            raise ValueError('取得制御に必要なPowerShell 7が見つかりません。')
        args = [shell, '-NoProfile', '-NonInteractive', '-File', str(NATIVE / 'runtime_client.ps1'),
                '-Action', action, '-Root', snapshot['root'], '-ExpectedProcessName', self.expected_process,
                '-ExpectedRunName', snapshot['run_name']]
        result = subprocess.run(args, capture_output=True, encoding='utf-8', errors='replace', timeout=20,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError((result.stderr or result.stdout).strip())
        return result.stdout.strip()


class Backend:
    def __init__(self, manifest=DEFAULT_MANIFEST, *, root=None, interval=0.5, log_directory=None,
                 monitor=None, controller=None):
        self.monitor = monitor if monitor is not None else RuntimeMonitor(root=root) if root is not None else RuntimeMonitor(manifest=manifest)
        self.controller = controller or RuntimeController()
        self.interval = interval
        self.log_directory = log_directory
        self.updates = queue.Queue(maxsize=1)
        self.events = queue.Queue()
        self.requests = queue.Queue()
        self.closed = threading.Event()
        self.thread = None
        self.auto_quests = True
        self.auto_error = None

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
                ('クエスト' if info['kind'] == 'quest' else 'クエスト外') + f" #{info['number']}",
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
        if action in {'AutoOn', 'AutoOff'}:
            self.auto_quests = action == 'AutoOn'
            self.events.put({'type': 'success', 'message': 'クエスト自動計測（試験中）を' +
                             ('有効' if self.auto_quests else '無効') + 'にしました。'})
            return
        if action == 'Open':
            self.events.put({'type': 'opened', 'snapshot': saved_snapshot(path, auto_quests=self.auto_quests)})
            return
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
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    self.events.put({'type': 'error', 'message': str(error)})
            try:
                self._publish(self._poll())
            except Exception as error:
                self.events.put({'type': 'error', 'message': str(error)})
            self.closed.wait(self.interval)
