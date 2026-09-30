import copy
import csv
import json
import bisect
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dividend_monitor as dm
import update_arisk_data as updater


class DividendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The imported official seed remains immutable when live history is refreshed.
        p, t = {}, {}
        for suffix in ('warmup', 'perf'):
            p.update(dm.normalize_csi(json.loads((dm.DATA / 'raw' / f'csi_930955_{suffix}.json').read_text()), '930955'))
            t.update(dm.normalize_csi(json.loads((dm.DATA / 'raw' / f'csi_H20955_{suffix}.json').read_text()), 'H20955'))
        cls.quotes = dm.paired_quotes(p, t)
        with (dm.DATA / 'cn10y-seed.csv').open() as f:
            cls.bonds = {r['date']: float(r['cn10y_yield']) for r in csv.DictReader(f)}
        cls.rows = dm.calculate(cls.quotes, cls.bonds)

    def row(self, **changes):
        result = dict(pe=8., ready=True, pe_pct=50., dy_pct=50., spread=2.5,
                      price_bias250_pct=-7., price_ma250_change20_pct=.1)
        result.update(changes)
        return result

    def test_ma_boundaries(self):
        for bias, expected in [(10, 0), (5, .5), (-4.999, 1), (-5, 2),
                               (-9.999, 2), (-10, 3), (-14.999, 3), (-15, 4)]:
            self.assertEqual(dm.rules(self.row(price_bias250_pct=bias))['ma'], expected)

    def test_joint_guards_and_exact_boundaries(self):
        cases = [({'pe_pct': 80, 'price_bias250_pct': -20}, 0),
                 ({'pe_pct': 60}, 1), ({'pe_pct': 39.99, 'spread': 1.5}, 3),
                 ({'pe_pct': 40, 'spread': 1.5}, 2),
                 ({'pe_pct': 19.99, 'price_bias250_pct': -10, 'spread': 2.3}, 4),
                 ({'pe_pct': 20, 'price_bias250_pct': -10, 'spread': 2.3}, 3),
                 ({'pe_pct': 10, 'price_bias250_pct': -20, 'spread': -.01}, 1),
                 ({'pe_pct': 10, 'price_bias250_pct': -20, 'dy_pct': 19.99}, 1),
                 ({'pe_pct': 10, 'price_bias250_pct': -20, 'dy_pct': 20}, 4),
                 ({'pe_pct': 10, 'price_bias250_pct': 10}, 0)]
        for changes, expected in cases:
            self.assertEqual(dm.rules(self.row(**changes))['joint'], expected)

    def test_invalid_pe_and_warmup(self):
        for pe in [None, 0, -1, float('nan')]:
            s = dm.rules(self.row(pe=pe))
            self.assertEqual((s['joint'], s['trend'], s['pe_only'], s['old']), (0, 0, 0, 0))
        s = dm.rules(self.row(ready=False, pe_pct=None, dy_pct=None))
        self.assertEqual((s['joint'], s['pe_only']), (2, 1))
        self.assertEqual(dm.rules(self.row(ready=False, price_bias250_pct=None))['joint'], 1)
        self.assertEqual(dm.rules(self.row(pe_pct=10, price_bias250_pct=-20,
                                        price_ma250_change20_pct=-.1))['trend'], 2)

    def test_archived_v13_daily_values(self):
        with (Path(__file__).parent / 'fixtures' / 'v13-signals.csv').open() as f:
            refs = list(csv.DictReader(f))
        self.assertEqual(len(refs), len(self.rows))
        keys = ['price', 'tr', 'pe', 'dy_proxy', 'cn10y', 'spread', 'dy_pct', 'pe_pct',
                'price_ma250', 'price_bias250_pct', 'price_ma250_change20_pct', 'tr_bias250_pct']
        for ref, row in zip(refs, self.rows):
            self.assertEqual(ref['date'], row['date'])
            self.assertEqual(ref['ready'] == 'True', row['ready'])
            for key in keys:
                if ref[key]:
                    self.assertAlmostEqual(float(ref[key]), row[key], places=7, msg=f'{row["date"]} {key}')
                else:
                    self.assertIsNone(row[key])
            # Independent scalar rendition of the archived backtest's v1.3 targets.
            bias, pp, spread, dp = (row[k] for k in ('price_bias250_pct', 'pe_pct', 'spread', 'dy_pct'))
            m = 1 if bias is None else 0 if bias >= 10 else .5 if bias >= 5 else 1 if bias > -5 else 2 if bias > -10 else 3 if bias > -15 else 4
            if row['ready']:
                if pp >= 60: m = min(m, 1)
                if pp < 40 and bias <= -5 and spread >= 1.5: m = max(m, 3)
                if pp < 20 and bias <= -10 and spread >= 2.3: m = 4
                if spread < 0 or dp < 20: m = min(m, 1)
                if pp >= 80: m = 0
            if row['pe'] is None or row['pe'] <= 0: m = 0
            self.assertEqual(m, row['signals']['joint'])

    def test_no_lookahead_and_moving_average_maturity(self):
        prefix = dm.calculate(self.quotes[:1500], self.bonds)
        self.assertEqual(prefix, self.rows[:1500])
        self.assertIsNone(self.rows[248]['price_ma250'])
        self.assertIsNotNone(self.rows[249]['price_ma250'])
        self.assertIsNone(self.rows[268]['price_ma250_change20_pct'])
        self.assertIsNotNone(self.rows[269]['price_ma250_change20_pct'])
        mature = next(r for r in self.rows if r['ready'])
        self.assertEqual(mature['pe_n'], 756)

    def test_six_month_return_rank_uses_only_known_price_observations(self):
        days = [q['date'] for q in self.quotes]
        latest = self.rows[-1]
        anchor = dm.months_before(latest['date'], 6)
        base_index = bisect.bisect_right(days, anchor) - 1
        expected_return = 100 * (self.quotes[-1]['price'] / self.quotes[base_index]['price'] - 1)
        self.assertEqual(latest['price_return_6m_base_date'], days[base_index])
        self.assertAlmostEqual(latest['price_return_6m_pct'], expected_return)
        prior = sorted(r['price_return_6m_pct'] for r in self.rows if r['price_return_6m_pct'] is not None)
        expected_rank = 100 * (bisect.bisect_left(prior, expected_return) +
                               .5 * (bisect.bisect_right(prior, expected_return) - bisect.bisect_left(prior, expected_return))) / len(prior)
        self.assertEqual(latest['price_return_6m_rank_n'], len(prior))
        self.assertAlmostEqual(latest['price_return_6m_rank_pct'], expected_rank)
        self.assertEqual(next(r['price_return_6m_rank_n'] for r in self.rows if r['price_return_6m_signal'] != 'unavailable'), 756)
        self.assertEqual(dm.months_before('2024-08-31', 6), '2024-02-29')
        self.assertEqual(dm.months_before('2025-08-31', 6), '2025-02-28')

    def test_six_month_signal_boundaries_and_gap_guard(self):
        cases = [(9.99, 'strong_buy'), (10, 'strong_buy'), (10.01, 'low_buy'),
                 (20, 'low_buy'), (20.01, 'neutral'), (79.99, 'neutral'),
                 (80, 'sell'), (89.99, 'sell'), (90, 'clear'), (100, 'clear')]
        for percentile, expected in cases:
            self.assertEqual(dm.return6_signal(percentile, 756), expected)
            self.assertEqual(dm.return6_signal(percentile, 755), 'unavailable')
        self.assertEqual(dm.return6_signal(None, 1000), 'unavailable')
        # A broken historical series must not use a quote weeks before the anchor.
        rows = dm.calculate([{'date': '2026-01-02', 'price': 100, 'tr': 100, 'pe': 8},
                             {'date': '2026-07-20', 'price': 90, 'tr': 90, 'pe': 8}], {})
        self.assertIsNone(rows[-1]['price_return_6m_pct'])
        self.assertIsNone(rows[-1]['price_return_6m_base_date'])

    def test_pair_dates_and_prices_rejected(self):
        with self.assertRaises(ValueError): dm.paired_quotes({'2026-09-28': {}}, {})
        with self.assertRaises(ValueError): dm.calculate([self.quotes[1], self.quotes[0]], self.bonds)
        q = copy.deepcopy(self.quotes[:2]); q[1]['tr'] = 0
        with self.assertRaises(ValueError): dm.calculate(q, self.bonds)

    def test_bond_alignment_never_uses_future(self):
        q = self.quotes[:3]
        rows = dm.calculate(q, {q[1]['date']: 3., q[2]['date']: 4.})
        self.assertIsNone(rows[0]['cn10y'])
        self.assertEqual(rows[1]['cn10y'], 3.)

    def test_holiday_and_publishing_buffer(self):
        sessions = ['2026-09-24', '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-08', '2026-10-09']
        self.assertEqual(dm.expected_session(sessions, datetime(2026, 9, 29, 17)), '2026-09-28')
        self.assertEqual(dm.expected_session(sessions, datetime(2026, 9, 29, 18)), '2026-09-29')
        self.assertEqual(dm.expected_session(sessions, datetime(2026, 10, 5, 20)), '2026-09-30')
        self.assertIsNone(dm.expected_session(sessions, datetime(2026, 10, 10, 20)))
        ref = dm.monthly_reference(self.rows)
        self.assertLess(ref['signal_date'], ref['execution_date'])
        self.assertEqual(ref['execution_date'], '2026-09-01')

    def test_snapshot_freshness_without_fake_current(self):
        h = {'quotes': self.quotes, 'bonds': self.bonds, 'calendar': ['2026-09-16', '2026-09-17', '2026-09-18', '2026-10-08']}
        self.assertFalse(dm.build_snapshot(h, datetime(2026, 9, 18, 19))['value']['current'])
        self.assertTrue(dm.build_snapshot(h, datetime(2026, 9, 18, 17))['value']['current'])
        h['calendar'] = []
        self.assertFalse(dm.build_snapshot(h)['value']['current'])

    def test_exact_etf_subset_and_missing_not_zero(self):
        new = [{'基金代码': '515100', '基金份额': 120}, {'基金代码': '515300', '基金份额': 999}]
        old = [{'基金代码': '515100', '基金份额': 100}]
        result = dm.etf_subset(new, old, {'515100': 2}, '2026-09-28', '2026-07-02', '新浪')
        self.assertEqual(len(result['value']['members']), 1)
        self.assertAlmostEqual(result['value']['members'][0]['change_pct'], 20)
        self.assertTrue(result['stale'])
        self.assertFalse(result['value']['coverage_complete'])
        self.assertIsNone(result['value']['change_yi'])
        result = dm.etf_subset(new, [], {}, '2026-09-28', '2026-07-02', '新浪')
        self.assertIsNone(result['value']['members'][0]['change_yi'])

    def test_partial_window_is_not_a_failed_current_fetch(self):
        new = [{'基金代码': r['code'], '基金份额': 120} for r in dm.ETF_MEMBERS]
        old = new[:3]
        prices = {r['code']: 2 for r in dm.ETF_MEMBERS}
        result = dm.etf_subset(new, old, prices, '2026-09-29', '2026-07-02', '新浪')
        self.assertFalse(result['stale'])
        self.assertFalse(result['value']['coverage_complete'])
        self.assertEqual(result['value']['comparable_count'], 3)
        self.assertIsNone(result['value']['change_yi'])

    def test_core_failure_preserves_history_and_marks_current_unavailable(self):
        prior = copy.deepcopy(updater.PREV)
        prior['dividend_lowvol100'] = dm.build_snapshot(
            {'quotes': self.quotes, 'bonds': self.bonds, 'calendar': ['2026-09-17', '2026-09-18']},
            datetime(2026, 9, 17, 19))
        fresh_etf = {'value': {'members': []}, 'stale': False}
        with patch.object(updater, 'PREV', prior), patch.object(updater, 'DIVIDEND_ETF', fresh_etf), \
             patch.object(dm, 'fetch_history', side_effect=TimeoutError('official source timeout')), \
             patch.object(updater, 'log'):
            result = updater.fetch_dividend_lowvol100()
        self.assertTrue(result['stale'])
        self.assertFalse(result['value']['current'])
        self.assertEqual(result['value']['history'], prior['dividend_lowvol100']['value']['history'])
        self.assertEqual(result['value']['etf'], fresh_etf)
        self.assertTrue(prior['dividend_lowvol100']['value']['current'])

    def test_etf_failure_does_not_block_core_and_section_timeout_is_bounded(self):
        history = json.loads((dm.DATA / 'history.json').read_text())
        with patch.object(updater, 'DIVIDEND_ETF', None), patch.object(dm, 'fetch_history', return_value=history), \
             patch.object(updater, 'log'):
            result = updater.fetch_dividend_lowvol100()
        self.assertEqual(result['value']['latest']['date'], history['quotes'][-1]['date'])
        self.assertTrue(result['value']['etf']['stale'])
        event = threading.Event()
        try:
            with patch.object(updater, 'log'):
                timed = updater.run_section('dividend_lowvol100', event.wait, timeout=.01)
            self.assertTrue(timed['stale'])
            self.assertFalse(timed['value']['current'])
        finally:
            event.set()

    def test_full_pipeline_keeps_existing_fields_and_adds_independent_field(self):
        called = []
        def section(key, fn):
            called.append(key)
            return copy.deepcopy(updater.PREV[key])
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'snapshot.json'
            with patch.object(updater, 'OUT_PATH', str(dest)), patch.object(updater, 'run_section', section), \
                 patch.object(updater, 'log'), patch.object(updater.os, '_exit'):
                updater.main()
            result = json.loads(dest.read_text())
        self.assertEqual(len(called), 14)
        self.assertEqual(called[-1], 'dividend_lowvol100')
        for key in called:
            self.assertEqual(result[key], updater.PREV[key])


if __name__ == '__main__':
    unittest.main()
