"""Qwen intent adapter and per-Windows-user encrypted credentials.

No SDK dependency, raw responses, prompts or credentials are logged.
Offline callers must explicitly opt in by passing ModelManager to Workbench.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler, getproxies

from .contracts import INTENTS, SYSTEM_INSTRUCTIONS, Understanding

DEFAULT_MODEL = "qwen3.8-flash"


class ModelError(ValueError):
    """Only fixed, safe messages may cross the UI/log boundary."""


def validate_endpoint(value):
    value = str(value).strip().rstrip("/")
    parts = urlsplit(value)
    hosts = r"(?:[a-z0-9-]+\.(?:cn-beijing|ap-southeast-1|ap-northeast-1|eu-central-1|cn-hongkong)\.maas\.aliyuncs\.com|dashscope\.aliyuncs\.com|dashscope-intl\.aliyuncs\.com)"
    if (parts.scheme != "https" or not re.fullmatch(hosts, parts.netloc) or
            parts.path != "/compatible-mode/v1" or parts.query or parts.fragment):
        raise ValueError("请填写百炼官方 HTTPS 接口地址，结尾为 /compatible-mode/v1")
    return value


def _protect(raw: bytes, decrypt=False):
    if os.name != "nt":
        raise ModelError("本机不支持 Windows 密钥保护")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source = Blob(len(raw), buffer)
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "Feige Qwen", None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise ModelError("密钥保护失败，请在当前 Windows 账户重新保存密钥")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Authorization to another host, even after a server redirect.
        return None


def _post(endpoint, key, payload):
    endpoint = validate_endpoint(endpoint)
    request = Request(endpoint + "/chat/completions",
                      data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    # Chinese Bailian endpoints prefer direct TLS. A local desktop proxy may
    # close the connection even while the same official endpoint is reachable.
    # Only this model transport changes route; OS/browser settings stay intact.
    routes = [False, True] if any(k in getproxies() for k in ('http','https')) else [False]
    for use_proxy in routes:
        try:
            handlers = [NoRedirect()] if use_proxy else [ProxyHandler({}), NoRedirect()]
            with build_opener(*handlers).open(request, timeout=30) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ModelError("模型返回内容过大，本轮未发送")
            return json.loads(raw)
        except HTTPError as exc:
            # Never retry authentication, throttling, redirects or service errors.
            # Upstream body/URL can echo secrets or customer data.
            messages = {400: "模型参数未被接口接受，请核对模型名称和接口地址", 401: "密钥未通过认证，请核对地域和密钥",
                        403: "模型访问被拒绝，请检查空间授权、IP 白名单和账号状态",
                        404: "接口或模型不存在，请核对模型名称和业务空间地址", 429: "模型调用额度或频率受限，请稍后重试"}
            raise ModelError(messages.get(exc.code, "千问服务暂时不可用，本轮未发送") + f"（HTTP {exc.code}）") from None
        except (URLError, TimeoutError, OSError):
            if use_proxy == routes[-1]:
                raise ModelError("千问网络连接失败或超时，本轮未发送，请稍后重试") from None
        except ModelError:
            raise
        except (ValueError, TypeError):
            raise ModelError("千问返回格式无效，本轮未发送") from None


OUTPUT_GUIDE = """返回一个 JSON 对象，不能有 Markdown 或其他文字。允许的 intents 为：%s。
未提供的字段可以省略；不要返回 null 作为 attribute/faq_query/variant/budget/product_ids 的值。
示例 {"intents":["price","inventory"],"product_id":"请求中的商品ID","variant":{},"needs_clarification":false}。
全部可用字段：intents 数组，product_id/destination/order_id 字符串或 null（无值可直接省略），attribute/faq_query 字符串，
variant {color,size}，clear_product/clear_order/clear_destination/needs_clarification/clear_budget/clear_use_date 布尔值，
budget {min,max,target} 数值，use_date YYYY-MM-DD 或 null，product_ids 商品ID数组。
destination 尽量归一为省级地区；价格口语如几钱、多少米、多少大洋都结合上下文判断。
已确认条件沿用；顾客纠正时更新条件。顾客只回答地点/规格时，继续处理 state.pending_intents。
product_reference 只能是 implicit/named/previous/comparison。问“这个多少钱”等沿用当前卡片时用 implicit；
本轮明确另选商品用 named，明确“前一个/上一款”用 previous，比较用 comparison。不要把模型自己猜到的商品标为 named。
state.platform_product_reference 是当前店铺独立采集的商品引用；local_product_id=null 表示尚未对应到本店已确认资料，不得套用前款价格。
details 的 attribute 选择该商品提供的 attribute_keys；没有对应属性仍可保留顾客询问的属性名。
actual after_sale 包括退款到账、已发生质量问题、要求退换；没买前问退换规则使用 policy。
messages 中 human_handled=true 仅表示商家确认已人工处理该轮，不代表退款已到账或质量问题已解决。后续明确改问商品可识别为售前；含糊追问仍需结合真实上下文，不能把退款“啥时候到”当成物流。
售后词的引用、明确否定或假设不等于申请售后；按真实诉求分类。仅找人工才用 human_request。
只有无法确定顾客想问什么时 needs_clarification=true；明确意图只缺地区/商品/订单时该字段为 false，由业务层补问。
比较两款的价格时仅使用 compare，product_ids 包含两款ID，不要额外添加依赖单一商品的 price/inventory。
FAQ：只在 faq_catalog 中选择语义相同、条件适用的一条，把 faq_query 设为它原样的 question 并使用 faq 意图。
不同问题不能选成同一 FAQ；无依据则 unknown，faq_query 保留原问题。不要从历史答复中推导新承诺。
可以返回 requested_attributes（顾客要了解/比较的属性列表），product_type（商品种类），recommendation_ids（仅适合该种类的商品ID列表）。
选择FAQ时必须返回 faq_index（对应 faq_catalog 中原样的 index），不要只把顾客原话放入faq_query后就结束。
clear_* 只有顾客明确撤销相应条件才返回true；普通追问不能清空已知的商品、预算和日期。
属性问题参考 attribute_values，不仅看属性名称。例如供电写在规格里，也应选择规格作为证据；requested_attributes仍记录供电方式。
比较必须保留具体比较指标；问哪个不震手时 requested_attributes=["震手"]，不能只比较价格。
推荐先识别商品种类，再选择 recommendation_ids。要球杆时排除巧粉等配件，不是所有预算内商品都适合。
能便宜吗/少点/抹零等议价诉求使用 promotion，不是只查 price。
收货地、商品、订单缺失时仍保留已明确意图，不置 needs_clarification=true。
已买商品问当前履约/物流时增加 order_status，让业务层核对授权订单，不能用一般发货时限冒充订单查询。
不得输出任何答复正文、价格、库存或政策。""" % ", ".join(sorted(INTENTS))


def output_schema(request):
    nullable = {"type": ["string", "null"]}
    products = [p["product_id"] for p in request.get("products", [])]
    properties = {"intents": {"type": "array", "items": {"type": "string", "enum": sorted(INTENTS)}, "minItems": 1},
                  "product_id": {**nullable, "enum": [None, *products]},
                  "destination": nullable, "order_id": {**nullable, "enum": [None, *request.get("authorized_order_ids", [])]},
                  "attribute": {"type": "string"}, "faq_query": {"type": "string"}, "use_date": nullable,
                  "variant": {"type": "object", "properties": {"color": {"type":"string"}, "size": {"type":"string"}}, "additionalProperties": False},
                  "budget": {"type": "object", "properties": {key: {"type":"number", "minimum":0} for key in ("min", "max", "target")}, "additionalProperties": False},
                  "product_ids": {"type":"array", "items": {"type":"string", **({"enum":products} if products else {})}, "maxItems":5}}
    properties.update({key: {"type":"boolean"} for key in
                       ("clear_product", "clear_order", "clear_destination", "needs_clarification", "clear_budget", "clear_use_date")})
    properties.update({"requested_attributes":{"type":"array","items":{"type":"string"},"maxItems":8},
                       "product_type":{"type":"string"},
                       "recommendation_ids":{"type":"array","items":{"type":"string", **({"enum":products} if products else {})},"maxItems":20}})
    properties['faq_index']={'type':['integer','null'],'enum':[None,*[row['index'] for row in request.get('faq_catalog',[])]]}
    properties['product_reference']={'type':'string','enum':['implicit','named','previous','comparison']}
    return {"type":"object", "properties":properties, "required":["intents"], "additionalProperties":False}


class QwenInterpreter:
    def __init__(self, endpoint, key, model=DEFAULT_MODEL, transport=None, on_result=None):
        self.endpoint = validate_endpoint(endpoint)
        if not re.fullmatch(r"qwen[a-zA-Z0-9._-]{1,100}", model):
            raise ValueError("请填写有效的千问模型名称")
        self._key, self.model = key, model
        self._transport = transport or _post
        self._on_result = on_result
        self.name = "qwen/" + model
        self.last_metrics = {}

    def understand(self, request):
        start = time.monotonic()
        # Credentials/config are never part of the customer request.
        data = {k: v for k, v in request.items() if k != "system"}
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 100_000:
            raise ModelError("本店资料和会话超出单次理解上限，请缩减资料或拆分咨询")
        payload = {"model": self.model, "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTIONS + "\n" + OUTPUT_GUIDE},
            {"role": "user", "content": encoded}], "stream": False, "enable_thinking": False,
            "temperature": 0, "max_tokens": 1600, "response_format": {"type": "json_object"}}
        if self.model.startswith(("qwen3.8", "qwen3.7")):
            payload["response_format"] = {"type":"json_schema", "json_schema": {
                "name":"customer_intent", "strict":True, "schema":output_schema(request)}}
        try:
            response = self._transport(self.endpoint, self._key, payload)
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ModelError("模型输出未完整完成，本轮未发送")
            content = choice["message"]["content"]
            result = json.loads(content)
            allowed = {p["product_id"] for p in request.get("products", [])}
            result = Understanding.from_dict(result, allowed).to_dict()
            if result['faq_index'] is not None and result['faq_index'] not in {row['index'] for row in request.get('faq_catalog',[])}:
                raise ModelError('模型选择了未提供的问答，本轮未发送')
            if result.get("order_id") and result["order_id"] not in request.get("authorized_order_ids", []):
                raise ModelError("模型返回了未授权的订单标识，本轮未发送")
            usage = response.get("usage", {})
            self.last_metrics = {"latency_ms": round((time.monotonic()-start)*1000),
                                 "input_tokens": int(usage.get("prompt_tokens", 0)),
                                 "output_tokens": int(usage.get("completion_tokens", 0))}
            if self._on_result:
                self._on_result(True, "")
            return result
        except Exception as exc:
            message = str(exc) if isinstance(exc, ModelError) else "模型判断格式不正确，本轮未发送"
            if self._on_result:
                self._on_result(False, message)
            raise ModelError(message) from None


class ModelManager:
    def __init__(self, path=None, protect=None, unprotect=None):
        self._path = Path(path) if path else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FeigeCustomerService" / "qwen.json"
        self._protect = protect or _protect
        self._unprotect = unprotect or (lambda raw: _protect(raw, True))
        self._lock = threading.RLock()
        self._last_signature = None
        self._connected = False
        self._error = ""

    def _read(self):
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (OSError, ValueError):
            raise ModelError("本地模型配置无法读取，请重新保存配置") from None

    def status(self):
        with self._lock:
            data = self._read()
            signature = json.dumps(data, sort_keys=True)
            current = signature == self._last_signature
            return {"provider": "qwen", "model": data.get("model", DEFAULT_MODEL),
                    "endpoint": data.get("endpoint", ""), "enabled": data.get("enabled", False),
                    "configured": bool(data.get("secret") and data.get("endpoint")),
                    "connected": bool(current and self._connected),
                    "error": self._error if current else ""}

    def save(self, endpoint, model, key="", enabled=True):
        if type(enabled) is not bool:
            raise ValueError("模型启用状态无效")
        if not isinstance(key, str) or len(key) > 2000 or (key and (not key.startswith("sk-") or any(c.isspace() for c in key))):
            raise ValueError("密钥格式无效，请重新粘贴")
        endpoint = validate_endpoint(endpoint)
        QwenInterpreter(endpoint, "validation-only", model)
        with self._lock:
            previous = self._read()
            secret = base64.b64encode(self._protect(key.encode())).decode() if key else previous.get("secret")
            if not secret:
                raise ValueError("请首次填写 API Key")
            data = {"provider": "qwen", "endpoint": endpoint, "model": model, "enabled": bool(enabled), "secret": secret}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temp, self._path)
            self._last_signature = None
            self._connected, self._error = False, ""
            return self.status()

    def interpreter(self):
        with self._lock:
            data = self._read()
            if not data.get("enabled"):
                return None
            if not data.get("secret"):
                raise ModelError("请先保存千问密钥")
            signature = json.dumps(data, sort_keys=True)
            try:
                key = self._unprotect(base64.b64decode(data["secret"], validate=True)).decode()
            except Exception:
                raise ModelError("无法解密本机密钥，请使用当前 Windows 账户重新保存") from None

        def completed(ok, error):
            with self._lock:
                if ok and json.dumps(self._read(), sort_keys=True) != signature:
                    raise ModelError("模型配置已改变，本轮未发送，请重新查询")
                self._last_signature = signature
                self._connected, self._error = ok, error
        return QwenInterpreter(data["endpoint"], key, data["model"], on_result=completed)

    def test(self):
        interpreter = self.interpreter()
        if interpreter is None:
            raise ValueError("请先启用模型")
        interpreter.understand({"today": "2026-10-18", "store_id": "connection_check",
                                "latest": "你好", "messages": [], "state": {}, "products": []})
        return {**self.status(), "metrics": interpreter.last_metrics}


_manager = None


def model_manager():
    global _manager
    if _manager is None:
        _manager = ModelManager()
    return _manager
