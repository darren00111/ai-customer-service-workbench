"""Account-free onboarding. Only an isolated demo Workbench is reachable here."""
from __future__ import annotations

import copy
import csv
import io
import json
import threading
from pathlib import Path

from .workbench import Workbench, FIXTURES
from .learning import LearningLoop
from .knowledge import redact


class Experience:
    def __init__(self, root: Path):
        # Never borrow the production registry, models, profiles or live workers.
        self.root = Path(root) / 'experience-v1'
        self.workbench = Workbench(self.root, self.root / 'unused-stores')
        self.sources = json.loads(FIXTURES.read_text(encoding='utf-8'))
        self.workbench.fixtures = {key: {**copy.deepcopy(row), 'products': [], 'business': {}}
                                   for key, row in self.sources.items()}
        self.lock = threading.RLock()

    def _kb(self, store_id):
        if store_id not in self.sources:
            raise ValueError('请选择一个示例店铺')
        return self.workbench.knowledge(store_id)

    def status(self, store_id='demo_billiards'):
        with self.lock:
            kb = self._kb(store_id)
            products, business = kb.active('products'), kb.active('business')
            faq = kb.active('faq')
            rows = products['payload'] if products else []
            return {'store_id': store_id, 'name': self.sources[store_id]['name'],
                    'choices': [{'id': key, 'name': value['name']} for key, value in self.sources.items()],
                    'prepared': bool(rows), 'products': len(rows),
                    'facts': sum(len(p.get('attributes', {})) for p in rows),
                    'faqs': len(faq['payload']) if faq else 0,
                    'examples': self.sources[store_id]['examples'][:5],
                    'transcript': kb.transcript('experience'),
                    'rollback_available': kb.rollback_target('faq') is not None,
                    'mode': 'offline_synthetic', 'model_calls': False, 'customer_messages_sent': False,
                    'knowledge_source': products['source'] if products else '',
                    'business_ready': bool(business and business['payload']),
                    'history': kb.publish_history(10)}

    def _publish(self, kb, kind, payload, source):
        current = kb.active(kind)
        if current and current['payload'] == payload:
            return current['id']
        revision = kb.stage(kind, payload, source, demo=True,
                            base_revision=current['id'] if current else 0)
        kb.activate(revision, allow_demo=True, reviewed=True, operator='模拟体验')
        return revision

    def prepare(self, store_id):
        with self.lock:
            kb = self._kb(store_id)
            # Re-entering the tour must never overwrite an imported sample file.
            if not self.status(store_id)['prepared']:
                for kind in ('products', 'business'):
                    self._publish(kb, kind, copy.deepcopy(self.sources[store_id][kind]), '内置示例资料（模拟）')
            if not kb.active('faq'):
                self._publish(kb, 'faq', [{'question': '这是模拟店铺吗',
                              'answer': '这是模拟店铺，资料和回复只用于体验。'}], '内置体验说明（模拟）')
            return self.status(store_id)

    def chat(self, store_id, text, reset=False):
        with self.lock:
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                raise ValueError('请输入1–2000字的问题')
            if not self.status(store_id)['prepared']:
                raise ValueError('请先整理示例资料，再开始试聊')
            result = self.workbench.chat(store_id, text.strip(), 'experience', reset=bool(reset))
            return {'reply': result['draft_reply'], 'action': result['action'],
                    'interaction_id': result['interaction_id'], 'evidence': result.get('evidence', []),
                    'missing': result.get('missing', []), 'mode': 'offline_synthetic', 'sent': False}

    def clear_chat(self, store_id):
        with self.lock:
            kb = self._kb(store_id)
            self.workbench._sessions.pop((store_id, 'experience'), None)
            with kb.connect() as db:
                db.execute('DELETE FROM sessions WHERE session_id=?', ('experience',))
                db.execute('DELETE FROM conversation_events WHERE session_id=?', ('experience',))
            return self.status(store_id)

    def preview_correction(self, store_id, interaction_id, answer):
        with self.lock:
            kb = self._kb(store_id)
            if type(interaction_id) is not int:
                raise ValueError('请先选择本次试聊的问题')
            with kb.connect() as db:
                latest = db.execute("SELECT id FROM interactions WHERE session_id='experience' ORDER BY id DESC LIMIT 1").fetchone()
            if not latest or latest['id'] != interaction_id:
                raise ValueError('试聊已有新消息，请重新选择最新问题')
            feedback = self.workbench.correct(store_id, interaction_id, answer)
            proposed = self.workbench.propose_correction(store_id, feedback['feedback_id'])
            report = self.workbench.evaluate(store_id, proposed['id'])
            products = (kb.active('products') or {}).get('payload', [])
            titles = {p['product_id']: p['title'] for p in products}
            items = [{**row, 'scope_label': '、'.join(titles.get(pid, '本店已选商品') for pid in row.get('product_ids', [])) or '本店通用问答'}
                     for row in proposed['payload']]
            return {'revision': proposed['id'], 'items': items, 'report': report,
                    'hint': '检查通过后，确认用于模拟回复；尚未修改生效知识。'}

    def confirm_correction(self, store_id, revision):
        with self.lock:
            kb = self._kb(store_id)
            candidate = kb.preview(int(revision))
            if candidate['kind'] != 'faq' or candidate['activated_at']:
                raise ValueError('请重新预览待确认的问答')
            report = self.workbench.evaluate(store_id, int(revision))
            if not report['passed']:
                raise ValueError('检查未通过，请修改正确答复后重新验证')
            self.workbench.activate(store_id, int(revision), True)
            return self.status(store_id)

    def rollback(self, store_id):
        with self.lock:
            self.workbench.rollback(store_id, 'faq', '体验者撤销最近一次问答更新', operator='模拟体验')
            return self.status(store_id)

    def import_products(self, store_id, content):
        """Plain Chinese CSV; fixed headings, no technical identifiers required."""
        with self.lock:
            kb = self._kb(store_id)
            if not isinstance(content, str) or len(content.encode('utf-8')) > 1_000_000:
                raise ValueError('请选择1MB以内的商品CSV文件')
            reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
            if not reader.fieldnames or not {'商品名称', '价格', '库存'} <= set(reader.fieldnames):
                raise ValueError('表头需要包含：商品名称、价格、库存；可选：材质、规格、别名')
            rows, names = [], set()
            import math
            for number, row in enumerate(reader, 2):
                if len(rows) >= 1000:
                    raise ValueError('每次最多整理1000件商品，请拆分文件')
                title = (row.get('商品名称') or '').strip()
                if not title or len(title) > 200 or title in names:
                    raise ValueError('第%d行商品名称为空、重复或超过200字' % number)
                try:
                    price = float(row.get('价格', ''))
                    stock = int(row.get('库存', ''))
                    if not math.isfinite(price) or price < 0 or stock < 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError('第%d行请填写非负价格和整数库存' % number) from None
                names.add(title)
                attrs = {key: row[key].strip() for key in ('材质', '规格') if row.get(key, '').strip()}
                if any(len(value) > 2000 for value in attrs.values()):
                    raise ValueError('第%d行属性超过2000字，请缩短' % number)
                aliases = [x.strip() for x in (row.get('别名') or '').split('、') if x.strip()]
                if len(aliases) > 20 or any(len(x) > 100 for x in aliases):
                    raise ValueError('第%d行别名过多或过长' % number)
                rows.append({'product_id': 'EXP-%04d' % len(rows), 'title': title, 'price': price,
                             'stock': stock, 'aliases': aliases, 'attributes': attrs,
                             'sample_data': True, 'source': 'sample'})
            if not rows:
                raise ValueError('文件中没有商品，请填写后重新导入')
            self._publish(kb, 'products', rows, '体验者导入CSV（仅用于模拟）')
            self.prepare(store_id)
            return self.clear_chat(store_id)


