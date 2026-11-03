"""Stateful reception with grounded answers and a replaceable intent interpreter."""
from __future__ import annotations

import copy
import re
import time
from datetime import date, timedelta

from .contracts import Interpreter, SYSTEM_INSTRUCTIONS, Understanding
from .data import DataSource, checked_fact
from .knowledge import KnowledgeStore
from .context import date_bounds, extract_conditions, store_today, summarize
from .retrieval import search_faq, validity
from .grounding import canonical_region, attributes_asked, attribute_key, recommendation_scope

REGIONS = ("黑龙江", "内蒙古", "新疆", "宁夏", "西藏", "广西", "广东", "湖南", "湖北",
           "河南", "河北", "山东", "山西", "江苏", "浙江", "安徽", "福建", "江西", "四川",
           "贵州", "云南", "陕西", "甘肃", "青海", "辽宁", "吉林", "海南", "北京", "上海", "天津", "重庆")
CITY_REGIONS = {"长沙": "湖南", "广州": "广东", "深圳": "广东", "杭州": "浙江", "成都": "四川"}
PRESALE = re.compile(r"(?:还没买|没下单|购买前|买之前|想买|如果|万一|支持|规则|政策)")
AFTERSALE = re.compile(r"(?:退款|退钱|退货|换货|售后|投诉|赔偿|坏了|破了|开线|少发|漏发|发错|货不对板)")
ACTION = re.compile(r"(?:我要|我想|帮我|申请|给我|已经|收到|买了|退一下|想退|要退|想换|要换)")


def offline_understand(text: str, state: dict, products: list[dict]) -> Understanding:
    """Deterministic development fallback. Deliberately NOT a semantic model."""
    query = re.sub(r"\s+", "", text).lower()
    # Drop explicitly negated clauses so corrections do not trigger stale facts.
    query = re.sub(r"(?:不是问|不是要问|不问|别说|不是寄|不寄)[^，。；,;]*(?:[，。；,;]|$)", "", query)
    query = re.sub(r"(?:不是要|不需要|不用|不要|不想|不)(?:申请)?(?:退款|退货|换货|投诉|赔偿)[^，。；,;]*(?:[，。；,;]|$)", "", query)
    result = Understanding()
    matches = []
    for p in products:
        aliases = [p["product_id"], p["title"], *(p.get("aliases") or [])]
        hits = [len(a) for a in aliases if isinstance(a, str) and len(a) >= 2 and a.lower() in query]
        if hits:
            matches.append((max(hits), p["product_id"]))
    matches.sort(reverse=True)
    if matches and (len(matches) == 1 or matches[0][0] > matches[1][0]):
        result.product_id = matches[0][1]
        result.product_reference='named'
    elif matches:
        result.clear_product = True
        result.needs_clarification = True
    if re.search(r"对比|比较|区别|哪个好", query):
        result.product_reference='comparison'
        result.product_ids = [pid for _, pid in matches]
        result.intents.append("compare")
        result.needs_clarification = len(result.product_ids) < 2
        result.clear_product = False
    if re.search(r"前一[个款根件]|上一[个款根件]", query):
        result.product_reference='previous'
        history = state.get("product_history", [])
        if len(history) >= 2 and history[-2] in {p['product_id'] for p in products}:
            result.product_id = history[-2]
        else:
            result.clear_product = True
            result.needs_clarification = True
    if re.search(r"换一[个款根件]|换款|不是这[个款根件]", query) and not result.product_id:
        result.clear_product = True
        result.needs_clarification = True
    region = next((r for r in REGIONS if r in query), None)
    result.destination = region or next((r for city, r in CITY_REGIONS.items() if city in query), None)
    if re.search(r"(?:改寄|换地址|不寄|不是寄)", query) and not result.destination:
        result.clear_destination = True
    if "另一个订单" in query:
        result.clear_order = True
    if re.search(r"转人工|人工客服|找真人", query):
        result.intents = ["human_request"]
        return result
    if AFTERSALE.search(query):
        # Explicit action takes priority even when a policy is mentioned too.
        if ACTION.search(query) and not re.search(r"还没买|没下单|买之前|购买前", query):
            result.intents = ["after_sale"]
            return result
        if PRESALE.search(query) or re.search(r"能退吗|能不能退|可以退吗", query):
            result.intents.append("policy")
        else:
            result.intents = ["after_sale"]
            return result
    patterns = {
        "price": r"多少钱|几钱|啥价|多少米|价格|售价|多钱",
        "inventory": r"有货|现货|库存|有没|还有吗|缺货|有[大小smlx\d]+码",
        "details": r"材质|规格|尺寸|参数|震手|掉色|起球|长度|多长|适用|成分",
        "delivery": r"啥时候到|什么时候到|多久到|几天.*到|到货|来得及|赶得上|用得上|啥时能到",
        "dispatch": r"啥时候发|什么时候发|多久发|发货时间|今天发|明天发",
        "origin": r"哪里发货|哪儿发货|哪里寄|从哪发|发货地",
        "carrier": r"什么快递|哪个快递|哪家快递",
        "promotion": r"便宜|优惠|折扣|活动价",
        "order_status": r"订单状态|发了没|发出了吗|查.*物流|包裹.*哪|快递.*哪",
        "recommend": r"推荐|选一款|选一根|选哪款",
    }
    for name, pattern in patterns.items():
        if re.search(pattern, query):
            result.intents.append(name)
    if "compare" in result.intents:
        result.intents = ["compare"]
    if not result.intents and re.fullmatch(r"你好[呀啊]?|您好|在吗|哈喽|谢谢[啦啊]?|好的", query.strip("！!。？?~～")):
        result.intents = ["greeting"]
    if not result.intents and state.get("pending_intents") and (result.destination or result.product_id):
        result.intents = list(state["pending_intents"])
    result.attribute = next((key for key in ("材质", "规格", "尺寸", "长度", "成分", "震手", "掉色", "起球") if key in query), "")
    size = re.search(r"(?<![a-z])(xxxl|xxl|xl|xs|s|m|l)码", query)
    if size:
        result.variant["size"] = size[1].upper()
    color = next((c for c in ("黑色", "白色", "蓝色", "红色", "米白", "卡其") if c in query), None)
    if color:
        result.variant["color"] = color
    if not result.intents:
        result.intents = ["unknown"]
        result.faq_query = text
    if "recommend" in result.intents or result.intents == ["unknown"]:
        result.product_type = next((p.get("product_type") for p in products if p.get("product_type") and p["product_type"] in query), "")
    result.requested_attributes = attributes_asked(text)
    return result


