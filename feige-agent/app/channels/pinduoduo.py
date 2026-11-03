"""Experimental PDD transport through the merchant's own authenticated browser.

No password/cookie export, separate websocket login, client patch, or auto-resend.
The private web protocol requires real-store acceptance before production use.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import queue
import re
import threading
from urllib.parse import urlsplit
import uuid

from ..engine.cdp import CDPClient
from ..engine import browser
from .pdd_wire import receive_envelopes

HOST = "mms.pinduoduo.com"
WORKSPACE = "https://mms.pinduoduo.com/chat-merchant/index.html"
EVENTS = ("Network.webSocketCreated", "Network.webSocketClosed", "Network.webSocketFrameReceived")


def remote_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("平台未提供稳定的身份或消息标识")
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", text):
        raise ValueError("平台标识格式发生变化，请重新适配")
    return text


@dataclass(frozen=True)
class Account:
    mall_id: str
    user_id: str
    name: str

    @property
    def key(self):
        return self.mall_id + ":" + self.user_id


@dataclass(frozen=True)
class Incoming:
    conversation_id: str
    message_id: str
    role: str
    content: str
    timestamp: int
    action: str
    product_id: str = ''
    kind: str = 'text'


def message_timestamp(value):
    if isinstance(value,str):
        if not re.fullmatch(r'\d{1,15}(?:\.\d{1,3})?',value):
            raise ValueError('消息时间格式未识别')
        value=float(value)
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
        raise ValueError('消息时间缺失或无效')
    value=value*1000 if value<100_000_000_000 else value
    if value>253402300799999:
        raise ValueError('消息时间超出有效范围')
    return int(value)


def product_card(msg,buyer,mid,role,stamp):
    """Only the published goods-inquiry/card shape; prices are never facts."""
    info=msg.get('info')
    pid=info.get('goodsID') if isinstance(info,dict) else None
    if isinstance(pid,bool) or not isinstance(pid,(str,int)) or not re.fullmatch(r'\d{1,20}',str(pid)):
        return Incoming(buyer,mid,role,'[商品卡片缺少可核实的商品标识，请在原平台核对]',stamp,'pause',kind='unsupported')
    pid=str(pid)
    content=msg.get('content')
    # URL cards are references, never instructions or a source of live prices.
    if isinstance(content,str) and content.strip().startswith(('https://','http://')):
        from urllib.parse import parse_qs
        url=urlsplit(content.strip())
        ids=parse_qs(url.query).get('goods_id',[])
        if url.scheme!='https' or url.hostname not in ('mobile.yangkeduo.com','mobile.pinduoduo.com') or ids!=[pid]:
            return Incoming(buyer,mid,role,'[商品卡片链接与商品标识不一致，请在原平台核对]',stamp,'pause',kind='unsupported')
        content=''
    if content is not None and (not isinstance(content,str) or len(content)>7000):
        return Incoming(buyer,mid,role,'[商品卡片正文结构未识别，请在原平台核对]',stamp,'pause',kind='unsupported')
    text='[顾客发送商品卡片]' if role=='buyer' else '[客服发送商品卡片]'
    if content and content.strip():text+='\n'+content.strip()
    return Incoming(buyer,mid,role,text,stamp,'reply' if role=='buyer' and msg.get('sub_type')==0 else 'context',pid,'product_card')


def normalize_frame(payload, account):
    """Verify both parties and stable IDs before interpreting supported content."""
    if not isinstance(payload, str) or len(payload) > 1_000_000:
        return None, "unsupported_frame"
    try:
        frame = json.loads(payload)
    except (TypeError, ValueError):
        return None, "invalid_json"
    if not isinstance(frame, dict):
        return None, "unsupported_frame"
    if frame.get("response") != "push":
        return None, "connection_event"
    msg = frame.get("message")
    if not isinstance(msg, dict):
        return None, "missing_message"
    sender, receiver = msg.get("from"), msg.get("to")
    if not isinstance(sender, dict) or not isinstance(receiver, dict):
        return None, "missing_identity"
    try:
        if sender.get("role") == "user" and receiver.get("role") == "mall_cs":
            if remote_id(receiver.get("uid")) != account.user_id:
                return None, "other_account"
            buyer, role = remote_id(sender.get("uid")), "buyer"
        elif sender.get("role") == "mall_cs" and receiver.get("role") == "user":
            if remote_id(sender.get("uid")) != account.user_id:
                return None, "other_account"
            buyer, role = remote_id(receiver.get("uid")), "agent"
        else:
            return None, "unknown_role"
        if type(msg.get('type')) is int and msg['type']==20:
            return None,'read_receipt'
        mid = remote_id(msg.get("msg_id"))
        try:
            stamp = message_timestamp(msg['ts'] if 'ts' in msg else msg.get('time'))
        except ValueError:
            return None, "missing_timestamp"
        kind, subtype = msg.get("type"), msg.get("sub_type")
        if type(kind) is not int:
            return None, "unknown_type"
        # Retractions and transfers must invalidate an earlier draft, even without text.
        if kind in (24, 1002):
            return Incoming(buyer, mid, role, "[会话转接或消息撤回，请在原平台核对]", stamp, "pause",kind='control'), "control"
        if kind==0 and type(subtype) is int and subtype in (0,2):
            card=product_card(msg,buyer,mid,role,stamp)
            return card,'product_card' if card.kind=='product_card' else 'unsupported_content'
        if kind==5:
            info=msg.get('info')
            description=info.get('description') if isinstance(info,dict) else None
            if isinstance(description,str) and 1<=len(description.strip())<=1000:
                return Incoming(buyer,mid,role,description.strip(),stamp,'reply' if role=='buyer' else 'context',kind='emotion'),'emotion'
        if kind != 0 or subtype not in (None,):
            return Incoming(buyer, mid, role, "[商品卡片或非文字消息，请在原平台查看]", stamp, "pause",kind='unsupported'), "unsupported_content"
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 8000:
            return None, "invalid_text"
        return Incoming(buyer, mid, role, content.strip(), stamp, "reply" if role == "buyer" else "context"), "text"
    except (ValueError,OverflowError):
        return None, "invalid_identity"


class PinduoduoPilot:
    def __init__(self, spec, profile_root: Path, ledger, cdp_factory=CDPClient, on_history=None):
        if spec.platform != "pinduoduo":
            raise ValueError("此连接仅用于拼多多店铺")
        self.spec, self.ledger = spec, ledger
        # Never share an existing Feige or the user's default browser profile.
        self.profile = Path(profile_root) / spec.store_id
        self.cdp = cdp_factory(spec.cdp_port)
        self.stream = None
        self._target_id = None
        self._login_prepared = False
        self._prebind_events = 0
        self.catalog = None
        self.history = None
        self.on_history = on_history
        self.account = None
        self.bound = False
        self.sockets = set()
        self.eligible_heads = {}
        self.state = "disconnected"
        self.counts = {}
        self.lock = threading.RLock()
        from .lease import ConnectionLease
        self.lease = ConnectionLease(ledger.workspace, spec)

    def status(self):
        return {"state": self.state, "account": asdict(self.account) if self.account else None,
                "bound": self.bound, "observed_sockets": len(self.sockets), "events": dict(self.counts),
                "automatic_sending": False, "production_verified": False,
                "catalog_capture":self.catalog.status() if self.catalog else {'products':0,'error':''},
                "history_capture":self.history.status() if self.history else {'pages':0,'candidate_versions':0,'learning_error':''}}

    def open_login(self):
        with self.lock:
            self.close()
            self.lease.acquire()
            try:
                launched = browser.ensure_browser(self.spec.cdp_port, self.profile, 'about:blank',
                                                   chrome=self.spec.chrome_path or None)
                targets = self._chat_targets()
                if not targets and launched.get('status') == 'launched':
                    targets = [t for t in self.cdp.list_targets()
                               if t.get('type') == 'page' and t.get('url') == 'about:blank']
                elif not targets and launched.get('status') == 'reused':
                    # A user may still be on this shop's merchant login page.
                    # Observe its navigation, but inspect() must later prove that
                    # this very target reached the HTTPS customer-service page.
                    targets = [t for t in self.cdp.list_targets() if t.get('type') == 'page'
                               and urlsplit(t.get('url', '')).scheme == 'https'
                               and urlsplit(t.get('url', '')).hostname == HOST]
                if len(targets) != 1:
                    raise ValueError('请在该店独立窗口只保留一个拼多多客服页面，再重新打开登录窗口')
                target = targets[0]
                self._attach(target)
                # On a fresh browser, observe Network before the first navigation.
                # Otherwise the initial websocket is established before inspect()
                # and all its later frames lack a trusted socket-created event.
                if target.get('url') == 'about:blank':
                    self.cdp.call('Page.navigate', {'url': WORKSPACE})
                self._login_prepared = True
                self.state = 'login_required'
            except Exception:
                self.close()
                raise
        return {"state": "login_required", "detail": "请在独立窗口自行登录，进入拼多多客服后检查连接"}

    def _chat_targets(self):
        return [t for t in self.cdp.list_targets() if t.get('type') == 'page'
                and urlsplit(t.get('url', '')).scheme == 'https'
                and urlsplit(t.get('url', '')).hostname == HOST
                and urlsplit(t.get('url', '')).path.startswith('/chat-merchant/')]

    def _attach(self, target):
        self.cdp.connect_target(target)
        self._target_id = target.get('id')
        self.stream = self.cdp.subscribe(EVENTS)
        from .catalog_capture import ProductCapture
        self.catalog = ProductCapture(self.cdp)
        self.cdp.call('Network.enable', {'maxResourceBufferSize':2*1024*1024,
                                        'maxTotalBufferSize':8*1024*1024})

    def _post(self, path, body):
        # No credentials leave the page. Only fixed same-origin routes are allowed.
        if path not in ("/janus/api/new/userinfo", "/earth/api/merchant/queryMerchantInfoByMallId",
                        "/plateau/chat/send_message"):
            raise ValueError("不允许的请求")
        expr = """(async()=>{
          if(location.origin!==%s) throw Error('wrong_origin');
          const c=new AbortController(), timer=setTimeout(()=>c.abort(),8000);
          try {const r=await fetch(%s,{method:'POST',credentials:'same-origin',redirect:'error',
            headers:{'Content-Type':'application/json'},body:JSON.stringify(%s),signal:c.signal});
            if(!r.ok) return {success:false};
            return await r.json();
          } finally {clearTimeout(timer);}
        })()""" % (json.dumps("https://" + HOST), json.dumps(path), json.dumps(body, ensure_ascii=False))
        return self.cdp.evaluate(expr, timeout=10)

    def _identity(self):
        if self.cdp.evaluate("location.origin") != "https://" + HOST:
            raise ValueError("页面已离开拼多多商家后台，请重新登录并检查连接")
        user = self._post("/janus/api/new/userinfo", {})
        shop = self._post("/earth/api/merchant/queryMerchantInfoByMallId", {})
        if not isinstance(user, dict) or not isinstance(shop, dict) or user.get("success") is not True or shop.get("success") is not True:
            raise ValueError("无法核实已登录店铺，可能未登录或平台接口已变化")
        u, s = user.get("result") or {}, shop.get("result") or {}
        mall, cs = remote_id(u.get("mall_id")), remote_id(u.get("id"))
        if mall != remote_id(s.get("mallId")) or not isinstance(s.get("mallName"), str):
            raise ValueError("平台返回的店铺身份不一致，已停止连接")
        return Account(mall, cs, s["mallName"][:100])

    def inspect(self):
        with self.lock:
            # Retain the subscription prepared before login. Reconnecting here
            # would lose the socket-created event we deliberately observed early.
            prepared = (self._login_prepared and not self.bound and self.stream is not None
                        and self.cdp.is_alive())
            if not prepared:
                self.close()
            self.lease.acquire()
            try:
                self.ledger.recover(self.spec)
                targets = self._chat_targets()
                if len(targets) != 1:
                    raise ValueError("请在该店独立窗口只保留一个拼多多客服页面，再检查连接")
                if prepared:
                    if not self._target_id or targets[0].get('id') != self._target_id:
                        raise ValueError('登录后客服页面已换到其他标签，请重新打开登录窗口以准备消息连接')
                    if self.stream.overflowed:
                        raise ValueError('登录期间消息缓冲已满，请重新打开登录窗口并在原平台核对未处理消息')
                else:
                    self._attach(targets[0])
                self.account = self._identity()
                self.state = "awaiting_confirmation"
                return self.status()
            except Exception:
                self.close()
                raise

    def bind(self, account_key, reviewed=False):
        with self.lock:
            if reviewed is not True or not self.account or account_key != self.account.key:
                raise ValueError("请核对当前店铺和客服身份，再确认绑定")
            if self._identity().key != self.account.key:
                self.close()
                raise ValueError("登录账号已变化，请重新检查连接")
            self.ledger.bind(self.spec, self.account.key)
            from .history_context import HistoryCapture, HistoryContext
            self.history = HistoryCapture(self.cdp, HistoryContext(self.ledger.workspace), self.spec, self.account,self.on_history)
            # Keep pre-binding traffic as context, never as a new send target.
            # Login redirects may also have opened and closed an earlier socket.
            self._prebind_events = self.stream.events.qsize()
            self.bound = True
            self.state = "observing"
            return self.status()

    def poll(self):
        with self.lock:
            if not self.bound or not self.stream or not self.cdp.is_alive():
                if self.bound:
                    self.close()
                raise ValueError("请先检查连接并确认店铺绑定")
            try:
                if self.stream.overflowed:
                    raise ValueError("消息缓冲已满，可能有缺失，请在原平台核对后重新连接")
                if self._identity().key != self.account.key:
                    raise ValueError("登录身份已变化，为防止串店已停止采集")
                inserted = 0
                for _ in range(1000):
                    try:
                        event = self.stream.events.get_nowait()
                    except queue.Empty:
                        break
                    historical = self._prebind_events > 0
                    if historical:
                        self._prebind_events -= 1
                    method, p = event.get("method"), event.get("params") or {}
                    sid = p.get("requestId")
                    if method == "Network.webSocketCreated":
                        self.sockets.discard(sid)
                        u = urlsplit(p.get("url") or "")
                        if u.scheme == "wss" and u.hostname in {"m-ws.pinduoduo.com", "titan-ws.pinduoduo.com"}:
                            self.sockets.add(sid)
                        continue
                    if method == "Network.webSocketClosed":
                        observed = sid in self.sockets
                        self.sockets.discard(sid)
                        if observed and not historical:
                            raise ValueError("消息通道已中断，可能有未同步消息，请重新检查连接")
                        continue
                    if sid not in self.sockets:
                        continue
                    response = p.get("response") or {}
                    envelopes = receive_envelopes(response.get('payloadData'), response.get('opcode'))
                    for envelope in envelopes:
                        msg, reason = normalize_frame(json.dumps(envelope, ensure_ascii=False), self.account)
                        self.counts[reason] = self.counts.get(reason, 0) + 1
                        if msg:
                            added = self.ledger.ingest(self.spec, self.account.key, msg, historical=historical)
                            inserted += added
                            if added and not historical:
                                if msg.role == 'buyer' and msg.action == 'reply':
                                    self.eligible_heads[msg.conversation_id] = msg.message_id
                                else:
                                    self.eligible_heads.pop(msg.conversation_id, None)
                        elif reason not in ("connection_event", "other_account",'read_receipt'):
                            raise ValueError("消息结构未识别，已停止采集，请在原平台核对")
                if self.stream.overflowed:
                    raise ValueError("同步期间消息缓冲已满，请在原平台核对后重新连接")
                if self.catalog:
                    self.catalog.poll()
                if self.history:
                    self.history.poll()
                return {**self.status(), "inserted": inserted,
                        "hint": "未观察到消息通道时，请在独立窗口刷新客服页，再接收测试消息" if not self.sockets else "已同步观察到的消息"}
            except Exception:
                self.close()
                raise

    def send_reviewed(self, conversation_id, head, text, validate=None, automatic=False):
        """Single attempt; success requires an exact server message id echo."""
        with self.lock:
            self.poll()  # Recheck identity, human replies and latest buyer message first.
            if validate:
                validate()
            latest = self.ledger.head(self.spec, conversation_id)
            if latest.get('sequence') != head or self.eligible_heads.get(conversation_id) != latest.get('message_id'):
                raise ValueError("本次连接未观察到该轮新咨询，不能发送旧会话草稿；请在原平台核对")
            token = self.ledger.reserve(self.spec, self.account.key, conversation_id, head, text, automatic)
            body = {"data": {"cmd": "send_message", "request_id": uuid.uuid4().hex,
                    "message": {"to": {"role": "user", "uid": remote_id(conversation_id)},
                    "from": {"role": "mall_cs"}, "content": text, "msg_id": None,
                    "type": 0, "is_aut": 0, "manual_reply": 1}}, "client": "WEB"}
            try:
                response = self._post("/plateau/chat/send_message", body)
                result = response.get("result") if isinstance(response, dict) else None
                # Unknown response shapes must never become a confirmed send.
                if not isinstance(result, dict) or response.get("success") is not True or result.get("error_code") not in (None, 0):
                    self.ledger.finish(token, "delivery_unknown")
                else:
                    mid = result.get("msg_id")
                    self.ledger.finish(token, "awaiting_echo", remote_id(mid) if mid is not None else "")
                self.poll()
            except Exception:
                self.ledger.finish(token, "delivery_unknown")
            return self.ledger.delivery(token)

    def close(self):
        with self.lock:
            try:
                if self.bound:
                    self.ledger.invalidate_connection(self.spec)
                self.bound = False
                self.account = None
                self.sockets.clear()
                self.eligible_heads.clear()
                self.cdp.close()
                self.stream = None
                self._target_id = None
                self._login_prepared = False
                self._prebind_events = 0
                self.catalog = None
                self.history = None
                self.state = "disconnected"
            finally:
                self.lease.release()
