"""Multi-platform desktop onboarding, isolated synthetic shops and no live login."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import StoreRegistry
from app.desktop import DesktopAPI
from app.multistore import MultiStoreSupervisor
from app.notifications import HandoffOutbox
from app.service.workbench import Workbench
from app.service.workbench_api import WorkbenchAPI
from selftest_desktop import FakeEmbedder


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.registry=StoreRegistry(self.root/'registry.json',self.root/'shops')
        self.registry.save([])
        self.w=Workbench(self.root/'reception',self.root/'shops',self.registry)
        self.factory=Mock(side_effect=AssertionError('Login must not create a reply agent'))
        with patch('app.multistore.DATA_DIR',self.root/'data'):
            self.supervisor=MultiStoreSupervisor(registry=self.registry,agent_factory=self.factory)
        self.control=WorkbenchAPI(self.w,persistent=True)
        self.api=DesktopAPI(self.supervisor,FakeEmbedder(),self.control)
        self.addCleanup(self.api.shutdown)

    def add(self,platform,name='品牌店'):
        result=self.api.add_store(platform,name)
        self.assertTrue(result['ok'],result)
        return result['store_id']

    def test_add_three_platforms_without_starting_login_or_reception(self):
        ids=[self.add(p) for p in ('feige','taobao','pinduoduo')]
        specs=self.registry.load()
        self.assertEqual(len(set(ids)),3)
        self.assertEqual(len({s.cdp_port for s in specs}),3)
        self.assertEqual(len({s.profile_dir for s in specs}),3)
        self.assertEqual(self.w._pilots,{})
        self.assertEqual(self.supervisor.agents,{})
        self.factory.assert_not_called()
        for spec in specs:
            self.assertFalse(spec.auto_start)
            self.assertFalse(spec.runtime_overrides.get('auto_send'))
        state=self.api.get_state()
        self.assertEqual((state['configured'],state['online']),(3,0))
        self.assertTrue(all(s['acceptance']['state']=='pending' for s in state['shops']))

    def test_same_platform_multiple_shops_persist_independently(self):
        first=self.add('pinduoduo');second=self.add('pinduoduo')
        self.assertNotEqual(first,second)
        reopened=Workbench(self.w.root,self.w.stores_dir,self.registry)
        self.addCleanup(reopened.close_connections)
        self.assertEqual({s.store_id for s in self.registry.load()},{first,second})
        self.assertEqual(reopened._pilots,{})

    def test_invalid_input_creates_no_shop(self):
        for args in [('xiaohongshu','店铺'),('taobao',' '),('feige','店铺','../other'),('taobao','店铺','local','')]:
            self.assertFalse(self.api.add_store(*args)['ok'])
        self.assertEqual(self.registry.load(),[])

    def test_feige_login_opens_only_owned_profile_not_reply_agent(self):
        sid=self.add('feige');spec=self.w.spec(sid)
        with patch('app.engine.browser.ensure_browser',return_value={'status':'launched'}) as launch:
            self.assertTrue(self.api.login_store(sid)['ok'])
        args=launch.call_args.args
        self.assertEqual(args[0],spec.cdp_port)
        self.assertEqual(args[1],spec.config_overrides()['profile_dir'])
        self.factory.assert_not_called()
        self.assertEqual(self.api.get_state()['shops'][0]['login']['state'],'verification_required')

    def test_pdd_and_qianniu_login_use_platform_connector_not_feige(self):
        with patch.object(self.w,'pilot_command',return_value={'state':'login_required'}) as command:
            for platform in ('pinduoduo','taobao'):
                sid=self.add(platform)
                self.assertTrue(self.api.login_store(sid)['ok'])
                self.assertEqual(command.call_args.args,(sid,'open_login'))
        self.factory.assert_not_called()
        self.assertEqual(self.w._channel_workers,{})

    def test_login_failure_does_not_block_other_platform(self):
        pdd=self.add('pinduoduo');feige=self.add('feige')
        with patch.object(self.w,'pilot_command',side_effect=ValueError('构造的平台窗口不可用')):
            self.assertFalse(self.api.login_store(pdd)['ok'])
        with patch('app.engine.browser.ensure_browser',return_value={'status':'launched'}):
            self.assertTrue(self.api.login_store(feige)['ok'])

    def test_missing_qianniu_requests_picker_without_claiming_login(self):
        from app.channels.qianniu_launcher import QianniuClientNotFound
        sid=self.add('taobao')
        with patch.object(self.w,'pilot_command',side_effect=QianniuClientNotFound('选择千牛安装位置')):
            result=self.api.login_store(sid)
        self.assertFalse(result['ok']); self.assertTrue(result['needs_client_path'])
        self.assertNotIn(sid,self.api._login_attempts)
        self.factory.assert_not_called()

    def test_login_cannot_disconnect_active_reception(self):
        sid=self.add('pinduoduo')
        monitor=SimpleNamespace(status=lambda:{'running':True},stop=lambda:True)
        self.w._channel_workers[sid]=monitor
        try:
            with patch.object(self.w,'pilot_command') as command:
                self.assertFalse(self.api.login_store(sid)['ok']);command.assert_not_called()
        finally:self.w._channel_workers.pop(sid)

    def test_knowledge_queue_is_scoped_and_status_read_starts_no_platform(self):
        first=self.add('feige');second=self.add('taobao')
        self.w.chat(first,'能刻字吗',session='customer-1')
        with patch.object(self.w,'_pilot',side_effect=AssertionError('Status cannot open a platform')):
            state=self.api.get_state()
        shops={s['store_id']:s for s in state['shops']}
        self.assertEqual(shops[first]['knowledge_queue']['open'],1)
        self.assertEqual(shops[second]['knowledge_queue']['open'],0)
        self.assertEqual(state['knowledge_pending'],1)
        self.assertEqual(state['online'],0)

    def test_single_store_knowledge_failure_does_not_hide_other_stores(self):
        first=self.add('feige');second=self.add('taobao')
        original=self.w.cards
        def cards(sid,*args):
            if sid==first:raise ValueError('构造的资料损坏')
            return original(sid,*args)
        with patch.object(self.w,'cards',side_effect=cards):state=self.api.get_state()
        self.assertTrue(state['knowledge_incomplete'])
        self.assertEqual(len(state['shops']),2)
        self.assertEqual(next(s for s in state['shops'] if s['store_id']==second)['knowledge_queue']['open'],0)

    def test_releasing_native_view_does_not_stop_reception_or_connection(self):
        self.add('feige')
        with patch.object(self.supervisor,'stop_store') as stop, patch.object(self.w,'close_connections') as close:
            self.assertTrue(self.api.release_embedded_page()['ok'])
            self.assertTrue(self.api._embedder.detached)
            stop.assert_not_called();close.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=1)
