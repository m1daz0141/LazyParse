import struct
import unittest

from tools.combat_core.test_combat_core import OTHER, SELF, engine, packet, registry
from tools.gui.timing import with_dps_mode
from tools.gui.viewmodel import actor_rows, attack_rows
from tools.combat_core.damage_policy import BURN_ATTACK_IDS

BURN = min(BURN_ATTACK_IDS)


def hit(hp, part, source=SELF, value=100, attack=99):
    data = bytearray(packet(source=source, value=value, attack=attack))
    struct.pack_into('<I', data, 0x34, hp)
    data[0x38] = part
    return bytes(data)


def run(hits):
    return engine([(n, n * 1000, data) for n, data in enumerate(hits, 1)], registry()).finish()


def actor(report, ref):
    return next(a for a in report['actors'] if a['object_id'] == ref.object_id)


class HpLinkTests(unittest.TestCase):
    def test_consecutive_hits_that_leave_hp_unchanged_are_unlinked_and_not_rated(self):
        report = run([hit(1000, 1), hit(900, 1), hit(900, 4, value=50), hit(900, 4, value=60),
                      hit(800, 1), hit(800, 4, value=70)])
        me = actor(report, SELF)
        self.assertEqual(480, me['damage_dealt'])
        self.assertEqual((180, 3), (me['hp_unlinked_damage'], me['hp_unlinked_hits']))
        self.assertEqual((3, 180), (report['damage_totals']['hp_unlinked_records'],
                                    report['damage_totals']['hp_unlinked_sum']))
        elapsed = report['timing']['elapsed_seconds']
        self.assertAlmostEqual(300 / elapsed, me['dps'])
        self.assertAlmostEqual(300 / elapsed, report['timing']['dps'])
        attack = next(a for a in report['attacks'] if a['attack_id'] == 99)
        self.assertEqual(180, attack['hp_unlinked_damage'])

    def test_single_non_lowering_hit_stays_rated(self):
        report = run([hit(1000, 1), hit(900, 1), hit(900, 2), hit(800, 2), hit(700, 1)])
        self.assertEqual(0, actor(report, SELF)['hp_unlinked_damage'])

    def test_part_relinks_when_hp_drops_and_killing_blow_is_never_unlinked(self):
        report = run([hit(1000, 1), hit(1000, 4), hit(1000, 4), hit(900, 4), hit(900, 4), hit(0, 4), hit(0, 4)])
        self.assertEqual(200, actor(report, SELF)['hp_unlinked_damage'])

    def test_unchanged_hp_seen_from_other_players_hits_counts_per_part(self):
        report = run([hit(1000, 1), hit(1000, 5, source=OTHER), hit(900, 1), hit(900, 5, source=OTHER), hit(800, 1)])
        self.assertEqual(200, actor(report, OTHER)['hp_unlinked_damage'])
        self.assertEqual(0, actor(report, SELF)['hp_unlinked_damage'])

    def test_partless_objects_are_left_out_of_dps_including_their_killing_blow(self):
        report = run([hit(5000, 255, value=10), hit(4990, 255, value=20), hit(4970, 255, value=30),
                      hit(0, 255, value=40, attack=BURN)])
        me = actor(report, SELF)
        self.assertEqual((100, 4), (me['hp_unlinked_damage'], me['hp_unlinked_hits']))

    def test_burn_ticks_on_a_normal_enemy_do_not_make_it_partless(self):
        report = run([hit(5000, 255, attack=BURN), hit(4900, 255, attack=BURN), hit(4800, 2),
                      hit(4700, 255, attack=BURN), hit(4600, 255), hit(4500, 255)])
        self.assertEqual(0, actor(report, SELF)['hp_unlinked_damage'])

    def test_reused_object_id_after_defeat_starts_a_new_target(self):
        report = run([hit(500, 1), hit(0, 1), hit(5000, 255, value=10), hit(4990, 255, value=20)])
        self.assertEqual(30, actor(report, SELF)['hp_unlinked_damage'])

    def test_rows_and_dps_modes_exclude_unlinked_damage(self):
        report = with_dps_mode(run([hit(1000, 1), hit(1000, 4), hit(1000, 4), hit(900, 1)]), 'quest')
        row = next(r for r in actor_rows(report) if r['role'] == 'self')
        elapsed = report['timing']['elapsed_seconds']
        self.assertEqual((200, 400, 200), (row['damage_dealt'], row['total_damage_dealt'], row['hp_unlinked_damage']))
        self.assertAlmostEqual(200 / elapsed, row['dps'])

    def test_list_rows_leave_out_unlinked_damage_max_hit_and_attack_but_detail_keeps_them(self):
        report = with_dps_mode(run([hit(1000, 1, value=300, attack=7), hit(1000, 4, value=5000, attack=8),
                                    hit(1000, 4, value=9000, attack=8), hit(700, 1, value=200, attack=7)]), 'quest')
        row = next(r for r in actor_rows(report) if r['role'] == 'self')
        self.assertEqual((500, 14500), (row['damage_dealt'], row['total_damage_dealt']))
        self.assertEqual((300, 7), (row['max_hit'], row['max_attack_id']))
        self.assertEqual(100, row['share'])
        detail = {a['attack_id']: a for a in attack_rows(report, row)}
        self.assertEqual((14000, 9000, 0), (detail[8]['damage_dealt'], detail[8]['max_hit'], detail[8]['rated_max_hit']))

    def test_detail_window_shows_excluded_damage_beside_damage(self):
        import tkinter as tk
        from tools.gui.app import DetailWindow
        report = with_dps_mode(run([hit(1000, 1, value=300, attack=7), hit(1000, 4, value=5000, attack=8),
                                    hit(1000, 4, value=9000, attack=8), hit(700, 1, value=200, attack=7)]), 'quest')
        row = next(r for r in actor_rows(report) if r['role'] == 'self')
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        try:
            detail = DetailWindow(root, report, row)
            tree = detail.attacks
            self.assertEqual('Damage(対象外)', tree.heading('excluded', 'text'))
            values = {tree.item(item, 'values')[1]: tree.item(item, 'values') for item in tree.get_children()}
            self.assertEqual(('14,000', '14,000'), values['0x00000008'][3:5])
            self.assertEqual(('500', '—'), values['0x00000007'][3:5])
            detail.sort_by(tree, 'excluded')
            self.assertEqual('0x00000008', tree.item(tree.get_children()[0], 'values')[1])
            detail.window.destroy()
        finally:
            root.destroy()

    def test_rated_max_falls_back_when_its_hit_is_reclassified(self):
        report = run([hit(1000, 1, value=50), hit(1000, 4, value=900), hit(1000, 4, value=10), hit(900, 1, value=40)])
        self.assertEqual(50, actor(report, SELF)['rated_max_hit'])
        self.assertEqual(900, actor(report, SELF)['max_hit'])

    def test_old_reports_without_the_field_keep_full_dps(self):
        report = with_dps_mode(run([hit(1000, 1), hit(900, 1)]), 'quest')
        for group in report['actors']:
            group.pop('hp_unlinked_damage')
        row = next(r for r in actor_rows(report) if r['role'] == 'self')
        self.assertAlmostEqual(200 / report['timing']['elapsed_seconds'], row['dps'])


if __name__ == '__main__':
    unittest.main()