class ExperienceAPI:
    def __init__(self, root):
        self._experience = Experience(Path(root))

    def _call(self, method, *args):
        try:
            return {'ok': True, 'data': getattr(self._experience, method)(*args)}
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return {'ok': False, 'error': redact(str(exc))}
        except Exception:
            return {'ok': False, 'error': '操作没有完成，请重新打开体验窗口后再试。'}

    def status(self, store_id='demo_billiards'): return self._call('status', store_id)
    def prepare(self, store_id): return self._call('prepare', store_id)
    def chat(self, store_id, text): return self._call('chat', store_id, text)
    def clear_chat(self, store_id): return self._call('clear_chat', store_id)
    def preview_correction(self, store_id, interaction_id, answer):
        return self._call('preview_correction', store_id, interaction_id, answer)
    def confirm_correction(self, store_id, revision): return self._call('confirm_correction', store_id, revision)
    def rollback(self, store_id): return self._call('rollback', store_id)
    def import_products(self, store_id, content): return self._call('import_products', store_id, content)


def open_experience(root):
    import webview
    from ..desktop import UI_DIR
    return webview.create_window('立即体验 · 智能客服', (UI_DIR / 'experience.html').resolve().as_uri(),
                                 js_api=ExperienceAPI(root), width=1060, height=820,
                                 min_size=(720, 600), background_color='#f7f7f5', text_select=True)
