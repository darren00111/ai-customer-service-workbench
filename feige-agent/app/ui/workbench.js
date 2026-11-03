'use strict';
const $ = id => document.getElementById(id);
let bridge = null;
let currentStore = '';
let session = String(Date.now());
let previewedRevision = null;
let pending = false;
let previewNeedsEvaluation = false;
let previewPassed = false;
let transcriptRows = [];
let currentModel = {};
let channelInfo = {};
let pilotAccountKey = '';
let uncertainDeliveryToken = '';
function canSendLive(info) {
  return !!(info.pilot?.bound && (info.connection?.platform==='pinduoduo' ||
    (info.connection?.platform==='taobao' && info.pilot?.verified_message_metadata)));
}

function showPilot(result) {
  $('pilotOutput').textContent=JSON.stringify(result,null,2);
  const names={disconnected:'已停止连接',awaiting_confirmation:'已识别登录身份，请核对后绑定',observing:'已绑定，可同步测试消息',
    client_not_running:'未检测到运行中的千牛',mapping_required:'已读取千牛结构，会话映射待适配',
    login_or_chat_required:'请登录千牛并打开客服会话',accessibility_unavailable:'千牛无障碍结构暂不可用',probe_timeout:'千牛检查超时'};
  $('pilotStatus').textContent=result.hint || result.detail || names[result.state] || result.state;
  if(result.account?.mall_id==='taobao' && !result.verified_message_metadata) {
    $('pilotStatus').textContent+=' · 消息作者与标识尚待客户端联调，目前只提供预览草稿';
  }
  if(result.account) {
    pilotAccountKey=`${result.account.mall_id}:${result.account.user_id}`;
    $('pilotIdentity').textContent=channelInfo.connection?.platform==='taobao' ? `千牛账号：${result.account.name}${result.buyer ? ' · 当前顾客：'+result.buyer : ''}` : `店铺：${result.account.name}（${result.account.mall_id}） · 客服账号：${result.account.user_id}`;
  } else {pilotAccountKey='';$('pilotIdentity').textContent='请先检查已登录店铺。';}
}
$('chooseQianniuClient').addEventListener('click',()=>operation(async()=>{
  const result=await invoke('choose_qianniu_client');
  feedback(result.hint);
}));
for(const [id,action] of [['pilotLogin','open_login'],['pilotInspect','inspect'],['pilotPoll','poll'],['pilotStop','stop'],['inspectQianniu','inspect_qianniu']]) {
  $(id).addEventListener('click',()=>operation(async()=>{
    const result=await invoke('pilot_command',currentStore,action);
    $('pilotReviewed').checked=false;showPilot(result);await refreshInfo();
  }));
}
$('pilotBind').addEventListener('click',()=>operation(async()=>{
  showPilot(await invoke('pilot_command',currentStore,'bind',pilotAccountKey,$('pilotReviewed').checked));
  $('pilotReviewed').checked=false;await refreshInfo();
}));
$('sendLiveDraft').addEventListener('click',()=>operation(async()=>{
  const result=await invoke('send_live_draft',currentStore,$('channelSelect').value,$('liveSendReviewed').checked);
  $('liveSendReviewed').checked=false;await refreshInfo();
  $('channelView').textContent=JSON.stringify(result,null,2);
  feedback(result.hint,!result.sent);
}));
$('captureProducts').addEventListener('click',()=>operation(async()=>{
  const result=await invoke('capture_product_revision',currentStore);
  const row=await invoke('preview_revision',currentStore,result.revision_id);
  await refreshInfo();$('revisionSelect').value=String(row.id);showPreview(row);feedback(result.hint);
  let parent=$('preview').parentElement;
  while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}
  $('preview').scrollIntoView({block:'center'});
}));
$('closeUncertainDelivery').addEventListener('click',()=>operation(async()=>{
  const result=await invoke('close_uncertain_delivery',currentStore,$('channelSelect').value,uncertainDeliveryToken,$('uncertainReviewed').checked);
  await refreshInfo();showChannel(await invoke('get_imported_conversation',currentStore,$('channelSelect').value));feedback(result.hint);
}));
$('channelMonitorForm').addEventListener('submit',event=>{
  event.preventDefault();operation(async()=>{
    const mode=$('channelMonitorMode').value;
    await invoke('start_channel_monitor',currentStore,mode,mode==='automatic' ? [$('monitorConversation').value] : [],$('monitorReviewed').checked);
    $('monitorReviewed').checked=false;await refreshInfo();feedback('持续接待检查已启动，可在下方查看会话与草稿。');
  });
});
$('stopChannelMonitor').addEventListener('click',()=>operation(async()=>{
  const state=await invoke('stop_channel_monitor',currentStore);await refreshInfo();feedback(state.stopping ? '正在结束已开始的任务；结束前不能重复启动。' : '已停止持续接待，不会处理后续新消息。');
}));
$('storeAutomationForm').addEventListener('submit',event=>{
  event.preventDefault();operation(async()=>{
    await invoke('set_store_automation',currentStore,true,$('storeAutomationReviewed').checked);
    $('storeAutomationReviewed').checked=false;
    await invoke('start_channel_monitor',currentStore,'store',[],true);
    await refreshInfo();feedback('已开启本店全店自动接待，售后进入本机人工待办。');
  });
});
$('disableStoreAutomation').addEventListener('click',()=>operation(async()=>{
  await invoke('set_store_automation',currentStore,false,false);
  $('storeAutomationReviewed').checked=false;await refreshInfo();feedback('本店全店自动接待已关闭。');
}));
function syncStoreAutomationControls(){
  $('enableStoreAutomation').disabled=pending || !channelInfo.store_automation?.can_enable || !$('storeAutomationReviewed').checked;
  $('disableStoreAutomation').disabled=pending || !channelInfo.store_automation?.enabled;
}
$('storeAutomationReviewed').addEventListener('change',syncStoreAutomationControls);

