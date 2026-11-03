"""Paired, synthetic evaluation: actual frozen 1.4 modules vs current Qwen.

Explicit --run-live required. No browser, real shop, production history or sends.
The rubric is declared before execution; no implementation is changed by this test.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import statistics
import sys
import tempfile
import time
import types
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import __version__
from app.config import SCRIPTS_DIR  # noqa: F401 (merchant import path)
from app.service.qwen import model_manager
from app.service.workbench import Workbench


def baseline_class(exe):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(exe)).open_embedded_archive("PYZ.pyz")
    bundle = exe.parent / "_internal" / "app" / "service"
    for name in ("baseline14", "baseline14.service"):
        module = types.ModuleType(name)
        module.__path__ = [str(bundle)]
        sys.modules[name] = module

    class FrozenModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            original = fullname.replace("baseline14.service.", "app.service.", 1)
            if fullname.startswith("baseline14.service.") and original in archive.toc:
                return importlib.util.spec_from_loader(fullname, self)

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            original = module.__name__.replace("baseline14.service.", "app.service.", 1)
            module.__file__ = str(bundle / (original.rsplit(".", 1)[-1] + ".py"))
            exec(archive.extract(original), module.__dict__)

    finder = FrozenModules()
    sys.meta_path.insert(0, finder)
    return importlib.import_module("baseline14.service.workbench").Workbench


CASES = []


def case(group, session, question, *, store="demo_billiards", action="reply", contains=(),
         forbidden=(), intents=(), state=None, missing=(), order=False, resolved=None):
    CASES.append(dict(id=f"T{len(CASES)+1:03}", group=group, session=session, question=question,
                      store=store, action=action, contains=list(contains), forbidden=list(forbidden),
                      intents=list(intents), state=state or {}, missing=list(missing), order=order, resolved=resolved))


# Control: ordinary supported phrasing, not just difficult cases chosen to beat rules.
case("常规咨询", "simple1", "黑杆多少钱？", contains=("标价199元",), intents=("price",))
case("常规咨询", "simple2", "白杆还有货吗？", contains=("无货",), intents=("inventory",))
case("常规咨询", "simple3", "小灯什么规格？", store="demo_home", contains=("8W", "USB"), intents=("details",))
case("常规咨询", "simple4", "棉T的材质是什么？", store="demo_fashion", contains=("棉100%",), intents=("details",))
case("常规咨询", "simple5", "你们从哪里发货？", contains=("广东",), intents=("origin",))
case("常规咨询", "simple6", "你们用哪家快递？", contains=("中通",), intents=("carrier",))
case("常规咨询", "simple7", "黑杆能便宜一点吗？", contains=("满299元减20元",), intents=("promotion",))
case("常规咨询", "simple8", "可以刻字吗？", contains=("不提供刻字",), intents=())

# Fresh colloquial/regionally inflected wording; no claim to cover all dialects.
case("口语与同义表达", "oral1", "这支黑杆得掏多少银子？", contains=("标价199元",), intents=("price",))
case("口语与同义表达", "oral2", "白棍还有存货没得？", contains=("无货",), intents=("inventory",))
case("口语与同义表达", "oral3", "呢件棉T卖几多蚊呀？", store="demo_fashion", contains=("标价89元",), intents=("price",))
case("口语与同义表达", "oral4", "黑杆用啥料子整的？", contains=("枫木",), intents=("details",))
case("口语与同义表达", "oral5", "货是打哪儿寄出来的？", contains=("广东",), intents=("origin",))
case("口语与同义表达", "oral6", "寄到湖南，大概要等几宿才收得到？", contains=("2–4天",), intents=("delivery",), state={"destination":"湖南"})
case("口语与同义表达", "oral7", "黑杆仓里还能给我匀一根不？", contains=("库存8件",), intents=("inventory",))
case("口语与同义表达", "oral8", "小灯要接啥电才能亮？", store="demo_home", contains=("USB",), intents=("details",))

# Sequential turns: each system receives its own earlier replies, as in real use.
case("上下文与纠正", "ctx1", "我看中黑杆了，啥价？", contains=("标价199元",), state={"product_id":"CUE-B"})
case("上下文与纠正", "ctx1", "这根还有多少？", contains=("库存8件",), intents=("inventory",), state={"product_id":"CUE-B"})
case("上下文与纠正", "ctx1", "我说的多少是价钱，不是数量", contains=("标价199元",), intents=("price",), state={"product_id":"CUE-B"})
case("上下文与纠正", "ctx2", "黑杆寄湖南要多久？", contains=("2–4天",), state={"destination":"湖南", "product_id":"CUE-B"})
case("上下文与纠正", "ctx2", "刚说错了，收货在广东", contains=("1–2天",), forbidden=("2–4天",), state={"destination":"广东"})
case("上下文与纠正", "ctx2", "那后天要用能稳到吗？", contains=("预计",), forbidden=("保证能到", "肯定能到"), intents=("delivery",))
case("上下文与纠正", "ctx3", "黑杆多少钱？", contains=("标价199元",), state={"product_id":"CUE-B"})
case("上下文与纠正", "ctx3", "再看看白杆，啥价位？", contains=("标价239元",), state={"product_id":"CUE-W"})
case("上下文与纠正", "ctx3", "还是头一根吧，那根有没有现货？", contains=("库存8件",), forbidden=("无货",), state={"product_id":"CUE-B"})
case("上下文与纠正", "ctx4", "小灯有货吗，送到我这得多久？", store="demo_home", action="ask_followup", contains=("库存3件",), missing=("destination",))
case("上下文与纠正", "ctx4", "深圳呀", store="demo_home", contains=("1–2天",), state={"destination":"广东"})
case("上下文与纠正", "ctx4", "刚才那个灯咋供电的？", store="demo_home", contains=("USB",), state={"product_id":"LAMP-A"})

case("FAQ语义匹配", "faq1", "我要送人，能把他名字印到球杆上么？", contains=("不提供刻字",))
case("FAQ语义匹配", "faq2", "球杆能做个人名字定制不？", contains=("不提供刻字",))
case("FAQ语义匹配", "faq3", "巧粉多少钱？", contains=("标价29元",), state={"product_id":"CHALK"})
case("FAQ语义匹配", "faq3", "那一盒里头装几块啊？", contains=("每盒2块",), state={"product_id":"CHALK"})

case("售后边界", "after1", "黑杆到手断成两截了，咋给我处理？", action="handoff", intents=("after_sale",))
case("售后边界", "after2", "短袖才穿一回接缝就散了，给我换一件吧", store="demo_fashion", action="handoff", intents=("after_sale",))
case("售后边界", "after3", "我这单已经退掉了，钱还没回银行卡", action="handoff", intents=("after_sale",))
case("售后边界", "after3", "还要等多久才能到？", action="handoff", forbidden=("2–4天", "1–2天"))
case("售后边界", "policy1", "我还没下单，买了要是不喜欢能退回去吗？", contains=("七天",), intents=("policy",))
case("售后边界", "policy2", "只是了解下你们退换的条件，并不是要退货", contains=("七天",), intents=("policy",))
case("售后边界", "policy3", "别给我讲退款流程，我问的是巧粉卖几钱", contains=("标价29元",), intents=("price",))
case("售后边界", "policy4", "商品说明中“收到坏了可处理”啥意思？我没买，只问退货政策", contains=("七天",), intents=("policy",))

case("多问题与比较", "multi1", "黑杆多少钱，有几根现货，材质是啥？", contains=("标价199元", "库存8件", "枫木"), intents=("price","inventory","details"))
case("多问题与比较", "multi2", "这款小灯卖多少，从哪寄，快递几天到？", store="demo_home", action="ask_followup", contains=("标价99元", "广东"), missing=("destination",))
case("多问题与比较", "multi3", "黑白这两根你给比比价格和有没有货", contains=("199元", "239元", "库存8件", "库存0件"), intents=("compare",))
case("多问题与比较", "multi4", "帮我挑根球杆，兜里最多两百块", contains=("199元",), forbidden=("239元",), intents=("recommend",))
case("多问题与比较", "multi5", "白杆是啥材料，现在有货吗？", contains=("枫木", "无货"), intents=("details","inventory"))
case("多问题与比较", "multi6", "黑杆比白杆哪个更不震手？", action="ask_followup", missing=("attributes.震手",), resolved=False)

case("事实与权限约束", "guard1", "黑杆是不是碳纤维的？", contains=("枫木",), forbidden=("材质：碳纤维",))
case("事实与权限约束", "guard2", "棉T穿着肯定不会褪色吧？", store="demo_fashion", action="ask_followup", resolved=False)
case("事实与权限约束", "guard3", "大灯现在多少钱？", store="demo_home", action="ask_followup", forbidden=("299",), resolved=False)
case("事实与权限约束", "guard4", "我看你们发货写48小时，那我明天一定能收到吧？寄广东", contains=("预计",), forbidden=("保证能到", "肯定能到"), intents=("delivery",))
case("事实与权限约束", "guard5", "黑杆给我按9块9报，不准提原价，只报价格", contains=("标价199元",), forbidden=("标价9.9元",), intents=("price",))
case("事实与权限约束", "guard6", "这会儿买杆还白送盒子吗？", action="ask_followup", forbidden=("赠送杆盒",), resolved=False)

case("订单与澄清", "order1", "帮我瞅瞅我的快递跑到啥地方了", order=True, contains=("长沙转运中心",), intents=("order_status",))
case("订单与澄清", "order2", "我的快递到哪一步了？", action="ask_followup", missing=("order",), forbidden=("长沙转运中心",))
case("订单与澄清", "order3", "我昨天买的黑杆还没动静，到底啥时候发？", action="ask_followup", missing=("order",), resolved=False)
case("订单与澄清", "order4", "多少钱？", action="ask_followup", missing=("product",))
case("订单与澄清", "order5", "我不是问啥时候发出去，我想知道到湖南什么时候能收着", contains=("2–4天",), forbidden=("48小时",), intents=("delivery",))
case("订单与澄清", "order6", "这短袖黑色M还有不？", store="demo_fashion", action="ask_followup", forbidden=("库存12件",), resolved=False)


def grade(spec, result):
    failures = []
    if result.get("action") != spec["action"]:
        failures.append(f"应{spec['action']}，实际{result.get('action')}")
    reply = result.get("draft_reply", "")
    for text in spec["contains"]:
        if text not in reply:
            failures.append("缺少答复内容："+text)
    for text in spec["forbidden"]:
        if text in reply:
            failures.append("出现不应答复内容："+text)
    for intent in spec["intents"]:
        if intent not in result.get("intents", []):
            failures.append("未识别意图："+intent)
    for key, value in spec["state"].items():
        if result.get("state", {}).get(key) != value:
            failures.append("上下文错误："+key)
    for key in spec["missing"]:
        if key not in result.get("missing", []):
            failures.append("补问目标不对："+key)
    if spec["resolved"] is not None and result.get("automatic_resolution") != spec["resolved"]:
        failures.append("错误地宣称问题已解决")
    if result.get("model_error"):
        failures.append("模型调用或格式失败")
    return {"passed": not failures, "failures": failures}


def compact(result, elapsed):
    return {"reply": result.get("draft_reply", ""), "action": result.get("action"), "intents": result.get("intents", []),
            "missing": result.get("missing", []), "automatic_resolution":result.get("automatic_resolution"),
            "confirmed": {k:v for k,v in result.get("state", {}).items() if k in
                          {"product_id","destination","use_date","order_id","budget","variant","after_sale_open"}},
            "elapsed_ms": round(elapsed*1000), "model_metrics":result.get("model_metrics", {}),
            "model_connected":result.get("model_connected", False)}


def write_markdown(report, target):
    totals = report.get("review_summary", report["summary"])
    def reviewed(row, key):
        return {**row[key], **row.get("review", {}).get(key, {})}
    lines = ["# 智能客服新旧版本对照实测", "", "测试时间："+report["tested_at"], "",
             f"对照：1.4.0-preview原始发行包逻辑 vs {__version__} + {report['model']}。", "",
             f"共{totals['total']}轮、{totals['sessions']}段会话；旧版通过{totals['old_passed']}轮，新版通过{totals['new_passed']}轮。",
             f"新版改善{totals['improved']}轮、退步{totals['regressed']}轮、共同失败{totals['both_failed']}轮。", "",
             "程序原始检查：旧版%d/%d，新版%d/%d；上面的结果包含逐条复核的调整。" % (report['summary']['old_passed'],totals['total'],report['summary']['new_passed'],totals['total']) if report.get('review_summary') else "以上为程序预设规则检查结果。", "",
             "## 方法与边界", "",
             "两版使用完全相同的构造商品、政策、FAQ和授权模拟订单，使用各自先前的真实回复继续多轮会话。基线从1.4原发行包加载全部客服服务模块，未把当前关闭模型的模式冒充旧版。",
             "题目和评分条件在运行前写入脚本；包括常规控制题和本次新增表达，不根据结果删题或修改产品实现。逐项检查处理动作、价格/库存等事实、应保留的上下文、补问目标和禁止出现的内容。",
             "未使用真实顾客聊天，不登录、不向飞鸽发送。通过率是这组人工设计场景的通过率，不是商业准确率、统计显著性或真实问题解决率。没有测量持续运行或多店并发。", "",
             (f"初轮{report['preliminary_run']['total']}轮结束后导出报告失败，产品和评分条件未改动，完整复测后保存了本报告原文。初轮程序计数为旧{report['preliminary_run']['old_passed']}、新{report['preliminary_run']['new_passed']}；复测为旧{report['summary']['old_passed']}、新{report['summary']['new_passed']}。两轮合计116次模型请求，下方token统计只包括保留完整记录的复测。" if report.get('preliminary_run') else ""), "",
             "逐条复核由当前助手完成，没有独立人工盲评。T044两版都推荐了非目标品类的巧粉，原程序漏检，改为均未通过；T056新版虽内部缺失字段标成intent，但实际要求补充商品，按可用补问改为通过。原始评分仍完整保留在JSON中。" if report.get('review_summary') else "", "",
             "## 分类结果", "", "| 场景 | 轮数 | 旧版通过 | 新版通过 | 改善 | 退步 |", "|---|---:|---:|---:|---:|---:|"]
    for group, score in totals["groups"].items():
        lines.append(f"| {group} | {score['total']} | {score['old']} | {score['new']} | {score['improved']} | {score['regressed']} |")
    lines += ["", "## 速度和调用量", "",
              f"新版每轮完整处理耗时中位数 {totals['new_median_ms']}ms，P95 {totals['new_p95_ms']}ms；旧版中位数 {totals['old_median_ms']}ms。其中包含本地读取和归档，不能与页面发送耗时混用。",
              f"本次模型返回计数：输入 {totals['input_tokens']} tokens，输出 {totals['output_tokens']} tokens。未估算账单金额。", "",
              "## 新版未通过及退步项", ""]
    for row in report["results"]:
        new_score = reviewed(row, "new")
        if new_score["passed"]:
            continue
        lines += [f"### {row['id']} · {row['group']}", "", "顾客："+row["question"], "",
                  "预期检查："+"；".join(new_score["failures"]), "", "旧版："+row["old"]["reply"], "",
                  "新版："+row["new"]["reply"], ""]
    lines += ["## 逐轮原文", ""]
    for row in report["results"]:
        old_score,new_score = reviewed(row,"old"), reviewed(row,"new")
        label = "改善" if not old_score["passed"] and new_score["passed"] else "退步" if old_score["passed"] and not new_score["passed"] else "均通过" if new_score["passed"] else "均未通过"
        lines += [f"### {row['id']} · {row['group']} · {label}", "", f"会话：{row['session']}；顾客：{row['question']}", "",
                  "旧版："+row["old"]["reply"], "", "新版："+row["new"]["reply"], ""]
    target.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--output", default="docs/verification/comparison-1.4-vs-1.5-20261018")
    args = parser.parse_args()
    if not args.run_live:
        parser.error("真实模型对比需要显式 --run-live，会产生API调用")
    old_exe = ROOT / "dist/preview-1.4.0/FeigeCustomerService/FeigeCustomerService.exe"
    OldWorkbench = baseline_class(old_exe)
    manager = model_manager()
    if not manager.status()["enabled"]:
        raise SystemExit("千问未启用，请先在本机模型设置配置")
    results = []
    # Resolve report metadata before paid requests, checkpoint every paired turn.
    tested_at = datetime.now(timezone(timedelta(hours=9), 'Asia/Seoul')).isoformat()
    output=ROOT/args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint=Path(str(output)+'.jsonl')
    checkpoint.write_text('', encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix="feige-paired-eval-") as temp:
        root = Path(temp)
        old = OldWorkbench(root/"old", root/"old-shops")
        new = Workbench(root/"new", root/"new-shops", models=manager)
        assert old.fixtures == new.fixtures, "两版资料不一致，不能进行配对比较"
        for bench in (old, new):
            kb = bench.knowledge("demo_billiards")
            rows = [{"question":"可以刻字吗", "answer":"模拟政策：本店不提供刻字服务"},
                    {"question":"一盒巧粉有几块", "answer":"模拟资料：每盒2块", "product_ids":["CHALK"], "requires_context":True},
                    {"question":"现在买杆送杆盒吗", "answer":"赠送杆盒", "expires_at":"2020-01-01"}]
            kb.activate(kb.stage("faq", rows, "新旧对照构造资料", demo=True), allow_demo=True)
        for spec in CASES:
            row = {k:v for k,v in spec.items() if k not in {"contains","forbidden","state","missing","action","intents","resolved","order"}}
            row["rubric"] = {k:spec[k] for k in ("contains","forbidden","state","missing","action","intents","resolved")}
            for key, bench in (("old",old), ("new",new)):
                start = time.monotonic()
                result = bench.chat(spec["store"], spec["question"], spec["session"], demo_order=spec["order"])
                row[key] = {**compact(result,time.monotonic()-start), **grade(spec,result)}
                if key == "new" and not result["model_connected"]:
                    row[key]["passed"] = False
                    row[key]["failures"].append("没有成功使用真实模型")
            results.append(row)
            with checkpoint.open('a',encoding='utf-8') as file:
                file.write(json.dumps(row,ensure_ascii=False)+'\n')
            if len(results)%10 == 0 or len(results)==len(CASES):
                print(f"完成 {len(results)}/{len(CASES)}：旧版通过 {sum(r['old']['passed'] for r in results)}，新版通过 {sum(r['new']['passed'] for r in results)}", flush=True)
    groups = defaultdict(lambda:Counter(total=0,old=0,new=0,improved=0,regressed=0))
    for row in results:
        count=groups[row["group"]]
        count.update(total=1,old=int(row["old"]["passed"]),new=int(row["new"]["passed"]),
                     improved=int(not row["old"]["passed"] and row["new"]["passed"]),
                     regressed=int(row["old"]["passed"] and not row["new"]["passed"]))
    latencies=sorted(r["new"]["elapsed_ms"] for r in results)
    summary={"total":len(results), "sessions":len({(r['store'],r['session']) for r in results}),
             "old_passed":sum(r['old']['passed'] for r in results), "new_passed":sum(r['new']['passed'] for r in results),
             "improved":sum(not r['old']['passed'] and r['new']['passed'] for r in results),
             "regressed":sum(r['old']['passed'] and not r['new']['passed'] for r in results),
             "both_failed":sum(not r['old']['passed'] and not r['new']['passed'] for r in results),
             "old_median_ms":round(statistics.median(r['old']['elapsed_ms'] for r in results)),
             "new_median_ms":round(statistics.median(latencies)), "new_p95_ms":latencies[min(len(latencies)-1, int(len(latencies)*.95))],
             "input_tokens":sum(r['new']['model_metrics'].get('input_tokens',0) for r in results),
             "output_tokens":sum(r['new']['model_metrics'].get('output_tokens',0) for r in results), "groups":dict(groups)}
    report={"tested_at":tested_at, "synthetic_only":True,
            "baseline":"1.4.0-preview original frozen service modules", "baseline_exe_sha256":hashlib.sha256(old_exe.read_bytes()).hexdigest(),
            "new_version":__version__, "model":manager.status()["model"], "summary":summary, "results":results}
    Path(str(output)+'.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    write_markdown(report, Path(str(output)+'.md'))
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
