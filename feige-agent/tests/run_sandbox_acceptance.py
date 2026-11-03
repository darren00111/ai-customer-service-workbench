"""Reproducible A/B acceptance. Separate from real-model/user/platform claims."""
import argparse
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


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path,
        default=ROOT/'docs/verification/sandbox-onboarding-acceptance.json')
    args=parser.parse_args();rows=[]
    for module in ('selftest_experience','selftest_sandbox'):
        start=time.monotonic()
        result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(module)))
        rows.append({'suite':module,'tests':result.testsRun,'failures':len(result.failures),
                     'errors':len(result.errors),'skipped':len(result.skipped),'passed':result.wasSuccessful(),
                     'seconds':round(time.monotonic()-start,2)})
    paths=['app/service/experience.py','app/channels/sandbox.py','app/ui/experience.html',
           'app/ui/experience.js','app/ui/experience.css','tests/selftest_experience.py','tests/selftest_sandbox.py']
    report={'kind':'offline_synthetic_acceptance','verified_at':datetime.now(timezone.utc).isoformat(),
            'passed':all(row['passed'] for row in rows),'suites':rows,
            'actual_model_calls':False,'customer_messages_sent':False,'platform_compatibility_verified':False,
            'novice_user_acceptance':{'state':'not_run','participants':0},
            'soak_24h':{'state':'not_run'},'semantic_quality':{'state':'not_run'},
            'source_sha256':{path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in paths}}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'suites':rows,'report':str(args.report)},ensure_ascii=True))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
