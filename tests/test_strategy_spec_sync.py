import tempfile
import unittest
from pathlib import Path

import strategies


class StrategySpecSyncTests(unittest.TestCase):
    def test_write_master_spec_snapshot_updates_runtime_values(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / 'ARCHITECTURE_AND_STRATEGY_SPEC.md'
            spec_path.write_text('# Test\n\n## Auto-generated active runtime snapshot\n\nOld snapshot\n', encoding='utf-8')

            original_values = {
                'SHORT_WINDOW': strategies.SHORT_WINDOW,
                'LONG_WINDOW': strategies.LONG_WINDOW,
                'ATR_PERIOD': strategies.ATR_PERIOD,
                'STOP_ATR_MULT': strategies.STOP_ATR_MULT,
                'TARGET_ATR_MULT': strategies.TARGET_ATR_MULT,
                'MACRO_EMA_PERIOD': strategies.MACRO_EMA_PERIOD,
                'RVOL_PERIOD': strategies.RVOL_PERIOD,
                'BB_PERIOD': strategies.BB_PERIOD,
                'RSI_PERIOD': strategies.RSI_PERIOD,
                'SCALP_TP_PCT': strategies.SCALP_TP_PCT,
                'TIME_DECAY_BARS': strategies.TIME_DECAY_BARS,
                'CRYPTO_SPREAD_CAP_PCT': strategies.CRYPTO_SPREAD_CAP_PCT,
                'POSITION_SIZE_USD': getattr(strategies, 'POSITION_SIZE_USD', 500),
                'DAILY_LOSS_LIMIT_USD': getattr(strategies, 'DAILY_LOSS_LIMIT_USD', 150),
            }

            try:
                strategies.SHORT_WINDOW = 11
                strategies.LONG_WINDOW = 31
                strategies.ATR_PERIOD = 17
                strategies.STOP_ATR_MULT = 1.8
                strategies.TARGET_ATR_MULT = 2.8
                strategies.MACRO_EMA_PERIOD = 220
                strategies.RVOL_PERIOD = 29
                strategies.BB_PERIOD = 37
                strategies.RSI_PERIOD = 19
                strategies.SCALP_TP_PCT = 1.9
                strategies.TIME_DECAY_BARS = 25
                strategies.CRYPTO_SPREAD_CAP_PCT = 0.004
                strategies.POSITION_SIZE_USD = 600
                strategies.DAILY_LOSS_LIMIT_USD = 200

                strategies.write_master_spec_snapshot(spec_path)
                text = spec_path.read_text(encoding='utf-8')

                self.assertIn('SHORT_WINDOW = 11', text)
                self.assertIn('LONG_WINDOW = 31', text)
                self.assertIn('ATR_PERIOD = 17', text)
                self.assertIn('STOP_ATR_MULT = 1.8', text)
                self.assertIn('TARGET_ATR_MULT = 2.8', text)
                self.assertIn('MACRO_EMA_PERIOD = 220', text)
                self.assertIn('RVOL_PERIOD = 29', text)
                self.assertIn('BB_PERIOD = 37', text)
                self.assertIn('RSI_PERIOD = 19', text)
                self.assertIn('SCALP_TP_PCT = 1.9', text)
                self.assertIn('TIME_DECAY_BARS = 25', text)
                self.assertIn('CRYPTO_SPREAD_CAP_PCT = 0.004', text)
                self.assertIn('POSITION_SIZE_USD = 600', text)
                self.assertIn('DAILY_LOSS_LIMIT_USD = 200', text)
            finally:
                for key, value in original_values.items():
                    setattr(strategies, key, value)



    def test_apply_live_params_refreshes_master_spec(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            spec_path = Path(tmp_dir) / 'ARCHITECTURE_AND_STRATEGY_SPEC.md'
            original_values = {
                'SHORT_WINDOW': strategies.SHORT_WINDOW,
                'LONG_WINDOW': strategies.LONG_WINDOW,
            }
            try:
                strategies.SHORT_WINDOW = 9
                strategies.LONG_WINDOW = 21
                strategies.apply_live_params({'SHORT_WINDOW': 12, 'LONG_WINDOW': 30}, spec_path=spec_path)
                text = spec_path.read_text(encoding='utf-8')
            finally:
                for key, value in original_values.items():
                    setattr(strategies, key, value)

            self.assertIn('SHORT_WINDOW = 12', text)
            self.assertIn('LONG_WINDOW = 30', text)

if __name__ == '__main__':
    unittest.main()
