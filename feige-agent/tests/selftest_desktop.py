# -*- coding: utf-8 -*-
"""Desktop bridge and essential UI contract tests."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.desktop import DesktopAPI, UI_DIR       # noqa: E402
from app.engine import browser                    # noqa: E402
from app.notifications import HandoffOutbox      # noqa: E402


class FakeEmbedder:
    def __init__(self):
        self.attached = []
        self.bounds = []
        self.detached = False

    def status(self):
        return {"supported": True, "embedded": bool(self.attached), "error": ""}

    def attach(self, port, wait=8.0):
        self.attached.append(port)
        return {"ok": True, "embedded": True}

    def set_bounds(self, *values):
        self.bounds.append(values)
        return {"ok": True}

    def detach(self):
        self.detached = True


class FakeSupervisor:
    def __init__(self, root):
        self.notifications = HandoffOutbox(root / "alerts.jsonl")
        self.notifications.publish({
            "store_id": "shop_001", "conversation_id": "c1", "fingerprint": "f1",
            "category": "handoff_after_sale", "buyer": "测试买家", "summary": "我要退货",
            "draft_reply": "请提供订单号",
        })
        self.started = []
        self.stopped = []
        self.monitor_started = False
        self.closed = False
        self.sent_drafts = []
        self.specs = [type("Spec", (), {"store_id": "shop_001", "cdp_port": 9222})()]

    def status(self):
        return {"configured": 10, "online": 0, "needs_attention": 0,
                "pending_notifications": len(self.notifications.list_pending()),
                "shops": [{"store_id": "shop_001", "name": "店铺01", "cdp_port": 9222,
                           "state": "stopped", "desired": True}], "ts": "2026-09-22T10:00:00"}

    def boot_all(self, max_parallel=3):
        return {"configured": 10, "attempted": 10, "online": 10, "results": []}

    def boot_store(self, store_id):
        self.started.append(store_id)
        return {"ok": True, "state": "ready", "store_id": store_id}

    def stop_store(self, store_id):
        self.stopped.append(store_id)
        return True

    def start_monitor(self):
        self.monitor_started = True
        return True

    def resolve_handoff(self, event_id):
        return self.notifications.mark_done(event_id)

    def send_handoff_reply(self, event_id, text):
        self.sent_drafts.append((event_id, text))
        self.notifications.mark_done(event_id)
        return {"ok": True}

    def shutdown(self):
        self.closed = True


def main():
    temp = Path(tempfile.mkdtemp(prefix="feige-desktop-"))
    supervisor = FakeSupervisor(temp)
    embedder = FakeEmbedder()
    api = DesktopAPI(supervisor, embedder=embedder)
    checks = []
    state = api.get_state()
    checks.append(("状态接口返回 10 店与通知", state["configured"] == 10 and len(state["notifications"]) == 1))
    checks.append(("全部启动接口可供桌面按钮调用", api.start_all().get("online") == 10))
    checks.append(("单店启停接口正确路由", api.start_store("shop_001").get("ok") and
                   api.stop_store("shop_001").get("ok") and
                   supervisor.started == ["shop_001"] and supervisor.stopped == ["shop_001"] and
                   supervisor.monitor_started))
    checks.append(("启动后复用同一店铺浏览器并内置原页面",
                   embedder.attached == [9222] and api.embed_store("shop_001").get("ok") and
                   embedder.attached == [9222, 9222]))
    checks.append(("原页面区域尺寸可以同步到原生窗口",
                   api.set_embed_bounds(390, 64, 800, 700).get("ok") and
                   embedder.bounds[-1] == (390, 64, 800, 700)))
    event_id = state["notifications"][0]["event_id"]
    checks.append(("人工草稿可经桌面接口确认发送",
                   api.send_handoff_reply(event_id, "修改后的草稿").get("ok") and
                   supervisor.sent_drafts == [(event_id, "修改后的草稿")]))
    supervisor.notifications.publish({
        "store_id": "shop_001", "conversation_id": "c2", "fingerprint": "f2",
        "category": "handoff_quality", "buyer": "测试买家2", "summary": "商品破损",
        "draft_reply": "请提供照片",
    })
    state = api.get_state()
    event_id = state["notifications"][0]["event_id"]
    checks.append(("人工消息可以标记完成", api.mark_notification_done(event_id).get("ok") and
                   not supervisor.notifications.list_pending()))
    api.shutdown()
    checks.append(("窗口关闭会解除原页面绑定并停止监督器",
                   supervisor.closed and embedder.detached))

    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    css = (UI_DIR / "styles.css").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    desktop_source = (ROOT / "app" / "desktop.py").read_text(encoding="utf-8")
    embed_source = (ROOT / "app" / "embedded_browser.py").read_text(encoding="utf-8")
    checks.append(("界面使用语义化主区域和原生按钮",
                   '<main class="main"' in html and html.count("<button") >= 3))
    checks.append(("键盘焦点和减少动态效果均有样式",
                   ":focus-visible" in css and "prefers-reduced-motion" in css))
    checks.append(("动态客户内容使用 textContent 写入", "textContent" in js and "innerHTML" not in js))
    checks.append(("人工草稿支持编辑后确认发送",
                   "draft-input" in js and "send_handoff_reply" in js and "确认并发送" in js))
    checks.append(("累计回复读取持久化统计并区分本次运行",
                   "cumulativeReplies" in js and "累计自动回复" in js and "本次运行回复" in js))
    checks.append(("界面保留原页面容器、恢复提示和手动重载入口",
                   'id="browserHost"' in html and 'id="embedRetryButton"' in html and
                   "set_embed_bounds" in js and "请先启动店铺，再重试" in js))
    checks.append(("最小化后恢复工作台会同步恢复浏览器绘制区域",
                   "events.restored += api._embedder.restore" in desktop_source and
                   "ShowWindow(self._browser_hwnd, 9)" in embed_source))

    with patch.object(browser.subprocess, "Popen") as popen:
        browser.launch_chrome(
            9222, temp / "edge-profile", chrome="msedge.exe", app_mode=True,
        )
        launch_args = popen.call_args.args[0]
    checks.append(("Edge 只使用受支持的调试与独立 profile 参数",
                   "--remote-debugging-port=9222" in launch_args and
                   any(arg.startswith("--user-data-dir=") for arg in launch_args) and
                   any(arg.startswith("--app=https://") for arg in launch_args) and
                   not any("AutomationControlled" in arg for arg in launch_args)))

    failed = []
    for name, ok in checks:
        print("  %s %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            failed.append(name)
    print("通过 %d 项，失败 %d 项" % (len(checks) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
