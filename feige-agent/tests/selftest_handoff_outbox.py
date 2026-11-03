"""Shared notification writers and handling must not lose human tasks."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.notifications import HandoffOutbox


def event(mid):
    return {'store_id':'test','conversation_id':'buyer','fingerprint':mid,'category':'handoff_after_sale',
            'summary':'构造售后问题','draft_reply':'请在平台人工处理'}


class OutboxTests(unittest.TestCase):
    def test_preexisting_instances_deduplicate_each_others_publications(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'notifications.jsonl'
            first,second=HandoffOutbox(path),HandoffOutbox(path)
            self.assertTrue(first.publish(event('same'))['queued'])
            self.assertTrue(second.publish(event('same'))['duplicate'])
            self.assertEqual(len(first.list_pending()),1)

    def test_cross_process_publish_and_resolution_preserve_all_unique_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'notifications.jsonl';box=HandoffOutbox(path)
            original=box.publish(event('original'))['event_id']
            children=[subprocess.Popen([sys.executable,__file__,'--writer',str(path)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) for _ in range(3)]
            try:
                self.assertTrue(box.mark_done(original))
                for child in children:
                    out,err=child.communicate(timeout=15)
                    self.assertEqual(child.returncode,0,err.decode(errors='replace'))
                pending=box.list_pending()
                self.assertEqual(len(pending),25)
                self.assertEqual(len({row['event_id'] for row in pending}),25)
                self.assertIsNone(box.get_pending(original))
            finally:
                for child in children:
                    if child.poll() is None:child.terminate();child.communicate(timeout=5)

    def test_truncated_utf8_tail_preserves_valid_tasks_and_next_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'notifications.jsonl';box=HandoffOutbox(path)
            first=box.publish(event('first'))['event_id']
            corrupt=b'{"summary":"\xe4\xb8'
            with path.open('ab') as stream:stream.write(corrupt)
            self.assertEqual(len(box.list_pending()),1)
            second=box.publish(event('second'))['event_id']
            self.assertEqual(len(box.list_pending()),2)
            self.assertTrue(box.mark_done(first))
            self.assertIn(corrupt,path.read_bytes())
            self.assertEqual([r['event_id'] for r in box.list_pending()],[second])


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--writer':
        box=HandoffOutbox(sys.argv[2])
        for index in range(25):box.publish(event('shared-'+str(index)))
    else:unittest.main(verbosity=1)
