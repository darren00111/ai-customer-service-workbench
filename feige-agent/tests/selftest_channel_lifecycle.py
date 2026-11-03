"""Desktop connection ownership and lifecycle; no real client or webview."""
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.desktop import DesktopAPI
from app.service.workbench_api import WorkbenchAPI
from app.channels.lease import ConnectionLease


def lease(path):
    return ConnectionLease(SimpleNamespace(path=Path(path)),SimpleNamespace(merchant_id='local',platform='pinduoduo',store_id='test'))


class LifecycleTests(unittest.TestCase):
    def test_process_crash_releases_ownership_without_manual_lockfile_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'workspace.sqlite3'
            proc=subprocess.Popen([sys.executable,__file__,'--lease-child',str(path)],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            owner=lease(path)
            try:
                self.assertEqual(proc.stdout.readline().strip(),b'locked')
                with self.assertRaises(ValueError):owner.acquire()
                proc.terminate();proc.communicate(timeout=5)
                owner.acquire()  # OS releases the lock even when Python cannot run finally.
            finally:
                owner.release()
                if proc.poll() is None:proc.terminate();proc.communicate(timeout=5)
    def test_closing_persistent_workbench_does_not_stop_reception(self):
        w=Mock();api=WorkbenchAPI(w,persistent=True)
        api._window=Mock();api._close()
        self.assertIsNone(api._window)
        w.close_connections.assert_not_called()
        api.shutdown();w.close_connections.assert_called_once()

    def test_standalone_workbench_closure_stops_its_service(self):
        w=Mock();api=WorkbenchAPI(w)
        api._close();w.close_connections.assert_called_once()

    def test_desktop_reopens_same_service_and_closes_child_on_exit(self):
        supervisor,embedder=Mock(),Mock()
        desktop=DesktopAPI(supervisor,embedder)
        api=WorkbenchAPI(Mock(),persistent=True)
        first,second=MagicMock(),MagicMock()
        with patch('app.service.workbench_api.WorkbenchAPI',return_value=api) as factory, patch(
            'app.service.workbench_api.open_workbench',side_effect=[first,second]) as opened:
            self.assertTrue(desktop.open_workbench('tb')['ok'])
            self.assertTrue(desktop.open_workbench('pdd')['ok'])
            self.assertEqual(opened.call_count,1)
            first.show.assert_called_once()
            self.assertEqual(api._initial_store,'pdd')
            desktop._workbench_closed()
            self.assertTrue(desktop.open_workbench('tb')['ok'])
            self.assertEqual(opened.call_count,2);factory.assert_called_once()
            desktop.shutdown()
        second.destroy.assert_called_once()
        api._workbench.close_connections.assert_called_once()
        supervisor.shutdown.assert_called_once();embedder.detach.assert_called_once()


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--lease-child':
        owner=lease(sys.argv[2]);owner.acquire()
        print('locked',flush=True);sys.stdin.read(1);owner.release()
    else:unittest.main(verbosity=1)
