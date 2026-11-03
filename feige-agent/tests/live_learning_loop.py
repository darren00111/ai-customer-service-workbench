"""Explicit real-model replay of a merchant correction, review and new phrasing."""
import json
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.service.workbench import Workbench
from app.service.qwen import model_manager


def main():
    if '--run-live' not in sys.argv:raise SystemExit('需要 --run-live')
    report={'synthetic_only':True,'ok':False}
    with tempfile.TemporaryDirectory(prefix='feige-learn-live-') as temp:
        root=Path(temp);wb=Workbench(root/'reception',root/'shops',models=model_manager())
        before=wb.chat('demo_billiards','可以刻字吗','before')
        assert before['model_connected'] and before['action']=='ask_followup'
        correction=wb.correct('demo_billiards',before['interaction_id'],'本店不提供刻字服务')
        wb.learning_refresh('demo_billiards')
        candidate=wb.propose_correction('demo_billiards',correction['feedback_id'])
        assert not candidate['active']
        evaluation=wb.evaluate('demo_billiards',candidate['id'],use_model=True)
        assert evaluation['passed'] and evaluation['mode']=='model_regression'
        wb.activate('demo_billiards',candidate['id'],True)
        after=wb.chat('demo_billiards','名字能不能刻到这根上？','after')
        print(json.dumps({'after':after['draft_reply'],'understanding':after['understanding']},ensure_ascii=False),flush=True)
        assert after['model_connected'] and '不提供刻字' in after['draft_reply']
        report.update(ok=True,before=before['draft_reply'],after=after['draft_reply'],evaluation=evaluation,
                      learned_revision=candidate['id'],merchant_review_required=True)
    (ROOT/'docs/verification/learning-loop-live-1.6.0.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
