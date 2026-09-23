import unittest
from test_draft_r1v2_resolution import escalated_hidden, escalated_never_sent, bound, put, pending, H, market_submissions, SYM
from test_stage5_liquidation import restart

class Option1(unittest.TestCase):
 def test_hidden_accepted_order_cannot_be_replaced_by_assertion(self):
  ns,T=escalated_hidden();b=ns['_broker']
  put(ns,bound(pending(ns),'not_placed','opt1'))
  T[0]=1000;H.cycle(ns)
  self.assertEqual(len(market_submissions(b)),1)
  self.assertTrue(pending(ns));self.assertEqual(ns['_marks'],[])
 def test_never_sent_assertion_leaves_identity_and_pending(self):
  ns,T=escalated_never_sent();cid=pending(ns)['client_order_id']
  put(ns,bound(pending(ns),'not_placed','opt1'))
  T[0]=1000;H.cycle(ns)
  self.assertTrue(pending(ns));self.assertEqual(pending(ns)['client_order_id'],cid)
  self.assertEqual(pending(ns)['attempt'],1)
 def test_old_authorized_attempt_cannot_bypass_missing_identity(self):
  ns,T=escalated_never_sent();liq=dict(pending(ns))
  liq.update(client_order_id=None,order_id=None,authorized_attempt=2)
  ns['db'].set_liquidation_state(SYM,**liq)
  count=len(market_submissions(ns['_broker']))
  H.cycle(ns)
  self.assertEqual(len(market_submissions(ns['_broker'])),count)
  self.assertTrue(pending(ns))
 def test_restart_keeps_rejected_assertion_pending(self):
  ns,T=escalated_hidden();put(ns,bound(pending(ns),'not_placed','opt1'))
  T[0]=1000;H.cycle(ns)
  n2=restart(ns);n2['_r1_now']=lambda:100000
  H.cycle(n2)
  self.assertEqual(len(market_submissions(n2['_broker'])),1)
  self.assertTrue(pending(n2));self.assertEqual(n2['_marks'],[])
 def test_manual_flat_is_not_order_reconciliation(self):
  ns,T=escalated_hidden();ns['_broker'].qty=0
  for t in (1000,100000):T[0]=t;H.cycle(ns)
  self.assertTrue(pending(ns));self.assertEqual(ns['_marks'],[])
 def test_late_visible_fill_resolves_without_replacement(self):
  ns,T=escalated_hidden();put(ns,bound(pending(ns),'not_placed','opt1'))
  T[0]=1000;H.cycle(ns)
  ns['_broker'].reveal(fill=True);T[0]=1100;H.cycle(ns)
  self.assertFalse(pending(ns));self.assertEqual(len(market_submissions(ns['_broker'])),1)
