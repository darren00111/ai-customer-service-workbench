"""One reproducible offline regression report; no shop login or model calls."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time
import unittest
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
MODULES=('selftest_channel_pilots','selftest_pdd_wire','selftest_qianniu','selftest_catalog_capture',
         'selftest_product_cards','selftest_history_context','selftest_store_automation','selftest_handoff_outbox',
         'selftest_channel_worker','selftest_channel_lifecycle','selftest_multichannel',
         'selftest_reception','selftest_learning','selftest_commerce','selftest_p0_loop','selftest_workspace',
         'selftest_experience','selftest_sandbox','selftest_merchant_edits')


def main():
    suites=[]
    for name in MODULES:
        started=time.monotonic()
        result=unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(name)))
        row={'suite':name,'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
             'skipped':len(result.skipped),'seconds':round(time.monotonic()-started,2),'passed':result.wasSuccessful()}
        suites.append(row)
        print(json.dumps(row),flush=True)
    sources={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((ROOT/'app').rglob('*')) if p.is_file() and p.suffix in ('.py','.js','.html','.css','.json')}
    for name in ('desktop_main.py','build_desktop.ps1'):
        sources[name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    report={'verified_at':datetime.now(timezone.utc).isoformat(),'kind':'offline_synthetic_regression',
            'live_shop_acceptance':False,'actual_model_calls':False,'customer_messages_sent':False,
            'passed':all(r['passed'] for r in suites),'suites':suites,'source_sha256':sources}
    target=ROOT/'docs/verification/channel-runtime-regression.json'
    target.parent.mkdir(exist_ok=True);target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
