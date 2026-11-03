"""Small native bridge for the onboarding and offline chat window."""
from __future__ import annotations

from ..config import RECEPTION_DIR, STORES_KB_DIR, StoreRegistry
from .workbench import Workbench
from .learning import LearningLoop
from .sync import SyncWorker
from .qwen import model_manager


class WorkbenchAPI:
    def __init__(self, workbench=None, on_created=None, persistent=False):
        self._workbench = workbench or Workbench(RECEPTION_DIR, STORES_KB_DIR, StoreRegistry(), models=model_manager())
        self._on_created = on_created
        self._window = None
        self._persistent = persistent
        self._initial_store = ''
        self._initial_section = ''
        self._sync_worker = SyncWorker(self._workbench)

    @staticmethod
    def _result(operation):
        try:
            return {"ok": True, "data": operation()}
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "操作未完成，请检查资料格式或重试。"}

    def list_stores(self):
        return self._result(self._workbench.stores)

    def get_initial_store(self):
        return self._result(lambda:self._initial_store)

    def get_initial_section(self):
        return self._result(lambda:self._initial_section)

    def get_model_settings(self):
        return self._result(lambda: self._models().status())

    def _models(self):
        if not self._workbench.models:
            raise ValueError("此离线测试环境未启用网络模型")
        return self._workbench.models

    def save_model_settings(self, endpoint, model, key="", enabled=True):
        return self._result(lambda: self._models().save(endpoint, model, key, enabled))

    def test_model_connection(self):
        return self._result(lambda: self._models().test())

    def get_info(self, store_id):
        return self._result(lambda: self._workbench.info(store_id))

    def chat(self, store_id, text, session, reset=False, demo_order=False):
        return self._result(lambda: self._workbench.chat(store_id, text, session, reset, demo_order))

    def stage_knowledge(self, store_id, kind, content, source, format="json"):
        return self._result(lambda: self._workbench.stage(store_id, kind, content, source, format))

    def preview_revision(self, store_id, revision):
        return self._result(lambda: self._workbench.knowledge(store_id).preview(int(revision)))

    def activate_revision(self, store_id, revision, reviewed):
        return self._result(lambda: self._workbench.activate(store_id, revision, reviewed))

    def create_store(self, store_id, name, category="通用零售", platform="feige", merchant_id="local"):
        def create():
            data = self._workbench.create_store(store_id, name, category, platform, merchant_id)
            if self._on_created:
                self._on_created()
            return data
        return self._result(create)

    def list_platforms(self):
        from ..platforms import list_platforms
        return self._result(list_platforms)

    def save_common_product(self,store_id,product_key,content,source,reviewed=False):
        return self._result(lambda:self._workbench.save_common_product(store_id,product_key,content,source,reviewed))

    def apply_common_product(self,store_id,product_id,revision,reviewed=False):
        return self._result(lambda:self._workbench.apply_common_product(store_id,product_id,revision,reviewed))

    def import_conversations(self,store_id,content):
        return self._result(lambda:self._workbench.import_conversations(store_id,content))

    def get_imported_conversation(self,store_id,conversation_id):
        return self._result(lambda:self._workbench.imported_conversation(store_id,conversation_id))

    def pause_imported_conversation(self,store_id,conversation_id,paused):
        return self._result(lambda:self._workbench.inbox.pause(self._workbench.spec(store_id),conversation_id,paused))

    def generate_imported_draft(self,store_id,conversation_id):
        return self._result(lambda:self._workbench.generate_imported_draft(store_id,conversation_id))

    def pilot_command(self, store_id, action, account_key='', reviewed=False):
        return self._result(lambda: self._workbench.pilot_command(store_id, action, account_key, reviewed))

    def send_live_draft(self, store_id, conversation_id, reviewed=False):
        return self._result(lambda: self._workbench.send_live_draft(store_id, conversation_id, reviewed))

    def capture_product_revision(self,store_id):
        return self._result(lambda:self._workbench.capture_product_revision(store_id))

    def close_uncertain_delivery(self,store_id,conversation_id,token,reviewed=False):
        return self._result(lambda:self._workbench.close_uncertain_delivery(store_id,conversation_id,token,reviewed))

    def start_channel_monitor(self, store_id, mode='draft', conversations=None, reviewed=False):
        return self._result(lambda: self._workbench.start_channel_monitor(store_id, mode, conversations, reviewed))

    def stop_channel_monitor(self, store_id):
        return self._result(lambda: self._workbench.stop_channel_monitor(store_id))

    def set_store_automation(self,store_id,enabled,reviewed=False):
        return self._result(lambda:self._workbench.set_store_automation(store_id,enabled,reviewed))

    def get_transcript(self, store_id, session, before=None):
        return self._result(lambda: self._workbench.knowledge(store_id).transcript(session, before))

    def record_correction(self, store_id, interaction_id, correction, action="reply"):
        return self._result(lambda: self._workbench.correct(store_id, interaction_id, correction, action))

    def get_correction_context(self, store_id, interaction_id):
        from .merchant_edits import MerchantEdits
        return self._result(lambda:MerchantEdits(self._workbench).context(store_id,interaction_id))

    def save_reply_edit(self, store_id, interaction_id, kind, value, product_id='', variant_index=None, versions=None):
        from .merchant_edits import MerchantEdits
        return self._result(lambda:MerchantEdits(self._workbench).save(store_id,interaction_id,kind,value,product_id,variant_index,versions))

    def save_card_answer(self, store_id, card_id, value):
        from .merchant_edits import MerchantEdits
        return self._result(lambda:MerchantEdits(self._workbench).save_card(store_id,card_id,value))

    def propose_correction(self, store_id, feedback_id):
        return self._result(lambda: self._workbench.propose_correction(store_id, feedback_id))

    def retire_feedback(self, store_id, feedback_id):
        return self._result(lambda: LearningLoop(self._workbench.knowledge(store_id)).retire(feedback_id))

    def evaluate_knowledge(self, store_id, revision=None, use_model=False):
        return self._result(lambda: self._workbench.evaluate(store_id, revision, use_model))

    def save_sales_policy(self,store_id,policy):
        return self._result(lambda:self._workbench.save_sales_policy(store_id,policy))

    def import_outcomes(self,store_id,content):
        return self._result(lambda:self._workbench.import_outcomes(store_id,content))

    def refresh_learning(self,store_id):
        return self._result(lambda:self._workbench.learning_refresh(store_id))

    def export_regressions(self, store_id):
        return self._result(lambda: LearningLoop(self._workbench.knowledge(store_id)).export_cases())

    def list_cards(self, store_id, include_closed=True):
        return self._result(lambda: self._workbench.cards(store_id, include_closed))

    def get_card(self, store_id, card_id):
        return self._result(lambda: self._workbench.card(store_id, int(card_id)))

    def card_action(self, store_id, card_id, action, payload=None):
        return self._result(lambda: self._workbench.card_action(store_id, int(card_id), action, payload or {}))

    def record_override(self, store_id, interaction_id, changes, reason, scope="session_only", special_offer=False, conversation_id=""):
        return self._result(lambda: self._workbench.override(store_id, int(interaction_id), changes, reason, scope,
                                                             bool(special_offer), conversation_id or ""))

    def list_overrides(self, store_id, session=None):
        return self._result(lambda: self._workbench.overrides(store_id, session))

    def publish_history(self, store_id):
        return self._result(lambda: self._workbench.publish_history(store_id))

    def rollback_knowledge(self, store_id, kind, reason, target=None):
        return self._result(lambda: self._workbench.rollback(store_id, kind, reason, target))

    def resolve_gap(self, store_id, gap_id):
        return self._result(lambda: self._workbench.knowledge(store_id).resolve_gap(gap_id))

    def resolve_task(self, store_id, task_id, note="资料已补充并核对"):
        return self._result(lambda: self._workbench.knowledge(store_id).resolve_task(task_id, note))

    def choose_sync_file(self):
        def choose():
            import webview
            if not self._window:
                raise ValueError("请从桌面窗口选择文件")
            files = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                                   file_types=("资料文件 (*.json;*.csv)",))
            return files[0] if files else ""
        return self._result(choose)

    def choose_qianniu_client(self):
        return {'ok':False,'error':'社区版仅支持抖店飞鸽'}

    def register_sync(self, store_id, path, kind, mode="merge", auto_publish=False, interval=60, trusted_outcomes=False):
        return self._result(lambda: self._workbench.sync(store_id).register(path, kind, mode, auto_publish, interval,trusted_outcomes))

    def sync_now(self, store_id):
        return self._result(lambda: self._workbench.sync(store_id).poll(force=True))

    def enable_sync(self, store_id, source_id, enabled):
        return self._result(lambda: self._workbench.sync(store_id).enable(source_id, enabled))

    def _close(self):
        self._window = None
        if not self._persistent:
            self.shutdown()

    def shutdown(self):
        self._sync_worker.stop()
        self._workbench.close_connections()


def open_workbench(api=None):
    import webview
    from ..desktop import UI_DIR
    api = api or WorkbenchAPI()
    window = webview.create_window("试聊与改正 · 智能客服", (UI_DIR / "workbench.html").resolve().as_uri(),
                                  js_api=api, width=1160, height=850,
                                  min_size=(720, 600), background_color="#f7f7f5", text_select=True)
    api._window = window
    api._sync_worker.start()
    window.events.closed += api._close
    return window


def run_workbench(debug=False):
    import webview
    from ..branding import APP_ICON, configure_desktop_identity
    configure_desktop_identity()
    open_workbench()
    webview.start(debug=debug, icon=str(APP_ICON))
