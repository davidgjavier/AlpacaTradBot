import os
import importlib.util
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class RestartAccounting(unittest.TestCase):
    def test_trade_commit_then_process_death_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            shutil.copy2(ROOT/'db.py', Path(folder)/'db.py')
            worker = [sys.executable, str(ROOT/'tests/accounting_worker.py'), folder]
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            crashed = subprocess.run(worker+['crash'], env=env, capture_output=True, text=True)
            self.assertEqual(crashed.returncode, 73, crashed.stderr)
            with sqlite3.connect(Path(folder)/'trading_system.db') as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM trade_history').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT target1_filled FROM position_state').fetchone()[0], 0)
            resumed = subprocess.run(worker+['recover'], env=env, capture_output=True, text=True)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            with sqlite3.connect(Path(folder)/'trading_system.db') as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM trade_history').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT target1_filled FROM position_state').fetchone()[0], 1)

class DatabaseDeduplication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        shutil.copy2(ROOT/'db.py', self.folder/'db.py')
        spec = importlib.util.spec_from_file_location('test_db', self.folder/'db.py')
        self.db = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.db)

    def tearDown(self): self.tmp.cleanup()

    def record(self, **changes):
        args = dict(symbol='BTC/USD', strategy='SCALP', entry_price=100., exit_price=101.5,
                    qty=0.5, entry_time='entry', exit_time='exit', exit_reason='target1',
                    execution_key='alpaca:order:shared:target1')
        args.update(changes)
        return self.db.log_trade(**args)

    def test_conflicting_quantity_is_not_silently_dropped(self):
        self.record()
        with self.assertRaises(self.db.ExecutionKeyConflict): self.record(qty=0.6)
        self.assertEqual(len(self.db.get_recent_trades()), 1)

    def test_replay_retains_original_price_and_flags_discrepancy(self):
        first = self.record()
        second = self.record(exit_price=102.)
        self.assertTrue(second['replayed'])
        self.assertTrue(second['price_discrepancy'])
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['exit_price'], 101.5)

    def test_unkeyed_legacy_calls_still_insert(self):
        self.record(execution_key=None)
        self.record(execution_key=None)
        self.assertEqual(len(self.db.get_recent_trades()), 2)

    def test_concurrent_processes_record_once(self):
        cmd = [sys.executable, str(ROOT/'tests/accounting_worker.py'), str(self.folder), 'insert']
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        processes = [subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(6)]
        for proc in processes:
            out, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(len(self.db.get_recent_trades()), 1)

    def test_concurrent_startup_upgrades_old_schema_once(self):
        with sqlite3.connect(self.folder/'trading_system.db') as conn:
            conn.execute('DROP TABLE trade_history')
            conn.execute('CREATE TABLE trade_history (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, strategy TEXT, entry_price REAL, exit_price REAL, qty REAL, entry_time TEXT, exit_time TEXT, exit_reason TEXT, gross_pnl REAL, fees_paid REAL, net_pnl REAL)')
        self.test_concurrent_processes_record_once()
        with sqlite3.connect(self.folder/'trading_system.db') as conn:
            self.assertIn('execution_key', [r[1] for r in conn.execute('PRAGMA table_info(trade_history)')])
            self.assertIn('ux_trade_execution_key', [r[1] for r in conn.execute('PRAGMA index_list(trade_history)')])

    def test_old_schema_upgrade_preserves_legacy_rows(self):
        with sqlite3.connect(self.folder/'trading_system.db') as conn:
            conn.execute('DROP TABLE trade_history')
            conn.execute('CREATE TABLE trade_history (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, strategy TEXT, entry_price REAL, exit_price REAL, qty REAL, entry_time TEXT, exit_time TEXT, exit_reason TEXT, gross_pnl REAL, fees_paid REAL, net_pnl REAL)')
            conn.execute("INSERT INTO trade_history(symbol, qty) VALUES ('BTC/USD', 0.5)")
        self.db.init_db()
        self.db.init_db()
        rows = self.db.get_recent_trades()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['execution_key'])
        self.record()
        self.record()
        self.assertEqual(len(self.db.get_recent_trades()), 2)

class TerminalIdentityGate(unittest.TestCase):
    def test_missing_id_and_open_partial_do_not_record_or_advance(self):
        import p0_1_harness as H
        from test_p0_1_scalp_tp import phase1
        for state, order_id in [('FILLED', None), ('OPEN', 'tp1')]:
            with self.subTest(state=state):
                ns = H.load_bot(qty=0.5, bid=101.6)
                phase1(ns, tp_id='tp1', stop_id=None)
                prior = ns['db'].get_position_state('BTC/USD')
                fs = dict(state=state, id=order_id, filled_qty=0.5, avg_price=101.5)
                ns['_scalp_advance_after_target1'](prior, 100., prior['entry_time'], 100., None, 'tp1', fs)
                self.assertEqual(ns['db'].trades, [])
                self.assertEqual(ns['db'].get_position_state('BTC/USD'), prior)

if __name__ == '__main__': unittest.main()
