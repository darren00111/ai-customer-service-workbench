"""Platform identities and explicit, shipped connection capabilities."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re


PLATFORMS = {
    "feige": {"name": "抖店飞鸽", "runtime_available": True,
              "connection": "browser", "status": "已有网页适配，真实店铺仍需验收"},
    "pinduoduo": {"name": "拼多多", "runtime_available": False,
                   "connection": "pilot", "status": "网页消息试接入可联调，尚未通过真实店铺验收"},
    "taobao": {"name": "淘宝／天猫", "runtime_available": False,
               "connection": "pilot", "status": "千牛桌面收发适配待验收，需匹配实际客户端结构"},
    "xiaohongshu": {"name": "小红书店铺", "runtime_available": False,
                    "connection": "pending", "status": "资料准备可用，店铺客服消息连接待接入"},
}


def platform_info(platform):
    if platform not in PLATFORMS:
        raise ValueError("不支持的平台")
    result = dict(PLATFORMS[platform], platform=platform)
    result["capabilities"] = {
        "receive_messages": result["runtime_available"],
        "send_messages": result["runtime_available"],
        "local_handoff": result["runtime_available"],
        "platform_transfer": False,
        "live_products": False, "live_orders": False, "live_logistics": False,
        "knowledge_preparation": True, "imported_drafts": True,
        "experimental_connection": platform in {"taobao", "pinduoduo"},
        "native_diagnostics": platform == "taobao",
    }
    return result


def list_platforms():
    return [platform_info(key) for key in PLATFORMS]


def create_agent(spec, **kwargs):
    if not platform_info(spec.platform)["runtime_available"]:
        raise ValueError("该平台尚未接入消息收发，请先在接入与试聊中准备资料")
    from .agent import FeigeAgent
    return FeigeAgent.from_spec(spec, **kwargs)


@dataclass(frozen=True)
class ConversationScope:
    merchant_id: str
    platform: str
    store_id: str
    conversation_id: str

    def __post_init__(self):
        for value in (self.merchant_id, self.store_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                raise ValueError("商家或店铺标识无效")
        platform_info(self.platform)
        if not isinstance(self.conversation_id, str) or not 1 <= len(self.conversation_id) <= 200:
            raise ValueError("会话标识需为1–200字")

    @property
    def key(self):
        value = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class MessageEvent:
    scope: ConversationScope
    message_id: str
    role: str
    content: str

    def __post_init__(self):
        if not isinstance(self.scope, ConversationScope):
            raise ValueError("消息需要明确的商家、平台、店铺和会话范围")
        if not isinstance(self.message_id, str) or not 1 <= len(self.message_id) <= 200:
            raise ValueError("消息标识需为1–200字")
        if not isinstance(self.role, str) or self.role not in {"buyer", "agent"} or not isinstance(self.content, str) or not 1 <= len(self.content) <= 8000:
            raise ValueError("消息角色仅支持 buyer、agent，正文为1–8000字")

    @property
    def key(self):
        return hashlib.sha256(json.dumps([self.scope.key,self.message_id],ensure_ascii=False).encode()).hexdigest()
