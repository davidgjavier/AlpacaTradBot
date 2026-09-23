"""Wrapper that runs Codex's six Option-1 regressions VERBATIM (review/candidate_option1_v2/codex_regressions/
test_option1_candidate.py, sha256 b3a609a9...). The only addition is the gate: skipped unless CANDIDATE_OPT1=1
(with BOT_SOURCE set), so the committed-bot suite is unaffected."""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_p = HERE.parent / "review" / "candidate_option1_v2" / "codex_regressions" / "test_option1_candidate.py"
_spec = importlib.util.spec_from_file_location("codex_option1_regressions_verbatim", _p)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Option1 = unittest.skipUnless(os.environ.get("CANDIDATE_OPT1") == "1", "Codex Option-1 regressions")(_mod.Option1)