function renderCommonPreview() {
  const row=(channelInfo.common_catalog?.products || []).find(item=>String(item.revision)===$('commonSelect').value);
  $('commonPreview').textContent=row ? JSON.stringify({商品:row.product_key,版本:row.revision,来源:row.source,属性:row.attributes},null,2) : '本商家暂无共用商品。';
}
function renderChannels(info) {
  channelInfo=info;
  const connection=info.connection || {};
  $('pilotPanel').hidden=!['taobao','pinduoduo'].includes(connection.platform);
  $('pddPilot').hidden=connection.platform!=='pinduoduo';$('taobaoPilot').hidden=connection.platform!=='taobao';
  $('productCapturePanel').hidden=connection.platform!=='pinduoduo';
  const historical=info.pilot?.history_capture || {};
  $('historyCaptureStatus').hidden=connection.platform!=='pinduoduo';
  $('historyCaptureStatus').textContent=`本次已读取 ${historical.pages || 0} 页历史，生成 ${historical.candidate_versions || 0} 个待审核问答版本。请在店铺资料中核对、评测后启用。${historical.learning_error ? ' '+historical.learning_error : ''}`;
  const capture=info.pilot?.catalog_capture || {};
  $('productCaptureStatus').textContent=`已采集 ${capture.products || 0} 件商品${capture.error ? ' · '+capture.error : ''}；价格、库存和经营政策仍需核对。`;
  $('pilotLogin').textContent=connection.platform==='taobao' ? '打开已安装的千牛客户端' : '打开拼多多独立登录窗口';
  $('liveSendPanel').hidden=true;
  $('uncertainDeliveryPanel').hidden=true;uncertainDeliveryToken='';$('uncertainReviewed').checked=false;
  $('liveSendReviewed').checked=false;
  $('connectionStatus').textContent=`${connection.name || ''} · ${connection.status || ''}${info.merchant_id ? '\n商家：'+info.merchant_id : ''}`;
  $('commonPanel').hidden=!!info.demo; $('channelPanel').hidden=!!info.demo;
  fillSelect('commonSelect',(info.common_catalog?.products || []).map(row=>new Option(`${row.product_key} · #${row.revision} · ${row.source}`,String(row.revision))),'暂无共用商品');
  fillSelect('localProductSelect',(info.catalog || []).map(row=>new Option(`${row.title} · ${row.product_id}`,row.product_id)),'请先在本店启用商品');
  fillSelect('channelSelect',(info.channel_threads || []).map(row=>new Option(`${row.paused ? '已暂停 · ' : ''}${row.conversation_id} · ${row.latest_content?.slice(0,35) || ''}`,row.conversation_id)),'尚未导入会话');
  fillSelect('monitorConversation',(info.channel_threads || []).filter(row=>row.live && !row.paused).map(row=>new Option(row.conversation_id,row.conversation_id)),'请先采集一条真实测试消息');
  const monitor=info.channel_monitor || {};
  $('channelMonitorStatus').textContent=`${monitor.stopping ? '正在结束上一轮接待' : monitor.running ? '持续接待运行中' : '持续接待已停止'}${monitor.mode==='store' ? ' · 本店全店自动接待' : monitor.mode==='automatic' ? ' · 指定测试顾客自动收发' : ''} · 已生成 ${monitor.generated || 0} 条草稿 · 已确认发送 ${monitor.confirmed || 0} 条${monitor.error ? '\n'+monitor.error : ''}`;
  const automation=info.store_automation || {};
  $('storeAutomationStatus').textContent=`${automation.enabled ? '本店全店自动接待设置已开启，任务状态见上方。' : '本店全店自动接待设置已关闭。'}${!automation.enabled && (automation.requirements || []).length ? '\n开启前需完成：'+automation.requirements.join('；') : ''}`;
  syncStoreAutomationControls();
  $('commonLinks').textContent=JSON.stringify((info.common_catalog?.links || []).map(row=>({本店商品:row.product_id,共用版本:row.shared_revision,生成的店铺知识版本:row.target_revision})),null,2);
  renderCommonPreview();
}
function showChannel(thread) {
  const uncertain=(thread.deliveries || []).find(row=>['delivery_unknown','awaiting_echo'].includes(row.state));
  uncertainDeliveryToken=uncertain?.token || '';
  $('uncertainDeliveryPanel').hidden=!uncertain;$('uncertainReviewed').checked=false;
  $('liveSendPanel').hidden=!(thread.live && canSendLive(channelInfo) && thread.draft && !thread.paused);
  $('liveSendReviewed').checked=false;
  $('channelView').textContent=JSON.stringify({平台:thread.platform,店铺:thread.store_id,会话:thread.conversation_id,
    本地草稿暂停:!!thread.paused,发送记录:thread.deliveries || [],平台商品引用:thread.product_references || [],已观察历史仅作上下文:thread.observed_history || [],原文:thread.messages.map(row=>({角色:row.role,消息:row.content})),
    草稿:thread.draft ? {回复:thread.draft.draft_reply,处理方式:thread.draft.action,已发送:false,
      理解方式:thread.draft.mode,模型连接:thread.draft.model_connected,依据:thread.draft.evidence} : thread.draft_stale ? '店铺资料已更新，请重新生成草稿。' : '暂无草稿'},null,2);
}
$('commonSelect').addEventListener('change',()=>{$('linkReviewed').checked=false;renderCommonPreview();});
$('localProductSelect').addEventListener('change',()=>{$('linkReviewed').checked=false;});
$('channelSelect').addEventListener('change',()=>{$('channelView').textContent='点击查看以读取当前会话的原文与草稿。';$('liveSendPanel').hidden=true;$('liveSendReviewed').checked=false;$('uncertainDeliveryPanel').hidden=true;uncertainDeliveryToken='';$('uncertainReviewed').checked=false;});
$('commonForm').addEventListener('submit',event=>{
  event.preventDefault();operation(async()=>{
    const saved=await invoke('save_common_product',currentStore,$('commonKey').value.trim(),$('commonContent').value,$('commonSource').value.trim(),$('commonReviewed').checked);
    $('commonReviewed').checked=false;await refreshInfo();$('commonSelect').value=String(saved.revision);renderCommonPreview();
    feedback('共用商品版本已保存，各店回复仍使用原有已启用资料。');
  });
});
$('linkForm').addEventListener('submit',event=>{
  event.preventDefault();operation(async()=>{
    const result=await invoke('apply_common_product',currentStore,$('localProductSelect').value,Number($('commonSelect').value),$('linkReviewed').checked);
    $('linkReviewed').checked=false;await refreshInfo();showPreview(result.revision);
    feedback(`同款关联已记录，生成待启用商品版本。保留本店属性：${result.preserved_local.join('、') || '无冲突'}。请在店铺资料中核对并启用。`);
    $('preview').scrollIntoView({block:'center'});
  });
});
$('channelFile').addEventListener('change',async()=>{
  const file=$('channelFile').files[0];if(!file)return;
  if(file.size>5_000_000){feedback('会话文件限5MB',true);return;}
  try{$('channelContent').value=await file.text();}catch(_){feedback('读取文件失败，请重试。',true);}
});
$('channelImportForm').addEventListener('submit',event=>{
  event.preventDefault();operation(async()=>{
    const result=await invoke('import_conversations',currentStore,$('channelContent').value);
    await refreshInfo();$('channelView').textContent='会话已导入，请查看原文后生成草稿。';
    feedback(`已导入 ${result.inserted} 条新消息，重复导入不会重复新增。未向平台发送消息。`);
  });
});
$('viewChannel').addEventListener('click',()=>operation(async()=>showChannel(await invoke('get_imported_conversation',currentStore,$('channelSelect').value))));
$('draftChannel').addEventListener('click',()=>operation(async()=>{
  await invoke('generate_imported_draft',currentStore,$('channelSelect').value);
  await refreshInfo();showChannel(await invoke('get_imported_conversation',currentStore,$('channelSelect').value));
  feedback('草稿已生成并记录到本店接待记录，可继续纠错验证；没有发送到平台。');
}));
$('pauseChannel').addEventListener('click',()=>operation(async()=>{
  const row=await invoke('get_imported_conversation',currentStore,$('channelSelect').value);
  await invoke('pause_imported_conversation',currentStore,row.conversation_id,!row.paused);
  await refreshInfo();showChannel(await invoke('get_imported_conversation',currentStore,row.conversation_id));
  feedback(row.paused ? '已恢复本地草稿生成。' : '已暂停本地草稿，请在原平台处理顾客咨询。');
}));

