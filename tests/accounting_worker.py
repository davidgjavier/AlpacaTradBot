"""Offline subprocess worker: real SQLite, fake broker, no production imports."""
import importlib.util
import os
from pathlib import Path
import sys
import p0_1_harness as H
from test_p0_1_scalp_tp import phase1

folder, action = Path(sys.argv[1]), sys.argv[2]
spec = importlib.util.spec_from_file_location('isolated_db', folder/'db.py')
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)
if action == 'insert':
    db.log_trade('BTC/USD', 'SCALP', 100., 101.5, 0.5, 'entry', 'exit', 'target1', execution_key='alpaca:order:shared:target1')
    raise SystemExit(0)
ns = H.load_bot(qty=0.5, bid=101.6)
ns['_broker'].add_order('tp1', 'limit', 0.5, status='filled', filled=0.5, avg=101.5)
if action == 'crash':
    phase1(ns, tp_id='tp1', stop_id=None)
    db.set_position_state('BTC/USD', **ns['db'].states['BTC/USD'])
ns['db'] = db
ns['log'] = lambda *args: None
if action == 'crash':
    original = db.log_trade
    def die_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)
    db.log_trade = die_after_commit
state = db.get_position_state('BTC/USD')
fs = ns['get_order_fill_state']('tp1')
ns['_scalp_advance_after_target1'](state, 100.0, state['entry_time'], 100.0, None, 'tp1', fs)
