"""Deterministic slot/date handling and source-linked summaries, without an LLM."""
from __future__ import annotations

import uuid
import math
import re
from datetime import date, datetime, timedelta, timezone


def store_today(business: dict) -> date:
    zone = business.get("timezone", "Asia/Shanghai")
    offset = {"Asia/Shanghai": 8, "Asia/Seoul": 9, "UTC": 0}.get(zone, 8)
    return datetime.now(timezone(timedelta(hours=offset))).date()


def chinese_number(value: str) -> float:
    shorthand = re.fullmatch(r"(\d+(?:\.\d+)?)(万|千)", value)
    if shorthand:
        return float(shorthand[1]) * {"万": 10000, "千": 1000}[shorthand[2]]
    try:
        return float(value)
    except ValueError:
        if (re.search(r"[一二三四五六七八九两]{2}|[百千万][一二三四五六七八九两]$", value) or
                any(value.count(unit) > 1 for unit in "十百千万")):
            raise ValueError("口语金额需要确认")
        digits = dict(zip("零一二三四五六七八九两", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))
        total, part, current = 0, 0, 0
        for char in value:
            if char in digits:
                current = digits[char]
            elif char in "十百千":
                part += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
                current = 0
            elif char == "万":
                total += (part + current) * 10000
                part, current = 0, 0
            else:
                raise ValueError("金额格式无效")
        return float(total + part + current)


def extract_conditions(text: str, today: date, allow_date=False) -> dict:
    result = {}
    number = r"[0-9]+(?:\.[0-9]+)?(?:万|千)?|[零一二三四五六七八九十百千万两]+"
    interval = re.search(r"(?:预算|价格)?\s*(%s)\s*(?:元)?\s*(?:到|至|[-~～])\s*(%s)\s*(?:元|块)?" % (number, number), text)
    if not re.search(r"预算|价格|价位|元|块", text):
        interval = None
    cap = re.search(r"(?:不超过|最多|预算上限)\s*(%s)|(%s)\s*(?:元|块)?\s*以内" % (number, number), text)
    if cap and re.match(r"小时|天|日|个|件|码|厘米", text[cap.end():]):
        cap = None
    target = re.search(r"预算\s*(%s)\s*(?:元|块)?" % number, text)
    try:
        if interval:
            low, high = chinese_number(interval[1]), chinese_number(interval[2])
            if low <= high:
                result["budget"] = {"min": low, "max": high}
            else:
                result["budget_needs_confirmation"] = True
        elif cap:
            result["budget"] = {"max": chinese_number(cap[1] or cap[2])}
        elif target:
            result["budget"] = {"target": chinese_number(target[1])}
    except ValueError:
        result["budget_needs_confirmation"] = True
    if any(not math.isfinite(v) for v in result.get("budget", {}).values()):
        result.pop("budget", None)
        result["budget_needs_confirmation"] = True
    if re.search(r"不限预算|预算不限|不限制预算", text):
        result["clear_budget"] = True
        result.pop("budget", None)
    if re.search(r"不赶时间|日期还没定|时间还没定", text):
        result["clear_use_date"] = True
    elif (allow_date or re.search(r"比赛|用|赶|到|收货|收到|出差|生日|之前|截止|要货", text) or
          re.fullmatch(r"(?:大后天|后天|明天|今天|(?:下|本|这)?(?:个)?(?:周|星期|礼拜)[一二三四五六日天]|(?:\d{4}年)?\d{1,2}月\d{1,2}(?:日|号)?|20\d{2}-\d{2}-\d{2})", text.strip())):
        relative = re.search(r"大后天|后天|明天|今天", text)
        weekday = re.search(r"(下|本|这)?(?:个)?(?:周|星期|礼拜)([一二三四五六日天])", text)
        absolute = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})(?:日|号)?", text)
        iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        wanted = None
        try:
            if iso:
                wanted = date.fromisoformat(iso[1])
            elif absolute:
                wanted = date(int(absolute[1] or today.year), int(absolute[2]), int(absolute[3]))
            elif relative:
                wanted = today + timedelta(days={"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[relative[0]])
            elif weekday:
                day = "一二三四五六日天".index(weekday[2])
                day = min(day, 6)
                if weekday[1] in {"下", "本", "这"}:
                    wanted = today - timedelta(days=today.weekday()) + timedelta(days=day + (7 if weekday[1] == "下" else 0))
                else:
                    wanted = today + timedelta(days=(day - today.weekday()) % 7)
            if wanted:
                if wanted < today:
                    result["date_needs_confirmation"] = True
                else:
                    result["use_date"] = wanted.isoformat()
        except ValueError:
            result["date_needs_confirmation"] = True
    return result


def date_bounds(value) -> tuple[int, int] | None:
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    if isinstance(value, list) and len(value) == 2 and all(type(v) in (int, float) for v in value):
        low, high = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        low = high = value
    elif isinstance(value, str):
        match = re.search(r"(\d+)\s*[-–—~～至到]\s*(\d+)\s*天", value)
        hours = re.search(r"(\d+)\s*小时", value)
        days = re.search(r"(\d+)\s*天", value)
        if match:
            low, high = map(int, match.groups())
        elif hours:
            low, high = 0, (int(hours[1]) + 23) // 24
        elif days:
            low, high = 0, int(days[1])
        else:
            return None
    else:
        return None
    if low < 0 or high < low or high > 365:
        return None
    return int(low), int(high)


def summarize(state: dict, question: str, answer: str, history: list) -> None:
    """Summary is a projection of known slots; original turns have stable refs."""
    turn = int(state.get("turn", 0)) + 1
    state["turn"] = turn
    epoch = state.setdefault("memory_id", uuid.uuid4().hex[:12])
    refs = state.setdefault("slot_sources", {})
    keys = ("product_id", "destination", "order_id", "variant", "budget", "budget_unlimited", "use_date", "comparison_products")
    confirmed = {k: state[k] for k in keys if state.get(k) is not None}
    prior = state.get("summary", {}).get("confirmed", {})
    changes = state.setdefault("condition_changes", [])
    for key in keys:
        if confirmed.get(key) != prior.get(key):
            reference = "%s:turn:%d:buyer" % (epoch, turn)
            refs[key] = reference
            changes.append({"field": key, "before": prior.get(key), "after": confirmed.get(key), "reference": reference})
    state["condition_changes"] = changes[-20:]
    state["summary"] = {"confirmed": confirmed, "pending_intents": state.get("pending_intents", []),
                        "after_sale_open": bool(state.get("after_sale_open")),
                        "references": {k: refs[k] for k in confirmed if k in refs},
                        "turns": turn, "method": "structured_projection"}
    state["last_turn"] = {"buyer": {"reference": "%s:turn:%d:buyer" % (epoch, turn), "content": question},
                          "agent": {"reference": "%s:turn:%d:agent" % (epoch, turn), "content": answer}}
    state["messages"] = (history + [{"role": "agent", "content": answer}])[-24:]
