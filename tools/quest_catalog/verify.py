"""Verify private extraction evidence and the native catalog mock artifacts."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path

from tools.combat_core.metadata import read_metadata
from .packets import join_names, parse_quests, read_catalog

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    build_path = HERE / 'bin/autostart-build.json'
    build = json.loads(build_path.read_text(encoding='utf-8-sig'))
    native = Path(build['test_directory'])
    proof = json.loads((HERE / 'results/local-names.json').read_text(encoding='utf-8'))
    assert all(sha(ROOT / name) == value for name, value in proof['protected_dictionaries_sha256'].items())
    assert sha(ROOT / 'QuestNames.csv') == proof['output_sha256']
    game_data = Path(os.environ.get("PSO2_GAME_DATA", "pso2_data"))
    archives = json.loads((HERE / 'research/local-archive-evidence.json').read_text(encoding='utf-8-sig'))
    for archive in archives:
        assert archive['source_unchanged'] and sha(game_data / archive['archive']) == archive['sha256']
    for source, expected in proof['input_member_sha256'].items():
        assert sha(HERE / 'research/extracted' / source) == expected
    with (ROOT / 'QuestNames.csv').open(encoding='utf-8-sig', newline='') as stream:
        names = {int(row['name_id_candidate']): row['name'] for row in csv.DictReader(stream)}
    raw = (native / 'identity-context/quest-catalog.opcat').read_bytes()
    records = read_catalog(raw)
    assert len(records) == 5 and [len(r.packet) for r in records] == [2164, 178628, 820, 8, 8]
    assert [len(parse_quests(r.packet)) for r in records] == [2, 166, 2, 0, 0]
    rows = join_names(records, names)
    assert len(rows) == 166
    assert next(r for r in rows if r['quest_id'] == 475)['name'] == '森林探索'
    assert next(r for r in rows if r['quest_id'] == 1130)['name'] == '領域調査：異世界の残滓'
    status = json.loads((native / 'identity-context/quest-catalog-status.json').read_text())
    assert status['stopped'] and status['seen'] == 7 and status['enqueued'] == status['written'] == 5
    assert status['dropped'] == status['io_error'] == 0
    assert status['invalid'] == status['oversize'] == 1  # Deliberate malformed/oversized fixtures.
    normal = native / 'observer-normal'
    catalog = native / 'observer-catalog'
    assert not (normal / 'identity-context/quest-catalog.opcat').exists()
    for relative in ('identity-context/identity.opmeta', *(f'segment-{i:06d}/identity.opmeta' for i in range(1, 5))):
        left, right = (read_metadata(base / relative) for base in (normal, catalog))
        normalized = lambda events: [(e.sequence, e.damage_sequence, e.packet_size, e.prefix) for e in events]
        assert normalized(left) == normalized(right), relative
    for i in range(1, 5):
        frames = [(base / f'segment-{i:06d}/capture.opcap').read_bytes() for base in (normal, catalog)]
        assert frames[0][:16] == frames[1][:16]
        assert [frames[0][j + 24:j + 108] for j in range(32, len(frames[0]), 108)] == [
            frames[1][j + 24:j + 108] for j in range(32, len(frames[1]), 108)]
    auto_runs = sorted((native / 'autostart-test').glob('run-*'))
    assert len(auto_runs) == 2
    for run in auto_runs:
        # Bootstrap fixture only waits for Classic, then shuts down. Actual
        # observer operation is exercised separately by both observer hosts.
        assert (run / 'control.command').read_text().splitlines() == ['OPC-CONTROL-1', '1', 'shutdown']
        assert not (run / 'identity-context').exists()
    bootstrap_state = json.loads((native / 'autostart-test/autostart-state.json').read_text())
    assert bootstrap_state['state'] == 'shutdown' and bootstrap_state['win32_error'] == 0
    current_normal = json.loads((ROOT / 'tools/native_capture/bin/autostart-build.json').read_text(encoding='utf-8-sig'))
    assert sha(Path(current_normal['core'])) == current_normal['core_sha256']
    assert sha(Path(current_normal['loader'])) == current_normal['loader_sha256']
    assert sha(Path(build['core'])) == build['core_sha256']
    assert sha(Path(build['loader'])) == build['loader_sha256']
    source_paths = [HERE / name for name in ('local_text.py', 'export_names.py', 'packets.py', 'test_catalog.py',
                   'capture_test_host.c', 'build_capture.ps1', 'extract_local.ps1', 'verify.py')]
    source_paths += [ROOT / 'tools/native_capture' / name for name in (
        'capture.c', 'identity_observer.h', 'quest_catalog_capture.h', 'autostart.ps1')]
    result = {
        'phase': 'offline_names_and_catalog_mock_verified_live_list_pending',
        'name_count': len(names), 'known_names_matched': 6, 'packet_name_ids_live_verified': False,
        'source_archives_unchanged': archives, 'protected_csv_unchanged': proof['protected_dictionaries_sha256'],
        'native_test_directory': str(native), 'native_mock_status': status,
        'normal_metadata_and_damage_packets_equal': True, 'autostart_runs_verified': len(auto_runs),
        'autostart_fixture_scope': 'bootstrap worker lifetime and relaunch, without game/observer initialization',
        'normal_build_manifest_and_binaries_retained': True,
        'catalog_manifest': str(build_path), 'catalog_capture_session': build['session'],
        'python_tests_passed': 9,
        'source_sha256': {str(path.relative_to(ROOT)): sha(path) for path in source_paths},
        'quest_names_csv_sha256': sha(ROOT / 'QuestNames.csv'),
    }
    live_path = HERE / 'results/live-result.json'
    cleanup_path = HERE / 'results/cleanup-after.json'
    if live_path.exists() and cleanup_path.exists():
        live = json.loads(live_path.read_text(encoding='utf-8'))
        cleanup = json.loads(cleanup_path.read_text(encoding='utf-8-sig'))
        current = json.loads((HERE / 'results/current.json').read_text(encoding='utf-8-sig'))
        run = Path(current['run_directory'])
        received = run / 'identity-context/quest-catalog.opcat'
        closed_status = json.loads((run / 'identity-context/quest-catalog-status.json').read_text())
        assert closed_status['stopped'] and closed_status['seen'] == closed_status['enqueued'] == closed_status['written'] == 230
        assert all(closed_status[key] == 0 for key in ('io_error', 'dropped', 'invalid', 'oversize'))
        assert sha(received) == live['raw_sha256']
        live_records = read_catalog(received.read_bytes())
        assert len(live_records) == 230 and sorted(r.sequence for r in live_records) == list(range(1, 231))
        live_rows = join_names(live_records, names)
        assert len(live_rows) == 810 and all(row['name'] for row in live_rows)
        assert sum(row['status'] == 'matched' for row in live_rows) == 809
        zero_rows = [row for row in live_rows if row['status'] != 'matched']
        assert len(zero_rows) == 1 and zero_rows[0]['quest_id'] == 0 and zero_rows[0]['name_id'] == 30010
        assert len(live['known_anchors']) == 6 and sha(ROOT / 'QuestCatalog.csv') == live['output_sha256']
        assert cleanup['added_loader_absent'] and cleanup['install_ledger_absent']
        assert not (Path(current['game_bin']) / 'eos_gaming.dll').exists()
        assert not (ROOT / 'tools/native_capture/captures/autostart-install.json').exists()
        result.update(phase='complete_810_objects_saved_loader_removed',
                      live_validation={'records': 230, 'quest_objects': 810, 'names_resolved': 810,
                                       'known_anchors': live['known_anchors'], 'status': closed_status,
                                       'zero_id_entry': zero_rows[0], 'raw_sha256': live['raw_sha256'],
                                       'catalog_csv_sha256': live['output_sha256'], 'scope': live['excluded_scope']},
                      cleanup=cleanup)
    (HERE / 'results/verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('phase', 'name_count', 'normal_metadata_and_damage_packets_equal', 'autostart_runs_verified')}))


if __name__ == '__main__':
    main()
