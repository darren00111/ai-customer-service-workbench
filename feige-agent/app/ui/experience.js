'use strict';
const el = id => document.getElementById(id);
let currentStore = 'demo_billiards', pending = false, ready = false, latest = null, revision = null;
function tell(message, error = false) { el('status').textContent = message; el('status').className = error ? 'error' : ''; }
function textNode(tag, content, className) { const node = document.createElement(tag); node.textContent = content; if (className) node.className = className; return node; }
function message(role, content) { const node = textNode('div', '', 'message ' + role); node.append(textNode('span', role === 'buyer' ? '顾客（模拟）' : '客服（模拟）', 'speaker'), document.createTextNode(content)); el('messages').append(node); el('messages').scrollTop = el('messages').scrollHeight; }
function invalidateCorrection() { revision = null; el('correctionPreview').hidden = true; }
function busy(value) { pending = value; el('content').setAttribute('aria-busy', String(value)); document.querySelectorAll('button,input,select,textarea').forEach(n => { n.disabled = value || !ready; }); el('previewCorrection').disabled = value || !latest; }
async function call(method, ...args) {
  if (!window.pywebview?.api) throw new Error('请从桌面程序的“立即体验”入口打开此页面。');
  const result = await window.pywebview.api[method](...args);
  if (!result.ok) throw new Error(result.error || '操作没有完成，请重试。');
  return result.data;
}
async function action(task) { if (pending) return; busy(true); try { await task(); } catch (error) { tell(error.message, true); } finally { busy(false); } }
function render(info, restore = false) {
  currentStore = info.store_id;
  el('question').placeholder = info.examples[0] || '输入想了解的商品和问题';
  el('sampleStore').replaceChildren(...info.choices.map(choice => { const node = textNode('option', choice.name); node.value = choice.id; return node; }));
  el('sampleStore').value = currentStore;
  el('summary').hidden = !info.prepared;
  el('summary').textContent = `已整理 ${info.products} 件商品、${info.facts} 项商品属性和 ${info.faqs} 条问答。来源：${info.knowledge_source}。`;
  el('prepare').textContent = info.prepared ? '继续体验' : '整理示例资料并开始';
  el('chatSection').hidden = !info.prepared; el('correctionSection').hidden = !info.prepared;
  el('rollback').hidden = !info.rollback_available;
  ['step1','step2','step3'].forEach(id => el(id).removeAttribute('aria-current')); el(info.prepared ? 'step3' : 'step1').setAttribute('aria-current', 'step');
  el('examples').replaceChildren(...[...info.examples.slice(0,3), '能刻字吗'].map(question => { const button = textNode('button', question); button.type='button'; button.addEventListener('click', () => { el('question').value=question; el('question').focus(); }); return button; }));
  if (restore) { latest=null; invalidateCorrection(); el('messages').replaceChildren(); (info.transcript || []).forEach(row => message(row.role, row.content)); el('evidencePanel').hidden=true; el('correctionResult').textContent=''; el('correctAnswer').value=''; el('correctionQuestion').textContent='先试聊一个问题，再填写它的正确答复。'; }
}
el('prepare').addEventListener('click', () => action(async () => { el('step1').removeAttribute('aria-current'); el('step2').setAttribute('aria-current','step'); tell('正在读取示例资料并整理商品和规则…'); render(await call('prepare',currentStore),true); tell('资料已准备好。先点一个示例问题，再查看模拟回复。'); el('question').focus(); }));
el('sampleStore').addEventListener('change', () => action(async () => { render(await call('status',el('sampleStore').value),true); tell('已切换示例店铺，各店资料和试聊分别保存。'); }));
el('chatForm').addEventListener('submit', event => { event.preventDefault(); const question=el('question').value.trim(); if (!question) return; action(async () => {
  invalidateCorrection(); const answer=await call('chat',currentStore,question); message('buyer',question); message('agent',answer.reply); latest=answer; el('question').value=''; el('correctionQuestion').textContent='正在纠正的问题：'+question;
  el('evidence').replaceChildren(...answer.evidence.map(row => textNode('p', `${row.source || '本店模拟资料'} · ${row.path || ''}`)));
  if (!answer.evidence.length) el('evidence').append(textNode('p',answer.action==='handoff'?'这次需要人工处理；体验环境只展示处理方式。':'资料不足或为普通引导，没有可引用的业务事实。'));
  el('evidencePanel').hidden=false; tell(answer.action==='handoff'?'已演示交人工处理，没有向真实平台转接。':'已生成模拟回复。可以继续追问，或在下方教它回答新问题。');
}); });
el('clearChat').addEventListener('click',()=>action(async()=>{render(await call('clear_chat',currentStore),true);tell('新对话已开始，已学会的资料保留。');el('question').focus();}));
el('correctionForm').addEventListener('submit',event=>{event.preventDefault();if(!latest)return;action(async()=>{invalidateCorrection();const result=await call('preview_correction',currentStore,latest.interaction_id,el('correctAnswer').value.trim()); revision=result.revision;
  el('correctionItems').replaceChildren(...result.items.map(row=>textNode('p',`适用范围：${row.scope_label}\n问题：${row.question}\n答复：${row.answer}`)));
  el('correctionCheck').textContent=result.report.passed?'原问题检查通过；这是离线规则复测。':'检查未通过，请核对答复后重试。';el('correctionPreview').hidden=false;el('confirmCorrection').hidden=!result.report.passed;tell('请核对候选答复，确认后才用于后续模拟回复。');
});});
el('correctAnswer').addEventListener('input',invalidateCorrection);
el('confirmCorrection').addEventListener('click',()=>action(async()=>{if(!revision)return;render(await call('confirm_correction',currentStore,revision));invalidateCorrection();el('correctionResult').textContent='已用于模拟回复。可在当前对话再问一次；开始新对话时，先说明适用商品，再问刚才的问题。';tell('知识已更新，只影响当前示例店铺和已确认的适用范围。');}));
el('rollback').addEventListener('click',()=>action(async()=>{render(await call('rollback',currentStore));invalidateCorrection();el('correctionResult').textContent='已撤销最近一次问答更新，历史记录保留。';tell('已恢复上一版问答。开始新对话后可再次检查。');}));
el('importProducts').addEventListener('click',()=>action(async()=>{const file=el('productFile').files[0];if(!file)throw new Error('请先选择商品CSV文件。');if(file.size>1000000)throw new Error('请选择1MB以内的商品CSV文件。');render(await call('import_products',currentStore,await file.text()),true);tell('商品表已整理，并开始新对话；只影响本店模拟资料。');}));
el('downloadTemplate').addEventListener('click',()=>{const blob=new Blob(['\ufeff商品名称,价格,库存,材质,规格,别名\n体验黑杆,199,8,枫木,145cm,黑杆、黑棍\n'],{type:'text/csv;charset=utf-8'});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='商品表示例.csv';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);});
window.addEventListener('pywebviewready',async()=>{ready=true;await action(async()=>{render(await call('status'),true);tell('请选择示例店铺，点击“整理示例资料并开始”。');});});
