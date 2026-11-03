"""No credentials/network: exercise actual adapter contract and credential boundary."""
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.service.qwen import ModelError, ModelManager, QwenInterpreter, validate_endpoint, _protect, NoRedirect, _post
from app.service.dialogue import ReceptionService
from app.service.data import SnapshotDataSource
from app.service.workbench import Workbench
from app.service.workbench_api import WorkbenchAPI

ENDPOINT = "https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
# Deliberately short, invalid provider credential used only with mocked transport.
FAKE_KEY = "sk-unit"


def completion(value, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


class QwenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ModelManager(self.root / "model.json", protect=lambda b: b[::-1], unprotect=lambda b: b[::-1])

    def configured(self):
        self.manager.save(ENDPOINT, "qwen3.8-flash", FAKE_KEY)
        return self.manager

    def test_transport_prefers_direct_and_does_not_retry_http_errors(self):
        opener=MagicMock()
        opener.open.side_effect=HTTPError(ENDPOINT,401,'private upstream details',{},None)
        with patch('app.service.qwen.getproxies',return_value={'https':'http://127.0.0.1:7897'}), patch('app.service.qwen.build_opener',return_value=opener) as build:
            with self.assertRaisesRegex(ModelError,'HTTP 401'):
                _post(ENDPOINT,FAKE_KEY,{})
            self.assertEqual(build.call_count,1)
            self.assertIsInstance(build.call_args.args[0],ProxyHandler)
            self.assertEqual(build.call_args.args[0].proxies,{})

    def test_transport_falls_back_only_after_network_failure(self):
        direct,proxy=MagicMock(),MagicMock()
        direct.open.side_effect=URLError('direct connection unavailable')
        proxy.open.return_value.__enter__.return_value.read.return_value=b'{"ok":true}'
        with patch('app.service.qwen.getproxies',return_value={'https':'http://127.0.0.1:7897'}), patch('app.service.qwen.build_opener',side_effect=[direct,proxy]) as build:
            self.assertEqual(_post(ENDPOINT,FAKE_KEY,{}),{'ok':True})
            self.assertEqual(build.call_count,2)
            self.assertIsInstance(build.call_args.args[0],NoRedirect)

    def test_transport_bounded_failure_without_proxy(self):
        opener=MagicMock();opener.open.side_effect=URLError('private failure details')
        with patch('app.service.qwen.getproxies',return_value={}),patch('app.service.qwen.build_opener',return_value=opener) as build:
            with self.assertRaisesRegex(ModelError,'网络连接失败'):
                _post(ENDPOINT,FAKE_KEY,{})
            self.assertEqual(build.call_count,1)

    def test_only_verified_aliyun_endpoints(self):
        self.assertEqual(validate_endpoint(ENDPOINT+"/"), ENDPOINT)
        for url in ("http://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                    "https://ws-test.cn-beijing.maas.aliyuncs.com.evil.example/compatible-mode/v1",
                    "https://evil.example/compatible-mode/v1", ENDPOINT+"?key=test",
                    ENDPOINT.replace("https://", "https://user@")):
            with self.assertRaises(ValueError):
                validate_endpoint(url)

    def test_settings_never_return_plain_or_cipher_key(self):
        manager = self.configured()
        for value in (manager.status(), manager.save(ENDPOINT, "qwen3.7-plus")):
            self.assertNotIn("secret", value)
            self.assertNotIn(FAKE_KEY, json.dumps(value))
            self.assertFalse(value["connected"])
        self.assertNotIn(FAKE_KEY, manager._path.read_text())

    def test_disabled_model_never_creates_interpreter(self):
        self.configured().save(ENDPOINT, "qwen3.8-flash", enabled=False)
        self.assertIsNone(self.manager.interpreter())

    def test_corrupt_cipher_fails_closed(self):
        self.configured()
        row = json.loads(self.manager._path.read_text())
        row["secret"] = "!!!"
        self.manager._path.write_text(json.dumps(row))
        with self.assertRaisesRegex(ModelError, "无法解密"):
            self.manager.interpreter()

    def test_structured_call_contains_context_not_credentials(self):
        captured = []
        def transport(endpoint, key, payload):
            self.assertEqual(key, FAKE_KEY)
            captured.append(payload)
            return completion({"intents": ["price"], "product_id": "A"})
        model = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=transport)
        request = {"latest": "这根几钱", "state": {"product_id": "A"}, "messages": [{"role": "agent", "content": "您问黑杆吗"}],
                   "products": [{"product_id": "A", "title": "黑杆"}], "today": "2026-10-18"}
        self.assertEqual(model.understand(request)["product_id"], "A")
        self.assertNotIn(FAKE_KEY, json.dumps(captured))
        self.assertFalse(captured[0]["enable_thinking"])
        self.assertEqual(model.last_metrics["input_tokens"], 100)
        self.assertEqual(json.loads(captured[0]["messages"][1]["content"])["state"]["product_id"], "A")

    def test_model_cannot_generate_answer_or_foreign_product(self):
        for value in ({"intents":["price"],"reply":"1元"}, {"intents":["price"],"product_id":"FOREIGN"},
                      {"intents":["invented"]}, {"intents":["price"],"budget":{"max": True}}):
            model = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=lambda *args: completion(value))
            with self.assertRaises(ModelError):
                model.understand({"products": [{"product_id": "A"}]})

    def test_unbound_order_id_rejected(self):
        model = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=lambda *args: completion({"intents":["order_status"],"order_id":"OTHER"}))
        with self.assertRaisesRegex(ModelError, "未授权"):
            model.understand({"authorized_order_ids":["CURRENT"]})

    def test_truncation_refusal_and_invalid_json_never_answer(self):
        for response in (completion({"intents":["price"]}, "length"), completion({"intents":["price"]}, "content_filter"),
                         {"choices":[{"finish_reason":"stop","message":{"content":"not JSON"}}]}):
            model = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=lambda *args: response)
            with self.assertRaises(ModelError):
                model.understand({})

    def test_transport_exception_cannot_leak_secret(self):
        def bad(*args):
            raise RuntimeError("upstream debug: " + FAKE_KEY)
        model = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=bad)
        with self.assertRaises(ModelError) as caught:
            model.understand({})
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_failed_network_blocks_live_send_without_rule_fallback(self):
        def bad(*args):
            raise ModelError("千问网络连接失败或超时，本轮未发送，请稍后重试")
        model = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=bad)
        source = SnapshotDataSource("shop", {}, [{"product_id":"A","title":"黑杆","price":99}])
        result = ReceptionService(source, interpreter=model).respond("黑杆多少钱", live=True)
        self.assertTrue(result["model_error"])
        self.assertFalse(result["can_send"])
        self.assertFalse(result["automatic_resolution"])
        self.assertNotIn("99", result["draft_reply"])

    def test_workbench_offline_isolation_even_if_machine_configured(self):
        with patch("app.service.qwen._post", side_effect=AssertionError("offline must not call network")):
            workbench = Workbench(self.root / "offline", self.root / "shops")
            self.assertFalse(workbench.chat("demo_billiards", "黑杆多少钱")["model_connected"])

    def test_verified_state_resets_on_save_and_network_failure(self):
        manager = self.configured()
        with patch("app.service.qwen._post", return_value=completion({"intents":["greeting"]})):
            self.assertTrue(manager.test()["connected"])
        manager.save(ENDPOINT, "qwen3.8-flash")
        self.assertFalse(manager.status()["connected"])
        with patch("app.service.qwen._post", side_effect=ModelError("测试连接失败")):
            with self.assertRaises(ModelError):
                manager.test()
        self.assertFalse(manager.status()["connected"])

    def test_settings_bridge_and_model_injected_to_workbench(self):
        manager = self.configured()
        workbench = Workbench(self.root / "wb", self.root / "shops", models=manager)
        api = WorkbenchAPI(workbench)
        self.assertTrue(api.get_model_settings()["data"]["configured"])
        with patch("app.service.qwen._post", return_value=completion({"intents":["price"],"product_id":"CUE-B"})):
            result = api.chat("demo_billiards", "黑棍多少大洋", "test")["data"]
        self.assertTrue(result["model_connected"])
        self.assertIn("199", result["draft_reply"])
        self.assertNotIn(FAKE_KEY, json.dumps(result))

    def test_request_limit_prevents_unbounded_context_and_cost(self):
        model = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=lambda *args: self.fail("must not send"))
        with self.assertRaises(ModelError):
            model.understand({"latest":"长"*100001})

    def test_windows_dpapi_round_trip(self):
        import os
        if os.name != "nt":
            self.skipTest("Windows only")
        cipher = _protect(FAKE_KEY.encode())
        self.assertNotIn(FAKE_KEY.encode(), cipher)
        self.assertEqual(_protect(cipher, True).decode(), FAKE_KEY)

    def test_configuration_change_during_request_cancels_result(self):
        manager = self.configured()
        interpreter = manager.interpreter()
        def changed(*args):
            manager.save(ENDPOINT, "qwen3.8-flash", enabled=False)
            return completion({"intents":["greeting"]})
        interpreter._transport = changed
        with self.assertRaisesRegex(ModelError, "配置已改变"):
            interpreter.understand({})
        self.assertFalse(manager.status()["connected"])

    def test_redirect_never_forwards_credentials(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_compare_multiple_intents_does_not_ask_single_product(self):
        model = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=lambda *args: completion({"intents":["compare","price"],"product_ids":["CUE-B","CUE-W"]}))
        wb = Workbench(self.root / "compare", self.root / "shops")
        service = wb.service("demo_billiards")
        service.interpreter = model
        result = service.respond("黑白两根哪个便宜")
        self.assertEqual(result["action"], "reply")
        self.assertIn("199", result["draft_reply"])
        self.assertIn("239", result["draft_reply"])
        self.assertIn("枫木", result["draft_reply"])

    def test_semantic_comparison_cannot_hide_missing_performance_data(self):
        wb = Workbench(self.root / "compare-missing", self.root / "shops")
        service = wb.service("demo_billiards")
        service.interpreter = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=lambda *args: completion({
            "intents":["compare","details"],"product_ids":["CUE-B","CUE-W"],"attribute":"震手"}))
        result = service.respond("黑杆白杆哪个不震手")
        self.assertEqual(result["action"], "ask_followup")
        self.assertIn("attributes.震手", result["missing"])

    def test_offline_regression_does_not_make_paid_calls(self):
        wb = Workbench(self.root / "regression", self.root / "shops", models=self.configured())
        result = wb.service("demo_billiards", offline=True).respond("黑杆多少钱")
        interaction = wb.knowledge("demo_billiards").save_turn("regression", "黑杆多少钱", {}, result)
        wb.correct("demo_billiards", interaction, "199")
        with patch("app.service.qwen._post", side_effect=AssertionError("no paid calls")) as transport:
            self.assertTrue(wb.evaluate("demo_billiards")["passed"])
            transport.assert_not_called()

    def test_semantic_price_not_overridden_by_negated_aftersales_topic(self):
        wb = Workbench(self.root / "negation", self.root / "shops")
        service = wb.service("demo_billiards")
        service.interpreter = QwenInterpreter(ENDPOINT, FAKE_KEY, transport=lambda *args: completion({"intents":["price"],"product_id":"CUE-B"}))
        result = service.respond("别讲退货退款流程了，我只问黑杆几多钱")
        self.assertEqual(result["action"], "reply")
        self.assertIn("199", result["draft_reply"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