function renderModel(model) {
  currentModel = model || {};
  const status = !currentModel.enabled ? '离线规则验证' : currentModel.connected ? `千问已验证 · ${currentModel.model}` : '千问待验证';
  $('modelBadge').textContent = status;
  $('modelStatus').textContent = currentModel.error || status;
  // Periodic refresh must never overwrite settings while the user is editing.
  if (!$('modelSettings').contains(document.activeElement)) {
    $('modelName').value = currentModel.model || 'qwen3.8-flash';
    $('modelEndpoint').value = currentModel.endpoint || '';
    $('modelEnabled').checked = !!currentModel.enabled;
  }
}
$('modelForm').addEventListener('submit', event => {
  event.preventDefault(); operation(async () => {
    try {
      renderModel(await invoke('save_model_settings', $('modelEndpoint').value.trim(), $('modelName').value.trim(), $('modelKey').value.trim(), $('modelEnabled').checked));
    } finally { $('modelKey').value = ''; }
    await refreshInfo(); feedback('本机配置已保存。点击验证连接可检查密钥与模型权限。');
  });
});
$('testModel').addEventListener('click', () => operation(async () => {
  const result = await invoke('test_model_connection'); renderModel(result);
  feedback(`千问连接成功，本次验证耗时 ${result.metrics.latency_ms} 毫秒。`);
}));

