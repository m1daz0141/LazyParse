"""Local command-file controller; no PowerShell or network dependency."""
import os
from pathlib import Path
import re
import time
import uuid

from ..combat_core.live import RuntimeMonitor
from ..combat_core.shared_files import read_text
from ..portable.files import exclusive_file


def publication_read(operation):
    # Atomic Windows publication can transiently deny even a fully shared
    # open. Retry only missing/sharing/locking errors, as the PS client did.
    for attempt in range(10):
        try:
            return operation()
        except OSError as error:
            if getattr(error, 'winerror', None) not in {2, 32, 33} or attempt == 9:
                raise
            time.sleep(.01)


def command_error_message(state):
    code = state['last_command_error']
    message = f'取得操作に失敗しました: Win32={code}'
    if (state.get('last_command') == 'start' and state.get('binding_error') == code
            and state.get('guard_error') == code):
        stage = {1: 'スレッド一覧の取得', 2: 'スレッドの確認',
                 3: 'スレッドの一時停止', 4: 'スレッド状態の取得'}.get(state.get('guard_stage'))
        if stage:
            message += f'（{stage}で失敗。登録先の更新を中止しました）'
    return message


def preparation_error(snapshot):
    """Keep a native startup failure visible even before its journal exists."""
    # Journal publication may fail during a real startup error. Preserve the
    # separately verified runtime heartbeat, without treating an old run as a
    # current failure or relaxing the controls' normal worker verification.
    if not snapshot.get('runtime_heartbeat_fresh', snapshot.get('heartbeat_fresh', False)):
        return None
    if snapshot.get('runtime_worker_verified') is False:
        return None
    process = snapshot.get('runtime_worker_probe', snapshot.get('worker_probe')) or {}
    if process.get('alive') is False:
        return None
    runtime = snapshot.get('runtime') or {}
    code = runtime.get('identity_context_error')
    if type(code) is not int or not code:
        return None
    message = f'識別情報の取得開始に失敗しました: Win32={code}'
    if runtime.get('guard_error') == code:
        stage = {1: 'スレッド一覧の取得', 2: 'スレッドの確認',
                 3: 'スレッドの一時停止', 4: 'スレッド状態の取得'}.get(runtime.get('guard_stage'))
        if stage:
            message += f'（{stage}で失敗）'
    return message


class RuntimeController:
    def __init__(self, expected_process='pso2', wait_seconds=10):
        self.expected_process, self.wait_seconds = expected_process, wait_seconds

    def invoke(self, snapshot, action):
        if action not in {'Start', 'Stop', 'Shutdown'}:
            raise ValueError('未対応の取得操作です。')
        if not snapshot.get('worker_verified') or not snapshot.get('heartbeat_fresh') or snapshot.get('state') == 'shutdown':
            raise ValueError('取得ワーカーを確認できません。最新の状態を確認してください。')
        monitor = RuntimeMonitor(root=Path(snapshot['root']), expected_process=self.expected_process)

        def current():
            _, run, state, fresh, alive, _, _ = publication_read(monitor._state)
            if (not alive or not fresh or state['state'] == 'shutdown' or
                    state['run_name'] != snapshot['run_name']):
                raise ValueError('取得ワーカーが停止・変更されたか、応答が古くなっています。')
            return run, state

        run, state = current()
        command = run / 'control.command'
        with exclusive_file(run / 'controller.lock'):
            _, state = current()
            last = state.get('last_command_id')
            if type(last) is not int or not 0 <= last < 0xffffffffffffffff:
                raise ValueError('取得操作の連番が不正です。')
            if command.exists():
                pending = re.fullmatch(r'OPC-CONTROL-1\n([0-9]+)\n(start|stop|shutdown)\n', publication_read(lambda: read_text(command)))
                if not pending or int(pending[1]) > last:
                    raise ValueError('前の操作の応答を待っています。状態を確認してください。')
            if action == 'Start':
                import json
                probe = json.loads(publication_read(lambda: read_text(run / 'binding-probe.json')))
                if state.get('desired_recording') or probe.get('stage') != 'binding_ready' or probe.get('win32_error') != 0:
                    raise ValueError('記録が開始済みか、受信先の検証が未完了です。')
                if 'identity_observing' in state and (not state['identity_observing'] or state.get('identity_context_error')):
                    raise ValueError('識別情報の取得が準備できていません。')
            next_id = last + 1
            temporary = run / ('control-' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_bytes(f'OPC-CONTROL-1\n{next_id}\n{action.lower()}\n'.encode('ascii'))
                os.replace(temporary, command)
            finally:
                temporary.unlink(missing_ok=True)
        deadline = time.monotonic() + self.wait_seconds
        while True:
            # Shutdown is an expected terminal state while awaiting its ack.
            _, _, state, _, _, _, _ = publication_read(monitor._state)
            if state['run_name'] != snapshot['run_name']:
                raise ValueError('操作の応答前に取得ワーカーが再起動しました。')
            if state.get('last_command_id') == next_id:
                if state.get('last_command_error'):
                    raise ValueError(command_error_message(state))
                return f"Acknowledged: {action} command={next_id} state={state['state']}"
            if time.monotonic() >= deadline:
                raise ValueError('取得操作の応答待ちです。再操作する前に状態を確認してください。')
            time.sleep(.1)
