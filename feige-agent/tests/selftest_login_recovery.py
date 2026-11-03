"""Check authenticated-home recovery without opening a real customer chat."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import FeigeAgent
from app.engine.page import PageAgent

WORKSPACE = "https://im.jinritemai.com/pc_seller_v2/main/workspace"
HOME = "https://fxg.jinritemai.com/ffa/mshop/homepage/index"


class FakeCDP:
    def __init__(self, url, targets=None):
        self.url = url
        self.target_url = url
        self.targets = targets or [{"type": "page", "url": url}]
        self.commands = []
        self.preferences = []
        self.alive = True

    def is_alive(self):
        return self.alive

    def evaluate(self, script, **kwargs):
        if script == "location.href":
            return self.url
        return {"hasInput": True, "buyer": "", "convCount": 0}

    def list_targets(self):
        return self.targets

    def close(self):
        self.alive = False

    def connect(self, prefer, timeout=3):
        self.preferences.append(prefer)
        self.url = next(row["url"] for row in self.targets if prefer in row["url"])
        self.alive = True
        return self.url

    def call(self, method, params):
        self.commands.append((method, params))


def agent_for(cdp):
    agent = FeigeAgent.__new__(FeigeAgent)
    agent.cfg = {"workspace_url": WORKSPACE, "cdp_port": 13999, "profile_dir": "offline"}
    agent.browser_app_mode = True
    agent.cdp = cdp
    agent.page = PageAgent(cdp)
    agent.page.inject = lambda: None
    agent.log = []
    agent._log_lock = threading.RLock()
    agent._workspace_recovery_at = float("-inf")
    return agent


def main():
    assert PageAgent(FakeCDP(HOME)).probe()["state"] == "wrong_site"
    assert PageAgent(FakeCDP("https://other.test/?next=" + WORKSPACE)).probe()["state"] == "wrong_site"
    cdp = FakeCDP(HOME)
    agent = agent_for(cdp)
    assert agent.connect()["state"] == "loading"
    assert cdp.commands == [("Page.navigate", {"url": WORKSPACE})]
    agent.connect()
    assert len(cdp.commands) == 1, "redirect recovery must not loop rapidly"
    cdp = FakeCDP("https://im.jinritemai.com/login")
    assert agent_for(cdp).connect()["state"] == "not_logged_in"
    assert not cdp.commands, "an unfinished login must be left for the merchant"
    cdp = FakeCDP(HOME, [{"type": "page", "url": HOME},
                        {"type": "page", "url": WORKSPACE}])
    assert agent_for(cdp).connect()["ok"]
    assert cdp.preferences == ["im.jinritemai.com"]
    assert not cdp.commands, "reuse the existing workspace instead of creating another"
    cdp = FakeCDP("https://fxg.jinritemai.com/ffa/morder/order/list")
    with patch("app.agent.browser.launch_chrome") as launch:
        assert agent_for(cdp).connect()["state"] == "loading"
        launch.assert_called_once()
    assert not cdp.commands, "preserve the merchant's order page"
    print("login recovery, exact page binding and redirect cooldown: PASS")


if __name__ == "__main__":
    main()