class ReceptionService:
    def __init__(self, source: DataSource, knowledge: KnowledgeStore | None = None,
                 interpreter: Interpreter | None = None, as_of: date | None = None):
        self.source, self.knowledge, self.interpreter = source, knowledge, interpreter
        self.as_of = as_of
        if knowledge and knowledge.store_id != source.store_id:
            raise ValueError("知识库与数据源必须属于同一店铺")

    def today(self):
        return self.as_of or store_today(self.source.business)

    def versions(self):
        if not self.knowledge:
            return {}
        return {kind: row["id"] for kind in ("products", "business", "faq") if (row := self.knowledge.active(kind))}

    def _request(self, text, state, messages):
        # Full original role-tagged context is carried separately from our slots.
        active = self.knowledge.active("faq") if self.knowledge else None
        candidates = search_faq(text, active["payload"], state.get("product_id"), self.source.business.get("synonyms")) if active else []
        return {"system": SYSTEM_INSTRUCTIONS, "store_id": self.source.store_id,
                "today": self.today().isoformat(), "faq_candidates": candidates,
                "terminology": self.source.business.get("synonyms", {}),
                "messages": messages[-24:], "latest": text,
                "state": {k: v for k, v in state.items() if k not in ("messages","last_turn")},
                "faq_catalog": [{"index":index,**{k: row[k] for k in ("question", "aliases", "product_ids", "requires_context") if k in row}}
                                for index,row in enumerate((active["payload"] if active else [])[:80]) if validity(row) == "valid"],
                "products": [{**{k: p[k] for k in ("product_id", "title", "aliases", "product_type", "category") if k in p},
                              "attribute_keys": list((p.get("attributes") or {}).keys()),
                              "attribute_values": {key: str(value)[:1000] for key,value in (p.get("attributes") or {}).items()}}
                             for p in self.source.products]}

    def respond(self, text: str, state: dict | None = None, messages: list | None = None,
                live: bool = False, authorized_order_ids: list[str] | None = None,
                track_gaps: bool = True, session_id: str = "", platform_product: dict | None = None,
                correction: dict | None = None) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("消息应为 1–8000 字符的文字")
        state = copy.deepcopy(state or {})
        knowledge_versions = self.versions()
        if state.get("store_id", self.source.store_id) != self.source.store_id:
            raise ValueError("会话状态不能跨店复用")
        ttl = self.source.business.get("reception", {}).get("context_ttl_seconds", 3600)
        try:
            ttl = max(60, float(ttl))
        except (ValueError, TypeError):
            ttl = 3600
        expired = bool(state.get("last_activity") and time.time() - state["last_activity"] > ttl)
        if expired:
            state = {"after_sale_open": True} if state.get("after_sale_open") else {}
        state["store_id"] = self.source.store_id
        from .platform_context import prepare_reference,apply_reference
        product_reference=prepare_reference(state,self.source.products,platform_product)
        history = [] if expired else copy.deepcopy(messages if messages is not None else state.get("messages", []))[-24:]
        if not history or history[-1].get("content") != text or history[-1].get("role") not in {"buyer", "user"}:
            history.append({"role": "buyer", "content": text})
        mode = getattr(self.interpreter, "name", "offline_rules")
        model_error = False
        model_status = ""
        try:
            request = self._request(text, state, history) if self.interpreter else None
            if request is not None:
                request["authorized_order_ids"] = authorized_order_ids or []
            understanding = (Understanding.from_dict(self.interpreter.understand(request),
                              {p["product_id"] for p in self.source.products}) if self.interpreter
                             else offline_understand(text, state, self.source.products))
        except Exception as exc:
            # No exception details: a future vendor SDK could include credentials.
            model_error = True
            from .qwen import ModelError
            model_status = str(exc) if isinstance(exc, ModelError) else "模型理解失败，本轮未发送"
            understanding = Understanding(intents=["unknown"], needs_clarification=True)
        # Protect explicit actions without letting bare aftersales keywords
        # override a successful semantic reading of policy/quoted/negated text.
        boundary = offline_understand(text, state, self.source.products)
        action_text = re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"|‘[^’]*’', "", text)
        explicit_after_sale = re.search(r"(?:我要|我想|帮我|给我)(?:申请)?(?:退货|退款|退钱|换货|退一下|退掉)|(?:收到|买了|到手).{0,15}(?:坏|破|裂|发错|漏发)", action_text)
        force_boundary = (self.interpreter is None or model_error or explicit_after_sale or
                          ("human_request" in boundary.intents and action_text == text))
        if force_boundary and set(boundary.intents) & {"after_sale", "human_request"}:
            understanding = boundary
        apply_reference(understanding,state,product_reference,text)
        if correction:
            from .overrides import OverrideBook
            state=OverrideBook.apply_to_state(state,correction)
            if 'product_id' in correction:
                understanding.product_id=correction['product_id']
                understanding.clear_product=not bool(correction['product_id'])
                understanding.variant={}
            if correction.get('intent') and not explicit_after_sale:
                understanding.intents=[correction['intent']]
                understanding.needs_clarification=False
        if "price" in understanding.intents and "promotion" in boundary.intents:
            understanding.intents.append("promotion")
        understanding.intents = list(dict.fromkeys(understanding.intents))
        understanding.destination = canonical_region(understanding.destination, self.source.business.get("shipping"))
        if state.get("destination"):
            state["destination"] = canonical_region(state["destination"], self.source.business.get("shipping"))
        if understanding.product_type:
            state["product_type"] = understanding.product_type
        if set(understanding.intents) & {"details", "compare"}:
            understanding.requested_attributes = list(dict.fromkeys(understanding.requested_attributes + attributes_asked(text)))[:8]
        date_context = set(understanding.intents) <= {"unknown", "delivery"} and bool(state.get("use_date") or "delivery" in state.get("pending_intents", []))
        conditions = extract_conditions(text, self.today(), allow_date=date_context)
        if conditions.get("clear_budget") or understanding.clear_budget:
            state.pop("budget", None)
            state["budget_unlimited"] = bool(conditions.get("clear_budget"))
        if conditions.get("clear_use_date") or understanding.clear_use_date:
            state.pop("use_date", None)
        budget = conditions.get("budget") or understanding.budget
        if budget:
            state["budget"] = budget
            state.pop("budget_unlimited", None)
            state.pop("budget_needs_confirmation", None)
        if conditions.get("budget_needs_confirmation"):
            state.pop("budget", None)
            state["budget_needs_confirmation"] = True
        use_date = conditions.get("use_date") or understanding.use_date
        if use_date:
            state["use_date"] = use_date
        if conditions.get("date_needs_confirmation"):
            state.pop("use_date", None)
            state["date_needs_confirmation"] = True
        elif use_date or conditions.get("clear_use_date"):
            state.pop("date_needs_confirmation", None)
        if understanding.intents == ["unknown"] and (budget or use_date):
            understanding.intents = list(state.get("pending_intents") or (["delivery"] if use_date else ["recommend"]))
        if understanding.product_ids:
            state["comparison_products"] = understanding.product_ids
        if not understanding.faq_query:
            understanding.faq_query = text
        if understanding.faq_index is not None and self.knowledge:
            faq=self.knowledge.active('faq')
            if faq and understanding.faq_index<len(faq['payload']):
                understanding.faq_query=faq['payload'][understanding.faq_index]['question']
        for slot, clear in (("product_id", "clear_product"), ("destination", "clear_destination"), ("order_id", "clear_order")):
            if getattr(understanding, clear):
                state.pop(slot, None)
            if getattr(understanding, slot):
                if slot == "product_id" and getattr(understanding, slot) != state.get(slot):
                    state.pop("variant", None)
                state[slot] = getattr(understanding, slot)
        if understanding.clear_product:
            state.pop("variant", None)
        if understanding.variant:
            state.setdefault("variant", {}).update(understanding.variant)
        if understanding.product_id:
            visited = state.setdefault("product_history", [])
            if not visited or visited[-1] != understanding.product_id:
                visited.append(understanding.product_id)
                del visited[:-8]
        intents = understanding.intents
        if "compare" in intents and len(state.get("comparison_products", [])) >= 2:
            # The comparison action already queries price/stock/attributes for
            # each selected item; do not ask for a spurious single product.
            intents = [i for i in intents if i not in {"price", "inventory", "details"}]
            understanding.intents = intents
        demo_data = self.source.demo or bool(self.knowledge and any(
            row and row["demo"] for kind in ("products", "business", "faq") if (row := self.knowledge.active(kind))))
        result = {"mode": mode, "model_connected": self.interpreter is not None and not model_error,
                  "demo": bool(demo_data), "understanding": understanding.to_dict(),
                  "evidence": [], "missing": [], "answers": [], "pending_intents": [],
                  "automatic_resolution": False, "can_send": not model_error and not (live and demo_data),
                  "model_error": model_error, "query_errors": [], "retrieval_candidates": []}
        result["model_status"] = model_status
        result["model_metrics"] = getattr(self.interpreter, "last_metrics", {}) if not model_error else {}
        result["reference_date"] = self.today().isoformat()
        result["context_expired"] = expired
        if correction and correction.get('action')=='keep_draft':result['can_send']=False
        result["knowledge_versions"] = knowledge_versions
        if self.knowledge:
            active_faq = self.knowledge.active("faq")
            if active_faq:
                result["retrieval_candidates"] = search_faq(text, active_faq["payload"], state.get("product_id"), self.source.business.get("synonyms"))
        if state.get("human_override") == "handoff":
            # A human took over: the tool may draft context but never auto-sends.
            result.update(action="handoff", category="handoff_human_override", can_send=False,
                          draft_reply="该会话已由人工接管。", confidence="high")
        elif "after_sale" in intents or state.get("after_sale_open"):
            state["after_sale_open"] = True
            result.update(action="handoff", category="handoff_after_sale",
                          draft_reply="您的售后问题需要人工处理，已记录当前情况。", confidence="high")
        elif "human_request" in intents:
            result.update(action="handoff", category="handoff_human_request",
                          draft_reply="已记录您需要人工协助的请求。", confidence="high")
        else:
            if live and self.interpreter is None:
                result["can_send"] = False
                result["missing"].append("model_connection")
            # The interpreter may mark an incomplete request as ambiguous.
            # When the missing slot is objectively known, the business queries
            # answer the remaining questions and ask for that precise slot.
            known_missing_slot = (('delivery' in intents and not state.get('destination')) or
                                  ('order_status' in intents and not state.get('order_id')) or
                                  ('recommend' in intents and not state.get('budget')) or
                                  (any(i in {'price','inventory','details'} for i in intents) and not state.get('product_id')))
            unmapped_reference=bool(product_reference and not product_reference['local_product_id'] and
                state.get('platform_reference_overridden')!=product_reference['message_id'])
            for intent in intents:
                try:
                    independent=unmapped_reference and intent in {'policy','dispatch','origin','carrier','promotion','greeting'}
                    answer, evidence, missing = (("", [], ["product" if understanding.clear_product or
                                                          (intent in {"price","inventory","details"} and not state.get("product_id")) else "intent"])
                        if not independent and understanding.needs_clarification and (understanding.clear_product or not known_missing_slot) else
                        self._answer(intent, understanding, state, authorized_order_ids or []))
                    # A store-wide, explicitly selected service FAQ can answer
                    # an otherwise unsupported detail without needing a SKU.
                    if missing and understanding.faq_index is not None and intent in {'details','unknown','faq'}:
                        active=self.knowledge.active('faq') if self.knowledge else None
                        row=active['payload'][understanding.faq_index] if active and understanding.faq_index<len(active['payload']) else None
                        labels=understanding.requested_attributes or ([understanding.attribute] if understanding.attribute else [])
                        relevant=row and all(any(re.sub(r'服务|方式|功能','',label) in form for form in [row['question'],*row.get('aliases',[])]) for label in labels)
                        if relevant:
                            faq_answer,faq_evidence,faq_missing=self._answer('faq',understanding,state,authorized_order_ids or [])
                            if faq_answer and not faq_missing:
                                answer,evidence,missing=faq_answer,faq_evidence,[]
                except Exception:
                    result["query_errors"].append(intent)
                    answer, evidence, missing = "", [], ["data_source_unavailable"]
                if answer:
                    result["answers"].append(answer)
                    result["evidence"].extend(evidence)
                if missing:
                    if unmapped_reference and intent in {'price','inventory','details','compare'}:
                        missing=['platform_product']
                    result["missing"].extend(missing)
                    result["pending_intents"].append(intent)
            result["missing"] = list(dict.fromkeys(result["missing"]))
            if result["pending_intents"]:
                if self.knowledge and track_gaps:
                    question=(text+'\n[待关联平台商品：'+product_reference['platform']+':'+product_reference['remote_product_id']+']') if unmapped_reference else text
                    self.knowledge.gap(question, state.get("product_id"), result["missing"], intents)
                ask = self._clarification(result["missing"])
                result.update(action="ask_followup", category="need_clarification", confidence="medium",
                              draft_reply="\n".join(result["answers"] + [ask]))
            else:
                result.update(action="reply", category="pre_sale_grounded", confidence="high",
                              draft_reply="\n".join(result["answers"]), automatic_resolution=True)
        state["pending_intents"] = result["pending_intents"]
        state["last_activity"] = time.time()
        summarize(state, text, result["draft_reply"], history)
        result["state"] = state
        result["product_id"] = state.get("product_id")
        result["reason"] = "基于资料回答" if result["automatic_resolution"] else "需补充信息或处理"
        result["intents"] = intents
        from .commerce import apply_sales, CommerceLearning
        winner = None
        if self.knowledge and self.source.business.get('sales',{}).get('learning_mode')=='auto':
            winner = CommerceLearning(getattr(self.knowledge,'kb',self.knowledge)).metrics(self.source.business,knowledge_versions,self.source.products)['auto_applied_variant']
        apply_sales(result,self.source.business,self.source.store_id,session_id,knowledge_versions,winner,self.source.products)
        # Keep archived/remembered agent text identical to the final response.
        if state.get('messages') and state['messages'][-1].get('role') in {'agent','assistant'}:
            state['messages'][-1]['content'] = result['draft_reply']
        if state.get('last_turn',{}).get('agent'):
            state['last_turn']['agent']['content'] = result['draft_reply']
        if self.versions() != knowledge_versions:
            result["can_send"] = False
            result["stale_knowledge"] = True
        return result

    @staticmethod
    def _clarification(missing):
        if 'platform_product' in missing:
            return '已收到商品卡片，但这款商品的对应资料尚未同步，暂时不能确认您咨询的信息，已记录资料缺口。'
        if "data_source_unavailable" in missing:
            return "当前查询暂时不可用，这一项还不能确认，请稍后再查询。"
        if "use_date" in missing:
            return "请确认您需要在哪一天使用，提供具体日期即可。"
        if "comparison_products" in missing:
            return "请提供要比较的两款商品名称或卡片。"
        if "budget" in missing:
            return "您大概的预算是多少？我按本店已有资料帮您筛选。"
        if "product_type" in missing:
            return "您想选哪一类商品？告诉我用途或商品名称，我再按预算帮您筛选。"
        attrs = [key.split(".",1)[1] for key in missing if key.startswith("attributes.")]
        if attrs:
            return "关于%s，目前没有可核实的信息，暂时无法确认。" % "、".join(dict.fromkeys(attrs))
        if "product" in missing:
            return "请确认您说的是哪款商品，发送商品名称或卡片即可。"
        if "destination" in missing:
            return "请问收货地是哪个省或城市？"
        if "order" in missing:
            return "请在当前会话关联要查询的订单，我再核对发货和物流信息。"
        if "model_connection" in missing or "intent" in missing:
            return "请补充一下您想了解的商品或具体问题。"
        return "这一项暂时缺少可核实的资料，已记录资料缺口；您还想了解这款的哪些方面？"

    def _answer(self, intent, u, state, authorized_ids):
        product = next((p for p in self.source.products if p["product_id"] == state.get("product_id")), None)
        business = self.source.business
        source_name = "本地已确认资料"

        def fact(value, path):
            provenance = getattr(self.source, "provenance", {}).get(path.split(".", 1)[0], {})
            value, ev = checked_fact(value, path, provenance.get("source") or source_name, self.source.demo)
            if provenance.get("id"):
                ev["revision"] = provenance["id"]
                ev["digest"] = provenance.get("digest")
            return value, [ev]

        if intent == "greeting":
            return "您好，请问想了解哪款商品，或需要查询哪方面的信息？", [], []
        if intent in {"recommend", "compare"}:
            candidates = self.source.products
            if intent == "compare":
                ids = state.get("comparison_products", [])
                candidates = [p for p in candidates if p["product_id"] in ids]
                if len(candidates) < 2:
                    return "", [], ["comparison_products"]
            elif not state.get("budget") and not state.get("budget_unlimited"):
                return "", [], ["budget"]
            if intent == "recommend":
                candidates = recommendation_scope(candidates, u, state)
                if not candidates:
                    return "", [], ["product_type"]
            options, evidence, missing = [], [], []
            budget = state.get("budget", {})
            for item in candidates:
                price, price_ev = fact(item.get("price"), "products.%s.price" % item["product_id"])
                stock, stock_ev = fact(item.get("stock"), "products.%s.stock" % item["product_id"])
                valid_price = type(price) in (int, float) and price >= 0
                valid_stock = type(stock) in (int, float) and stock >= 0
                if intent == "recommend":
                    if not valid_price or not valid_stock:
                        continue
                    if stock <= 0 or price < budget.get("min", 0) or price > budget.get("max", float("inf")):
                        continue
                parts = []
                if valid_price:
                    parts.append("标价%s元" % price)
                    evidence.extend(price_ev)
                else:
                    missing.append("products.%s.price" % item["product_id"])
                if valid_stock:
                    parts.append("资料库存%s件" % stock)
                    evidence.extend(stock_ev)
                else:
                    missing.append("products.%s.stock" % item["product_id"])
                description = item["title"] + "：" + "，".join(parts)
                if intent == "compare":
                    attrs, attr_ev = fact(item.get("attributes"), "products.%s.attributes" % item["product_id"])
                    attrs = attrs if isinstance(attrs, dict) else {}
                    requested = u.requested_attributes or ([u.attribute] if u.attribute else [])
                    selected = {}
                    for wanted in requested:
                        if wanted in {'价格','价钱','售价','库存','有货','数量'}:
                            continue  # Already checked as typed price/stock facts above.
                        key = attribute_key(wanted, attrs)
                        if key is None:
                            missing.append("attributes." + wanted)
                        else:
                            selected[key] = attrs[key]
                    if not requested:
                        selected = attrs
                    for key, value in selected.items():
                        value, ev = fact(value, "products.%s.attributes.%s" % (item["product_id"], key))
                        if value is not None:
                            description += "；%s：%s" % (key, value)
                            evidence.extend(ev + attr_ev)
                        elif requested:
                            missing.append("attributes." + key)
                elif intent=='recommend':
                    attrs,attr_ev=fact(item.get('attributes'),'products.%s.attributes'%item['product_id'])
                    attrs=attrs if isinstance(attrs,dict) else {}
                    for wanted in business.get('sales',{}).get('focus_attributes',[]):
                        key=attribute_key(wanted,attrs)
                        if key:
                            value,ev=fact(attrs[key],'products.%s.attributes.%s'%(item['product_id'],key))
                            if value is not None:
                                description+='；%s：%s'%(key,value)
                                evidence.extend(ev+attr_ev)
                distance=abs(price-budget.get('target',price)) if valid_price else 0
                preferred=business.get('sales',{}).get('preferred_product_ids',[])
                rank=(preferred.index(item['product_id']) if item['product_id'] in preferred else len(preferred),distance) if intent=='recommend' else distance
                options.append((rank, description))
            if not options:
                return "当前可核实的商品资料中，没有找到符合这些条件且有货的商品，您可以调整预算或商品范围。", [], []
            options.sort(key=lambda item: item[0])
            return "；".join(item[1] for item in options[:3]) + "。以上依据本店商品资料，实际以商品页为准。", evidence, list(dict.fromkeys(missing))
        if intent in {"price", "inventory", "details"}:
            if not product:
                return "", [], ["product"]
            key = {"price": "price", "inventory": "stock", "details": "attributes"}[intent]
            raw = product.get(key)
            variant = state.get("variant", {})
            path = "products.%s.%s" % (product["product_id"], key)
            identity_evidence = []
            base_color = False
            if intent in {'price','inventory'} and set(variant)=={'color'} and not product.get('variants'):
                # A named single-colour product is already the selected item;
                # do not demand a nonexistent SKU merely because its title says black.
                color = variant['color']
                named_colors = {c for c in ('黑色','白色','蓝色','红色','灰色','绿色','黄色','粉色','紫色','棕色','橙色','米白','卡其') if c in product['title']}
                attributes = product.get('attributes') or {}
                color_key = next((k for k in ('颜色','color') if k in attributes),None)
                if color_key:
                    value, proof = fact(attributes[color_key],'products.%s.attributes.%s' % (product['product_id'],color_key))
                    base_color = value==color and (not named_colors or named_colors=={color})
                    if base_color: identity_evidence = proof
                elif named_colors=={color}:
                    base_color = True
                    _, identity_evidence = fact(product['title'],'products.%s.title' % product['product_id'])
            if variant and intent in {"price", "inventory"} and not base_color:
                variants = product.get("variants", [])
                matches = [v for v in variants if isinstance(v, dict) and all(v.get(k) == val for k, val in variant.items())]
                if not matches or any(key not in v for v in matches):
                    return "", [], ["variant." + key]
                # Multiple colors/sizes must not be collapsed into one SKU answer.
                if len(matches) != 1:
                    return "", [], ["variant"]
                raw = matches[0][key]
                path = "products.%s.variants.%s.%s" % (product["product_id"], matches[0].get("sku_id", variant), key)
            value, ev = fact(raw, path)
            ev += identity_evidence
            if value is None:
                return "", [], [key]
            if intent in {"price", "inventory"}:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    return "", [], [key]
                if intent == "price":
                    return "%s资料标价%s元，实际成交价以商品页结算为准。" % (product["title"], value), ev, []
                return ("%s资料显示无货。" % product["title"] if value == 0 else
                        "%s资料显示库存%s件，具体规格和可购买数量以商品页为准。" % (product["title"], value)), ev, []
            if not isinstance(value, dict):
                return "", [], ["attributes"]
            requested = u.requested_attributes or ([u.attribute] if u.attribute else [])
            selected, missing = {}, []
            for wanted in requested:
                resolved = attribute_key(wanted, value)
                if resolved is None:
                    missing.append("attributes." + wanted)
                else:
                    selected[resolved] = value[resolved]
            if not requested:
                selected = value
            parts = []
            for k, v in selected.items():
                v, attr_ev = fact(v, path + "." + k)
                if v is None:
                    return "", [], ["attributes." + k]
                parts.append("%s：%s" % (k, v))
                ev.extend(attr_ev)
            return ("%s：%s。" % (product["title"], "；".join(parts)) if parts else ""), ev, missing
        if intent in {"order_status", "delivery", "dispatch"} and state.get("order_id"):
            order = self.source.order(state["order_id"], authorized_ids)
            if not order:
                return "", [], ["order"]
            key = "status" if intent == "order_status" else ("delivery_estimate" if intent == "delivery" else "dispatch_estimate")
            value, ev = fact(order.get(key), "orders.%s.%s" % (state["order_id"], key))
            return (("该订单资料显示：%s。" % value, ev, []) if value is not None else ("", [], [key]))
        if intent == "order_status":
            return "", [], ["order"]
        if intent in {"delivery", "dispatch", "origin", "carrier"}:
            shipping = business.get("shipping", {})
            key = {"delivery": "delivery_estimate", "dispatch": "lead_time", "origin": "origin", "carrier": "carriers"}[intent]
            if intent == "delivery":
                region = state.get("destination")
                if not region:
                    return "", [], ["destination"]
                regional = shipping.get("delivery_by_region", {})
                regional = {canonical_region(k, shipping): v for k,v in regional.items()}
                raw = regional.get(region) if regional else shipping.get(key)
                key = "delivery_by_region." + region if regional else key
            else:
                raw = shipping.get(key)
            value, ev = fact(raw, "business.shipping." + key)
            if value is None:
                return "", [], [key]
            if intent == "delivery" and state.get("date_needs_confirmation"):
                return "", [], ["use_date"]
            if intent == "delivery" and state.get("use_date"):
                transit = date_bounds(value)
                lead, lead_ev = fact(shipping.get("lead_time_days", shipping.get("lead_time")), "business.shipping.lead_time")
                preparation = date_bounds(lead)
                if transit and preparation:
                    today = self.today()
                    start = today + timedelta(days=preparation[0] + transit[0])
                    end = today + timedelta(days=preparation[1] + transit[1])
                    deadline = date.fromisoformat(state["use_date"])
                    if deadline < start:
                        verdict = "按现有时效估计，赶不上您的使用日期"
                    elif deadline < end:
                        verdict = "您的使用日期落在预计送达范围内，存在赶不上的风险"
                    else:
                        verdict = "按现有时效估计可能赶得上，但不能保证指定日期送达"
                    return "运输资料为%s。以店铺日期%s计算，备货加运输预计%s至%s送达。您需在%s使用，%s。" % (value, today, start, end, deadline, verdict), ev + lead_ev, []
                return "已有运输时效资料，但缺少完整备货或运输天数，暂时无法判断能否在%s前收到。" % state["use_date"], ev, ["shipping_window"]
            if isinstance(value, list):
                value = "、".join(map(str, value))
            prefix = {"delivery": "发往%s的运输时效资料为" % state.get("destination", ""),
                      "dispatch": "店铺发货时限资料为", "origin": "资料显示发货地为", "carrier": "店铺使用的快递为"}[intent]
            suffix = "请结合发货时间安排，具体送达以物流实际情况为准。" if intent == "delivery" else ""
            return "%s%s。%s" % (prefix, value, suffix), ev, []
        if intent in {"promotion", "policy"}:
            value, ev = fact((business.get("promotions") or {}).get("note") if intent == "promotion" else
                             (business.get("policies") or {}).get("return_policy"), "business." + intent)
            if value is None:
                return "", [], [intent]
            return str(value) + (" 客服可提供的优惠以已公布活动为限。" if intent == "promotion" else ""), ev, []
        # Offline retrieval deliberately requires an approved exact question/alias;
        # no weak similarity score can turn unrelated history into a factual answer.
        active = self.knowledge.active("faq") if self.knowledge else None
        if active:
            candidates = search_faq(u.faq_query, active["payload"], state.get("product_id"), business.get("synonyms"))
            for candidate in candidates:
                if candidate["answerable"]:
                    faq = active["payload"][candidate["index"]]
                    value, ev = fact({"value": faq["answer"], "expires_at": faq.get("expires_at"),
                                      "effective_from": faq.get("effective_from"),
                                      "source": active["source"]}, "faq.revision.%s" % active["id"])
                    return (str(value), ev, []) if value is not None else ("", [], ["faq_expired"])
            if any(c.get("exact") and c["match_type"] in {"expired", "not_effective", "invalid"} for c in candidates):
                return "", [], ["faq_expired"]
        return "", [], ["intent"]