function feedback(message, error = false) {
  $('feedback').textContent = message;
  $('feedback').classList.toggle('error', error);
}
async function invoke(method, ...args) {
  if (!bridge) throw new Error('请从桌面程序打开此窗口，网页预览不会执行操作。');
  const result = await bridge[method](...args);
  if (!result.ok) throw new Error(result.error || '操作未完成');
  return result.data;
}
async function operation(fn) {
  if (pending) return;
  pending = true;
  const controls = Array.from(document.querySelectorAll('button, select'));
  const prior = controls.map(el => el.disabled);
  controls.forEach(el => { el.disabled = true; });
  try { await fn(); } catch (error) { feedback(error.message, true); }
  finally {
    controls.forEach((el, index) => { el.disabled = prior[index]; });
    pending = false;
    syncActivate();
    syncStoreAutomationControls();
    if(typeof syncMerchantControls==='function')syncMerchantControls();
  }
}
function syncActivate() {
  $('activateButton').disabled = pending || !previewedRevision || !$('reviewed').checked || (previewNeedsEvaluation && !previewPassed);
  $('olderTranscript').disabled=pending || $('olderTranscript').dataset.more!=='true';
}
function resetPreview() {
  previewedRevision = null;
  previewNeedsEvaluation = false;
  previewPassed = false;
  $('reviewed').checked = false;
  $('preview').textContent = '选择一个版本查看内容。';
  syncActivate();
}
async function loadStores(selectId = '') {
  const stores = await invoke('list_stores');
  $('storeSelect').replaceChildren(...stores.map(shop => new Option(`${shop.platform_name ? shop.platform_name + ' · ' : ''}${shop.name}`, shop.store_id)));
  if (selectId) $('storeSelect').value = selectId;
  $('storeSelect').disabled = false;
  await selectStore();
}
async function selectStore() {
  currentStore = $('storeSelect').value;
  pilotAccountKey='';$('pilotReviewed').checked=false;$('liveSendReviewed').checked=false;
  $('monitorReviewed').checked=false;
  $('storeAutomationReviewed').checked=false;
  $('pilotIdentity').textContent='请先检查已登录店铺。';$('pilotStatus').textContent='尚未检查当前店铺。';
  $('pilotOutput').textContent='尚未检查当前店铺。';$('liveSendPanel').hidden=true;
  session = typeof crypto.randomUUID==='function' ? crypto.randomUUID() : String(Date.now())+Math.random();
  $('messages').replaceChildren();
  $('decision').textContent = '试聊后显示。';
  $('demoOrder').checked = false;
  $('demoOrder').disabled = currentStore !== 'demo_billiards';
  $('memorySummary').textContent = '试聊后显示已确认条件和原文引用。';
  $('transcript').textContent = '原文按当前店铺与会话独立保存。';
  transcriptRows=[]; $('olderTranscript').disabled=true; $('olderTranscript').dataset.more='false';
  for(const id of ['syncPath','correctionText','importContent','source','question']) $(id).value='';
  for(const id of ['commonKey','commonContent','commonSource','channelContent']) $(id).value='';
  $('commonReviewed').checked=false; $('linkReviewed').checked=false; $('channelFile').value='';
  $('channelView').textContent='选择本店导入会话后查看原文与草稿。';
  $('importFile').value='';
  $('evaluation').textContent='人工纠错生成的版本需先通过回归验证，再确认启用。';
  resetPreview();
  await refreshInfo();
}
async function refreshInfo() {
  const info = await invoke('get_info', currentStore);
  renderModel(info.model);
  renderSales(info);
  renderChannels(info);
  const labels = [info.demo ? '模拟店铺' : '客户店铺', `已启用商品 ${info.products} 件`,
    `资料不完整 ${info.missing.length} 件`, info.mode || '离线规则验证', '订单与物流未连接', '试聊不发送到平台'];
  $('readiness').replaceChildren(...labels.map(text => {
    const item = document.createElement('span'); item.textContent = text; return item;
  }));
  $('examples').replaceChildren(...info.examples.map(text => {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = text;
    button.addEventListener('click', () => { $('question').value = text; $('question').focus(); });
    return button;
  }));
  const kinds = {products:'商品', business:'店铺政策', faq:'常见问答'};
  const selectedRevision = $('revisionSelect').value;
  $('revisionSelect').replaceChildren(...(info.revisions.length ? info.revisions.map(row =>
    new Option(`#${row.id} · ${kinds[row.kind]} · ${row.active ? '使用中' : '未启用'} · ${row.source}`, String(row.id))) :
    [new Option('暂无版本', '')]));
  if (Array.from($('revisionSelect').options).some(o => o.value === selectedRevision)) $('revisionSelect').value = selectedRevision;
  $('gaps').textContent = JSON.stringify({商品缺失字段: info.missing, 待补充问题: info.gaps}, null, 2);
  fillSelect('interactionSelect', info.interactions.map(row => new Option(`#${row.id} · ${row.question.slice(0,60)}`, String(row.id))), '先完成一次试聊');
  fillSelect('feedbackSelect', info.feedback.map(row => new Option(`#${row.id} · ${row.status === 'retired' ? '已停用' : row.status === 'active' ? '已启用' : '待处理'} · ${row.question.slice(0,45)}`, String(row.id))), '暂无纠错案例');
  renderRecords('syncList', info.sync_sources, row => ({text:`${row.path}\n${row.enabled ? '检查中' : '已暂停'} · ${row.auto_publish ? '商品可自动启用' : '变化后待核对'}${row.last_error ? '\n' + row.last_error : ''}`,
    button:row.enabled ? '暂停此来源' : '恢复此来源', action:async()=>{await invoke('enable_sync',currentStore,row.id,!row.enabled); await refreshInfo();}}));
  renderCards(info);
  if(typeof renderMerchant==='function')await renderMerchant(info);
  const metric = info.metrics;
  $('learningMetrics').replaceChildren(...[`本地记录 ${metric.total} 次`, `资料齐备答复 ${metric.grounded_replies} 次`, `补问 ${metric.followups} 次`, `人工分流 ${metric.handoffs} 次`].map(text=>{
    const item=document.createElement('span'); item.textContent=text; return item;
  }));
}
function renderSales(info) {
  const stats=info.commerce;
  if(!stats) return;
  const policy=stats.policy;
  if(!$('salesPanel').contains(document.activeElement)) {
    $('salesEnabled').checked=policy.enabled;
    $('salesGoal').value=policy.goal; $('salesTone').value=policy.tone;
    $('salesNext').value=policy.next_step; $('salesMode').value=policy.learning_mode;
    $('salesSamples').value=policy.min_samples; $('salesDays').value=policy.observation_days;
    $('salesFocus').value=policy.focus_attributes.join('、');
    $('salesPreferred').replaceChildren(...(info.catalog || []).map(product=>{
      const option=new Option(product.title,product.product_id); option.selected=policy.preferred_product_ids.includes(product.product_id); return option;
    }));
  }
  const labels={none:'回答后等待',offer_help:'适时提供选购帮助'};
  const lines=[`已记录真实确认接待 ${stats.live_receptions} 次，模拟接待 ${stats.simulation_receptions} 次；导入结果 ${stats.reported_events} 条，其中来源已确认 ${stats.trusted_events} 条。`];
  for(const [key,group] of Object.entries(stats.groups)) lines.push(`${labels[key]}：完整观察 ${group.sessions} 段会话，观察到已付款且未退款 ${group.paid_sessions} 段。`);
  lines.push(stats.auto_applied_variant ? `当前按本店授权自动选择：${labels[stats.auto_applied_variant]}。` : stats.recommended_variant ? `可评估的建议：${labels[stats.recommended_variant]}。` : '尚无足够且完整的数据支持调整，保持本店设置或继续对照。');
  lines.push(stats.note); $('salesSummary').textContent=lines.join('\n');
  $('salesInsights').replaceChildren(...(info.learning_insights || []).map(item=>{
    const row=document.createElement('p'); row.textContent=`待补充：${item.detail.question}（出现 ${item.detail.occurrences} 次）`; return row;
  }));
}
$('salesForm').addEventListener('submit',event=>{
  event.preventDefault(); operation(async()=>{
    await invoke('save_sales_policy',currentStore,{enabled:$('salesEnabled').checked,goal:$('salesGoal').value,
      tone:$('salesTone').value,next_step:$('salesNext').value,learning_mode:$('salesMode').value,
      min_samples:Number($('salesSamples').value),observation_days:Number($('salesDays').value),
      preferred_product_ids:Array.from($('salesPreferred').selectedOptions).map(option=>option.value),
      focus_attributes:$('salesFocus').value.split(/[,，、]/).map(x=>x.trim()).filter(Boolean)});
    await refreshInfo(); feedback('本店策略已保存为新版本，下一轮生效；可在知识版本中回退。');
  });
});
$('refreshLearning').addEventListener('click',()=>operation(async()=>{
  const result=await invoke('refresh_learning',currentStore); await refreshInfo();
  feedback(`学习建议已整理，新增 ${result.generated_revisions.length} 个待核对知识版本。`);
}));
const kindLabels = {products:'商品', business:'店铺政策', faq:'常见问答'};
let cardEditorKey='';
function renderCards(info) {
  const summary = info.card_summary || {};
  $('cardSummary').textContent = `未关闭 ${summary.open || 0} 张 · 已补全 ${summary.done || 0} · 不适用 ${summary.not_applicable || 0} · 转开发 ${summary.dev || 0}（分别统计）`;
  fillSelect('cardSelect', (info.cards || []).map(c => new Option(
    `#${c.id} · ${c.type_label} · ${c.status_label} · ${c.occurrences}次 · ${(c.latest_question || c.fields.join('、')).slice(0,40)}`, String(c.id))), '暂无问题卡片');
  const catalog = (info.catalog || []).map(p => new Option(`${p.title}（${p.product_id}）`, p.product_id));
  fillSelect('cardProduct', catalog.map(o => new Option(o.text, o.value)), '本店暂无已生效商品');
  fillSelect('cardFormProduct', catalog.map(o => new Option(o.text, o.value)), '本店暂无已生效商品');
  const product = $('overrideProduct').value;
  $('overrideProduct').replaceChildren(new Option('不修改',''), ...catalog);
  if (Array.from($('overrideProduct').options).some(o=>o.value===product)) $('overrideProduct').value = product;
  $('publishHistory').textContent = (info.publish_history || []).length ? info.publish_history.map(r =>
    `${r.created_at.slice(0,19).replace('T',' ')} · ${kindLabels[r.kind]} · ${r.action==='rollback'?'回退':'发布'} #${r.from_revision ?? '无'} → #${r.to_revision} · ${r.operator}${r.reason ? ' · ' + r.reason : ''}`).join('\n') : '暂无发布记录。';
}
async function showCard() {
  if (!$('cardSelect').value) { $('cardDetail').textContent = '选择卡片查看原问题、已回复内容、缺口与处理记录。'; return; }
  const c = await invoke('get_card', currentStore, Number($('cardSelect').value));
  const editorKey=currentStore+'|'+c.id;
  if(cardEditorKey!==editorKey){$('cardFormValue').value='';$('cardFormSource').value='';cardEditorKey=editorKey;}
  $('cardFormQuestion').value=c.latest_question || '';
  $('cardFormProduct').value=c.product_id || $('cardFormProduct').options[0]?.value || '';
  $('cardFormProduct').disabled=Boolean(c.product_id);
  const attribute=c.fields.find(f=>f.startsWith('attributes.'));
  $('cardFormField').value=attribute ? attribute.slice('attributes.'.length) : '';
  $('cardFormType').value=attribute ? 'attribute' : c.fields.includes('lead_time') || c.fields.includes('origin') ? 'shipping' : 'faq';
  $('cardFormShippingField').value=c.fields.includes('origin') ? 'origin' : 'lead_time';
  syncCardForm();
  const lines = [`#${c.id} ${c.type_label} · ${c.status_label}${c.owner ? ' · 负责人 ' + c.owner : ''}`,
    `商品：${c.product_id || c.remote_product || '未确定'}；缺口：${c.fields.join('、')}；出现 ${c.occurrences} 次；最近 ${String(c.last_seen).slice(0,19).replace('T',' ')}`,
    `顾客侧处理：${c.customer_state || '—'}`, `建议／说明：${c.note}`, `隐私：${c.privacy}`,
    c.revision_id ? `关联候选版本：#${c.revision_id}` : '尚未关联候选版本', '', '原问题与已回复内容：',
    ...c.evidence.map(e => `· [${e.session_id || '无会话'}${e.channel ? ' · ' + e.channel : ''}] ${e.question}\n  工具回复：${e.reply || '—'}`)];
  if (c.retest) lines.push('', `复测（${c.retest.mode === 'model_regression' ? '真实模型' : '离线规则'}）：${c.retest.passed ? '通过' : '未通过'}`,
    ...c.retest.cases.map(r => `· ${r.passed ? '✓' : '✗'} ${r.origin}：${r.question} → ${r.action}${r.missing.length ? '（仍缺 ' + r.missing.join('、') + '）' : ''}`));
  if (c.observation) lines.push('', `新会话观察：${c.observation.real_reception ? '真实接待' : '构造或草稿会话，非真实效果'} · ${c.observation.answer}`);
  lines.push('', '处理记录：', ...c.events.map(e => `· ${String(e.created_at).slice(0,19).replace('T',' ')} ${e.action}${e.operator ? ' · ' + e.operator : ''}`));
  $('cardDetail').textContent = lines.join('\n');
}
function cardPayload(action) {
  if(action==='fill_form')return {form:$('cardFormType').value,question:$('cardFormQuestion').value,
    product_id:$('cardFormProduct').value,field:$('cardFormType').value==='shipping' ? $('cardFormShippingField').value : $('cardFormField').value,
    value:$('cardFormValue').value,source:$('cardFormSource').value};
  if (action === 'assign') return {owner: $('cardOwner').value};
  if (action === 'supplement') return {kind: $('cardKind').value, content: $('cardContent').value, source: `问题卡片 #${$('cardSelect').value} 补充资料`};
  if (action === 'link_existing') return {faq_index: Number($('cardFaqIndex').value)};
  if (action === 'map_product') return {product_id: $('cardProduct').value};
  if (action === 'retest') return {variants: $('cardVariants').value, use_model: $('cardUseModel').checked};
  if (action === 'correct_intent') return {expected_action: $('cardExpected').value, expected_contains: $('cardExpectedText').value};
  if (action === 'merge') return {target_id: Number($('cardMergeTarget').value)};
  if (action === 'not_applicable' || action === 'dev') return {reason: $('cardReason').value};
  return {};
}
const cardMessages = {assign:'已分配负责人。', supplement:'已生成候选版本，卡片进入待复测；补充资料本身不算补全。',
  fill_form:'已保存候选资料，请继续复测并核对发布；正式回复尚未改变。',
  link_existing:'已生成包含该问法的候选问答，请复测。', map_product:'已生成商品映射候选版本，请复测。',
  unlink:'已解除候选关联。', correct_intent:'已加入回归案例并转开发修正理解规则。', merge:'已合并，原会话证据全部保留。',
  not_applicable:'已标记不适用。', dev:'已转开发排查。'};
