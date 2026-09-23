import sys,json
sys.path.insert(0,'tests')
from test_draft_r1v2_resolution import escalated_never_sent,pending,H
ns,T=escalated_never_sent();ns['_broker'].qty=0.0
T[0]=1000.0;H.cycle(ns)
print(json.dumps({'broker_qty':ns['_broker'].qty,'pending_after_manual_flat':bool(pending(ns)),'breaker_completion_marks':ns['_marks']}))