function fillSelect(id, options, empty) {
  const before = $(id).value;
  $(id).replaceChildren(...(options.length ? options : [new Option(empty,'')]));
  if (Array.from($(id).options).some(o=>o.value===before)) $(id).value=before;
}
function renderRecords(id, rows, present) {
  $(id).replaceChildren();
  if (!rows.length) { $(id).textContent='暂无记录。'; return; }
  rows.forEach(row=>{
    const item=document.createElement('div'); item.className='record-item';
    const view=present(row); const text=document.createElement('p'); text.textContent=view.text;
    const button=document.createElement('button'); button.type='button'; button.className='secondary'; button.textContent=view.button;
    button.addEventListener('click',()=>operation(view.action)); item.append(text,button); $(id).append(item);
  });
}
function addMessage(role, content, mode = '') {
  const row = document.createElement('div'); row.className = `message ${role}`;
  const label = document.createElement('small'); label.textContent = role === 'buyer' ? '顾客（模拟）' : `客服（${mode || '离线规则'}）`;
  row.append(label, document.createTextNode(content)); $('messages').append(row);
  $('messages').scrollTop = $('messages').scrollHeight;
}
$('chatForm').addEventListener('submit', event => {
  event.preventDefault();
  operation(async () => {
    const text = $('question').value.trim();
    const result = await invoke('chat', currentStore, text, session, false, $('demoOrder').checked);
    addMessage('buyer', text); addMessage('agent', result.draft_reply, result.model_error ? '模型连接异常' : result.model_connected ? result.mode : '离线规则');
    $('question').value = '';
    const actionNames = {reply:'已回答', ask_followup:'待补充', handoff:'售后或人工请求'};
    $('decision').textContent = JSON.stringify({处理结果:actionNames[result.action], 理解方式:result.mode, 模型状态:result.model_status, 调用统计:result.model_metrics, 意图:result.intents,
      已确认条件: Object.fromEntries(Object.entries(result.state).filter(([k]) => !['messages','product_history'].includes(k))),
      缺失信息:result.missing, 回答依据:result.evidence, 已解决:result.automatic_resolution}, null, 2);
    $('memorySummary').textContent = JSON.stringify({摘要:result.state.summary, 条件变更:result.state.condition_changes,
      相似问答候选:result.retrieval_candidates},null,2);
    await refreshInfo();
    $('interactionSelect').value=String(result.interaction_id);
    feedback(result.model_error ? result.model_status : result.model_connected ? '本轮千问语义试聊已完成，回答依据见下方。' : '本轮离线试聊已完成；离线结果不代表模型效果。', result.model_error);
  });
});
$('resetChat').addEventListener('click', () => {
  session = typeof crypto.randomUUID==='function' ? crypto.randomUUID() : String(Date.now())+Math.random();
  $('messages').replaceChildren(); $('decision').textContent = '新会话已开始。';
  $('memorySummary').textContent='新会话尚无摘要。'; $('transcript').textContent='新会话尚无原文。';
  transcriptRows=[]; $('olderTranscript').dataset.more='false'; syncActivate();
});
$('storeSelect').addEventListener('change', () => operation(selectStore));
$('createForm').addEventListener('submit', event => {
  event.preventDefault(); operation(async () => {
    const id = $('newStoreId').value.trim();
    await invoke('create_store', id, $('newStoreName').value.trim(), $('newCategory').value, $('newPlatform').value, $('newMerchantId').value.trim()); await loadStores(id);
    feedback('店铺已创建，自动发送保持关闭。请导入真实资料，按连接状态完成后续验证。');
  });
});
$('importFile').addEventListener('change', async () => {
  const file = $('importFile').files[0];
  if (!file) return;
  if (file.size > 5_000_000) { feedback('单次导入限5MB，请拆分。', true); return; }
  try { $('importContent').value = await file.text(); } catch (_) { feedback('文件读取失败。', true); }
});
$('importForm').addEventListener('submit', event => {
  event.preventDefault(); operation(async () => {
    resetPreview();
    const isCsv = $('importFile').files[0]?.name.toLowerCase().endsWith('.csv');
    const row = await invoke('stage_knowledge', currentStore, $('importKind').value,
      $('importContent').value, $('source').value, isCsv ? 'csv' : 'json');
    await refreshInfo(); $('revisionSelect').value = String(row.id); showPreview(row);
    feedback('资料已生成待启用版本。请核对预览内容后再启用。');
  });
});
function showPreview(row) {
  previewedRevision = row.id; $('reviewed').checked = false;
  previewNeedsEvaluation = row.kind==='faq' && row.payload.some(item=>item.feedback_id);
  previewPassed=false;
  $('preview').textContent = JSON.stringify({版本:row.id, 来源:row.source, 类型:row.kind, 内容:row.payload}, null, 2);
  for(let parent=$('preview').parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
  syncActivate();
}
$('transcriptButton').addEventListener('click',()=>operation(async()=>{
  transcriptRows=await invoke('get_transcript',currentStore,session);
  $('transcript').textContent=JSON.stringify(transcriptRows,null,2);
  $('olderTranscript').dataset.more=String(transcriptRows.length===100);
}));
$('olderTranscript').addEventListener('click',()=>operation(async()=>{
  const older=await invoke('get_transcript',currentStore,session,transcriptRows[0]?.id);
  transcriptRows=older.concat(transcriptRows); $('transcript').textContent=JSON.stringify(transcriptRows,null,2);
  $('olderTranscript').dataset.more=String(older.length===100);
}));
$('exportCases').addEventListener('click',()=>operation(async()=>{
  $('regressionExport').textContent=JSON.stringify(await invoke('export_regressions',currentStore),null,2);
  $('regressionExport').parentElement.open=true; feedback('回归案例已导出，可在下方复制。');
}));
$('chooseSyncFile').addEventListener('click',()=>operation(async()=>{
  const path=await invoke('choose_sync_file'); if(path) $('syncPath').value=path;
}));
$('syncKind').addEventListener('change',()=>{
  $('syncAuto').disabled=$('syncKind').value!=='products'; if($('syncAuto').disabled) $('syncAuto').checked=false;
  $('syncOutcomesTrusted').disabled=$('syncKind').value!=='outcomes';
  if($('syncOutcomesTrusted').disabled) $('syncOutcomesTrusted').checked=false;
});
$('syncForm').addEventListener('submit',event=>{
  event.preventDefault(); operation(async()=>{
    await invoke('register_sync',currentStore,$('syncPath').value,$('syncKind').value,$('syncMode').value,$('syncAuto').checked,Number($('syncInterval').value),$('syncOutcomesTrusted').checked);
    await refreshInfo(); feedback('持续更新来源已保存，将按所选方式检查文件变化。');
  });
});
$('syncNow').addEventListener('click',()=>operation(async()=>{
  $('syncReport').textContent=JSON.stringify(await invoke('sync_now',currentStore),null,2); await refreshInfo();
}));
$('correctionForm').addEventListener('submit',event=>{
  event.preventDefault(); operation(async()=>{
    if(!$('interactionSelect').value) throw new Error('请先选择一次接待记录。');
    const saved=await invoke('record_correction',currentStore,Number($('interactionSelect').value),$('correctionText').value,$('expectedAction').value);
    await refreshInfo(); $('feedbackSelect').value=String(saved.feedback_id); feedback('纠错案例已保存，可生成候选或修正资料后运行回归。');
  });
});
$('proposeCorrection').addEventListener('click',()=>operation(async()=>{
  if(!$('feedbackSelect').value) throw new Error('请先保存或选择纠错案例。');
  const row=await invoke('propose_correction',currentStore,Number($('feedbackSelect').value));
  await refreshInfo(); $('revisionSelect').value=String(row.id); showPreview(row);
  feedback('候选知识已生成。请查看预览，并运行“回归验证预览版本”。');
}));
$('evaluatePreview').addEventListener('click',()=>operation(async()=>{
  if(!previewedRevision) throw new Error('请先预览一个版本。');
  const report=await invoke('evaluate_knowledge',currentStore,previewedRevision,$('useModelEvaluation').checked);
  $('evaluation').textContent=JSON.stringify(report,null,2); previewPassed=report.passed;
  feedback(report.passed?'回归通过，可核对后启用。':'回归未通过或尚无案例，请查看结果。',!report.passed);
}));
$('runRegression').addEventListener('click',()=>operation(async()=>{
  $('evaluation').textContent=JSON.stringify(await invoke('evaluate_knowledge',currentStore,null,$('useModelEvaluation').checked),null,2);
}));
$('retireFeedback').addEventListener('click',()=>operation(async()=>{
  if(!$('feedbackSelect').value) throw new Error('请先选择旧案例。');
  await invoke('retire_feedback',currentStore,Number($('feedbackSelect').value)); await refreshInfo(); feedback('已停用该案例，原记录仍保留。');
}));
$('previewButton').addEventListener('click', () => operation(async () => {
  if (!$('revisionSelect').value) throw new Error('请先导入资料。');
  showPreview(await invoke('preview_revision', currentStore, Number($('revisionSelect').value)));
}));
$('revisionSelect').addEventListener('change', resetPreview);
$('reviewed').addEventListener('change', syncActivate);
$('activateButton').addEventListener('click', () => operation(async () => {
  await invoke('activate_revision', currentStore, previewedRevision, $('reviewed').checked);
  resetPreview(); await refreshInfo(); feedback('所选知识版本已启用，下一轮试聊将使用新资料。');
}));
$('cardSelect').addEventListener('change', () => operation(showCard));
function syncCardForm(){for(const [id,kind] of [['cardFormFaq','faq'],['cardFormAttribute','attribute'],['cardFormShipping','shipping']])$(id).hidden=$('cardFormType').value!==kind;}
$('cardFormType').addEventListener('change',syncCardForm);
document.querySelectorAll('[data-card-action]').forEach(button => button.addEventListener('click', () => operation(async () => {
  if (!$('cardSelect').value) throw new Error('请先选择问题卡片。');
  const action = button.dataset.cardAction;
  const result = await invoke('card_action', currentStore, Number($('cardSelect').value), action, cardPayload(action));
  await refreshInfo(); $('cardSelect').value = String(result.id); await showCard();
  if (action === 'retest') {
    if(result.report.passed){$('revisionSelect').value=String(result.revision_id);showPreview(await invoke('preview_revision',currentStore,result.revision_id));navigateToSection('knowledge');}
    feedback(result.report.passed ? '复测通过，已打开关联资料预览；请核对差异后勾选确认，再发布。' : '复测未通过，请查看结果后修改资料。', !result.report.passed);
  }
  else feedback(cardMessages[action] || '已更新卡片。');
})));
$('overrideForm').addEventListener('submit', event => {
  event.preventDefault();
  operation(async () => {
    if (!$('interactionSelect').value) throw new Error('请先在上方选择需要纠正的接待。');
    const changes = {};
    if ($('overrideIntent').value) changes.intent = $('overrideIntent').value;
    if ($('overrideProduct').value) changes.product_id = $('overrideProduct').value;
    if ($('overrideAnswer').value.trim()) changes.answer = $('overrideAnswer').value.trim();
    if ($('overrideAction').value) changes.action = $('overrideAction').value;
    const result = await invoke('record_override', currentStore, Number($('interactionSelect').value), changes,
      $('overrideReason').value, $('overrideScope').value, $('overrideSpecial').checked, $('overrideConversation').value.trim());
    $('overrideResult').textContent = `修改前：${result.original.action} · 意图 ${result.original.intents.join('、')} · 商品 ${result.original.product_id || '未确定'}\n原回复：${result.original.reply}\n\n修改：${JSON.stringify(result.changes, null, 1)}\n回复预览（未发送）：${result.manual_reply_preview || '—'}${result.card_id ? '\n已生成问题卡片 #' + result.card_id : ''}${result.feedback_id ? '\n已保存回归案例 #' + result.feedback_id : ''}`;
    $('overrideAnswer').value = ''; $('overrideSpecial').checked = false;
    await refreshInfo(); feedback(result.hint);
  });
});
$('rollbackButton').addEventListener('click', () => operation(async () => {
  const result = await invoke('rollback_knowledge', currentStore, $('rollbackKind').value, $('rollbackReason').value);
  $('rollbackReason').value = ''; resetPreview(); await refreshInfo();
  feedback(`已回退到版本 #${result.restored}${result.reopened_cards.length ? '，相关问题卡片已重新打开' : ''}。未发送的旧草稿会重新校验。`);
}));
function navigateToSection(section) {
  const id={connection:'pilotPanel',knowledge:'merchantCatalog',versions:'knowledgeTitle',cards:'quickCards',overrides:'quickCorrectionForm'}[section];
  const target=id && document.getElementById(id);if(!target)return;
  target.hidden=false;
  for(let parent=target;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
  target.scrollIntoView({block:'start'});target.setAttribute('tabindex','-1');target.focus({preventScroll:true});
}
window.addEventListener('pywebviewready', async () => {
  bridge = window.pywebview.api;
  $('bridgeHint').textContent = '本地工作台已连接。';
  try { await loadStores(await invoke('get_initial_store'));navigateToSection(await invoke('get_initial_section')); } catch (error) { feedback(error.message, true); }
  setInterval(async()=>{ if(!pending && document.visibilityState==='visible') {
    try { await refreshInfo(); } catch (_) { /* Explicit operations surface errors. */ }
  } },10000);
});
setTimeout(() => {
  if (!bridge) $('bridgeHint').textContent = '这是界面预览；请从智能客服桌面程序打开“接入与试聊”使用完整功能。';
}, 1200);
