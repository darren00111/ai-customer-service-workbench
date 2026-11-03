const statusMeta = {
  store_monitoring: { label: '自动接待中', tone: 'online', hint: '本店全店自动接待运行中；售后生成本机人工待办。' },
  connected_idle: { label: '已连接，未接待', tone: 'neutral', hint: '店铺已绑定，可开始接待。' },
  acceptance_monitoring: { label: '接待验证中', tone: 'warning', hint: '正在持续读取平台消息；正式接待能力仍需真实验收。' },
  connection_required: { label: '待接入', tone: 'neutral', hint: '请在接入与试聊中准备资料；该平台尚未连接消息。' },
  online: { label: '在线', tone: 'online', hint: '正在监听飞鸽消息。' },
  starting: { label: '启动中', tone: 'warning', hint: '正在打开浏览器并连接飞鸽。' },
  not_logged_in: { label: '需要登录', tone: 'warning', hint: '请在该店铺浏览器窗口完成登录。' },
  wrong_site: { label: '页面异常', tone: 'warning', hint: '飞鸽页面未打开，请重新启动该店铺。' },
  loading: { label: '连接中', tone: 'warning', hint: '页面正在加载，请稍候。' },
  error: { label: '运行异常', tone: 'danger', hint: '连接失败。检查网络后重新启动。' },
  browser_error: { label: '浏览器异常', tone: 'danger', hint: '没有找到可用浏览器或 profile 被占用。' },
  network_error: { label: '网页连接失败', tone: 'danger', hint: '检查网络或代理后重新载入飞鸽页面。' },
  stopped: { label: '未启动', tone: 'neutral', hint: '启动后会自动监听客户消息。' },
  stopping: { label: '正在停止', tone: 'warning', hint: '正在结束已开始的接待任务，请稍候。' },
};

const demoState = {
  version: '1.2.2', configured: 10, online: 7, needs_attention: 2, pending_notifications: 3,
  ts: new Date().toISOString(),
  shops: Array.from({ length: 10 }, (_, i) => {
    const states = ['online', 'online', 'not_logged_in', 'online', 'online', 'online', 'error', 'online', 'stopped', 'online'];
    return {
      store_id: `shop_${String(i + 1).padStart(3, '0')}`,
      name: `店铺${String(i + 1).padStart(2, '0')}`,
      cdp_port: 9222 + i,
      state: states[i], watching: states[i] === 'online',
      counters: { rounds: 128 + i * 7, sent: 18 + i, skipped: 61, errors: i === 6 ? 2 : 0, handoffs: i % 3 },
      page: { hint: statusMeta[states[i]]?.hint || '' },
    };
  }),
  notifications: [
    { event_id: 'demo-1', store_id: 'shop_003', buyer: '顾客 8***2', category: 'handoff_after_sale', summary: '尺码不合适，想申请换货', reason: '退换货/退款类', confidence: 'high', draft_reply: '亲，收到您的售后需求了。麻烦您发送一下订单编号，并说明需要退货、换货还是退款，我帮您核实处理。', created_at: Date.now() / 1000 - 86 },
    { event_id: 'demo-2', store_id: 'shop_007', buyer: '顾客 3***9', category: 'handoff_quality', summary: '收到后发现袖口开线', reason: '质量问题', confidence: 'high', draft_reply: '亲，很抱歉给您带来不便。麻烦您发送一下订单编号和问题位置的清晰照片，我帮您核实。', created_at: Date.now() / 1000 - 310 },
    { event_id: 'demo-3', store_id: 'shop_001', buyer: '顾客 1***5', category: 'handoff_human_request', summary: '请转人工客服', reason: '顾客要求人工', confidence: 'high', draft_reply: '您好，人工客服已收到您的消息，请问具体需要帮您处理什么问题呢？', created_at: Date.now() / 1000 - 660 },
  ],
};

let state = demoState;
let selectedId = demoState.shops[0].store_id;
let apiReady = false;
let busy = false;
let toastTimer;
let embedRequestInFlight = false;
let platformFilter = 'all';
const draftEdits = new Map();

const $ = (id) => document.getElementById(id);

function metaFor(value) { return statusMeta[value] || statusMeta.stopped; }

function cumulativeReplies(shop) {
  const durable = Number(shop?.total_replies ?? shop?.conversations?.total_replies);
  if (Number.isFinite(durable)) return durable;
  return Number(shop?.counters?.sent || 0);
}

function formatTime(value) {
  if (!value) return '刚刚';
  const date = new Date(value * 1000);
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date);
}

function shopName(storeId) {
  return state.shops.find((shop) => shop.store_id === storeId)?.name || storeId;
}

function showToast(message, error = false) {
  const toast = $('toast');
  clearTimeout(toastTimer);
  $('toastMessage').textContent = message;
  toast.setAttribute('role', error ? 'alert' : 'status');
  toast.setAttribute('aria-live', error ? 'assertive' : 'polite');
  toast.className = `toast visible${error ? ' error' : ''}`;
  if (!error) toastTimer = setTimeout(() => { toast.className = 'toast'; }, 3200);
}

function setBusy(value) {
  busy = value;
  $('startAllButton').disabled = value;
  $('refreshButton').disabled = value;
  $('addStoreButton').disabled = value;
  $('loginStoreButton').disabled = value || !selectedId;
  $('storeKnowledgeButton').disabled = value || !selectedId;
  $('connectionStoreButton').disabled = value || !selectedId;
  const selected = state.shops.find((shop) => shop.store_id === selectedId);
  $('storeActionButton').disabled = value || !selected || selected.state==='stopping';
}

function visibleShops() { return state.shops.filter(shop=>platformFilter==='all' || (shop.platform || 'feige')===platformFilter); }
function showsNativeChat(shop){return shop && (!shop.platform || shop.platform==='feige') && ['online','starting','loading'].includes(shop.state);}

function renderShops() {
  const list = $('shopList');
  const focusedStore = document.activeElement?.classList?.contains('shop-item')
    ? document.activeElement.dataset.storeId : null;
  const scrollTop = list.scrollTop;
  const scrollLeft = list.scrollLeft;
  list.replaceChildren();
  $('shopCount').textContent = state.configured;
  $('shopListEmpty').hidden = visibleShops().length>0;
  visibleShops().forEach((shop) => {
    const meta = metaFor(shop.state);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'shop-item';
    button.dataset.storeId = shop.store_id;
    button.setAttribute('aria-current', String(shop.store_id === selectedId));
    button.setAttribute('aria-label', `${shop.name}，${meta.label}`);

    const primary = document.createElement('span');
    primary.className = 'shop-primary';
    const name = document.createElement('span');
    name.className = 'shop-name';
    name.textContent = shop.name;
    const dot = document.createElement('span');
    dot.className = `status-dot ${meta.tone}`;
    dot.setAttribute('aria-hidden', 'true');
    primary.append(name, dot);

    const secondary = document.createElement('span');
    secondary.className = 'shop-secondary';
    const label = document.createElement('span');
    label.textContent = `${shop.connection?.name ? shop.connection.name + ' · ' : ''}${meta.label}`;
    const sent = document.createElement('span');
    sent.textContent = `${cumulativeReplies(shop)} 条回复`;
    secondary.append(label, sent);
    button.append(primary, secondary);
    button.addEventListener('click', () => {
      selectedId = shop.store_id;
      render();
      requestEmbeddedPage(false);
    });
    const item = document.createElement('div');
    item.setAttribute('role', 'listitem');
    item.append(button);
    list.append(item);
  });
  list.scrollTop = scrollTop;
  list.scrollLeft = scrollLeft;
  if (focusedStore) list.querySelector(`[data-store-id="${focusedStore}"]`)?.focus({ preventScroll: true });
}

function detailRow(label, value) {
  const row = document.createElement('div');
  row.className = 'detail-row';
  const key = document.createElement('span');
  key.className = 'detail-label';
  key.textContent = label;
  const content = document.createElement('span');
  content.className = 'detail-value';
  content.textContent = value;
  row.append(key, content);
  return row;
}

function renderSelected() {
  const shop = visibleShops().find((item) => item.store_id === selectedId);
  $('loginStoreButton').disabled=busy || !shop;
  $('loginStoreButton').textContent=shop?.platform==='taobao' ? '打开千牛工作台' : '打开登录入口';
  $('chooseQianniuButton').hidden=shop?.platform!=='taobao';
  $('chooseQianniuButton').disabled=busy;
  $('storeKnowledgeButton').disabled=busy || !shop;
  $('connectionStoreButton').disabled=busy || !shop;
  document.querySelector('.workspace').classList.toggle('overview-mode',!showsNativeChat(shop));
  $('browserHost').hidden=!showsNativeChat(shop);
  if (!shop) {
    $('storeTitle').textContent='添加或选择店铺';$('storeStatus').textContent='未选择';
    $('storeStatus').className='status-pill neutral';
    $('storeHint').textContent='选择平台并添加店铺，再单独登录和开启接待。';
    $('storeActionButton').disabled=true;$('detailList').replaceChildren();
    $('activityEmpty').hidden=false;return;
  }
  selectedId = shop.store_id;
  const meta = metaFor(shop.state);
  $('storeTitle').textContent = shop.name;
  $('storeStatus').textContent = meta.label;
  $('storeStatus').className = `status-pill ${meta.tone}`;
  $('storeHint').textContent = shop.last_error
    ? `${meta.hint} ${shop.last_error}`
    : (shop.page?.hint || meta.hint);

  const action = $('storeActionButton');
  const isActive = shop.state === 'online' || shop.state === 'starting' || shop.state === 'acceptance_monitoring' || shop.state==='store_monitoring';
  action.textContent = shop.state==='stopping' ? '正在停止' : isActive ? '停止监听' : shop.can_start_store_reception ? '启动本店接待' : shop.connection?.runtime_available === false ? '连接并验收' : '启动店铺';
  action.dataset.action = isActive ? 'stop' : 'start';
  action.disabled = busy || shop.state==='stopping';
  $('browserHeading').textContent=shop.platform && shop.platform!=='feige' ? '客服连接' : '飞鸽原页面';
  $('embedRetryButton').textContent=shop.platform && shop.platform!=='feige' ? '查看接入与会话' : '重新载入原页面';

  const counters = shop.counters || {};
  const details = $('detailList');
  details.replaceChildren(
    detailRow('登录／身份',shop.login?.label || '待登录／核对'),
    detailRow('真实验收',shop.acceptance?.label || '未验收'),
    detailRow('知识补全',shop.knowledge_queue?.error || `${shop.knowledge_queue?.open || 0} 项待处理`),
    detailRow('监听状态', shop.watching ? '运行中' : '未运行'),
    detailRow('检查次数', String(counters.rounds || 0)),
    detailRow('累计自动回复', `${cumulativeReplies(shop)} 条`),
    detailRow('本次运行回复', `${counters.sent || 0} 条`),
    detailRow('转人工', `${counters.handoffs || 0} 条`),
    detailRow('异常', shop.channel_runtime ? shop.channel_runtime.monitor.error || shop.channel_runtime.handoff_error || '无' : counters.errors ? `${counters.errors} 次` : '无'),
  );
  $('activityEmpty').hidden = true;
  details.hidden = false;
}

async function openStoreSection(storeId,section) {
  if(!apiReady){showToast('请在桌面程序中管理店铺资料');return;}
  try {const result=await window.pywebview.api.open_workbench(storeId,section);if(!result.ok)showToast(result.error,true);}
  catch(_){showToast('无法打开店铺资料，请重试',true);}
}

function renderKnowledgeQueue() {
  const list=$('knowledgeQueueList');list.replaceChildren();
  const rows=visibleShops().filter(s=>s.knowledge_queue?.open || s.knowledge_queue?.error);
  $('knowledgeQueueCount').textContent=String(rows.reduce((sum,s)=>sum+(s.knowledge_queue?.open || 0),0));
  if(!rows.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='当前平台没有待补知识卡片。';list.append(empty);}
  for(const shop of rows){
    const row=document.createElement('div');row.className='knowledge-queue-row';
    const copy=document.createElement('div');const name=document.createElement('strong');name.textContent=shop.name;
    const info=document.createElement('span');info.className='muted';
    const queue=shop.knowledge_queue;const statuses=queue.by_status || {};
    info.textContent=queue.error || `${queue.open} 项待处理 · 待复测 ${statuses.retest || 0} · 待发布 ${statuses.publish || 0} · 观察中 ${statuses.observing || 0}`;
    const button=document.createElement('button');button.type='button';button.className='button button-secondary';button.textContent='补全本店资料';button.addEventListener('click',()=>openStoreSection(shop.store_id,'cards'));
    copy.append(name,info);row.append(copy,button);list.append(row);
  }
}

function renderNotifications() {
  const list = $('attentionList');
  const focusedEvent = document.activeElement?.dataset?.eventId || null;
  const selectionStart = document.activeElement?.selectionStart;
  list.querySelectorAll('textarea[data-event-id]').forEach((field) => {
    draftEdits.set(field.dataset.eventId, field.value);
  });
  const notifications = state.notifications || [];
  list.replaceChildren();
  $('attentionCount').textContent = notifications.length;
  $('attentionMetric').textContent = notifications.length;
  $('attentionEmpty').hidden = notifications.length > 0;
  list.hidden = notifications.length === 0;

  notifications.forEach((item) => {
    const card = document.createElement('article');
    card.className = 'attention-item';
    const top = document.createElement('div');
    top.className = 'attention-top';
    const source = document.createElement('span');
    source.className = 'attention-store';
    source.textContent = `${shopName(item.store_id)} · ${item.buyer || '顾客'}`;
    const time = document.createElement('time');
    time.className = 'attention-time';
    time.textContent = formatTime(item.created_at);
    top.append(source, time);
    const summary = document.createElement('p');
    summary.className = 'attention-summary';
    summary.textContent = item.summary || '需要人工处理';
    const reason = document.createElement('p');
    reason.className = 'attention-reason';
    const confidenceText = { high: '高', medium: '中', low: '低' }[item.confidence] || '待判断';
    reason.textContent = `${item.reason || item.category || '已转人工'} · 置信度 ${confidenceText}`;
    const draftLabel = document.createElement('label');
    draftLabel.className = 'draft-label';
    draftLabel.htmlFor = `draft-${item.event_id}`;
    draftLabel.textContent = '建议回复';
    const draft = document.createElement('textarea');
    draft.id = `draft-${item.event_id}`;
    draft.className = 'draft-input';
    draft.dataset.eventId = item.event_id;
    draft.rows = 3;
    draft.maxLength = 800;
    draft.value = draftEdits.has(item.event_id)
      ? draftEdits.get(item.event_id)
      : (item.draft_reply || '');
    draft.addEventListener('input', () => draftEdits.set(item.event_id, draft.value));
    const actions = document.createElement('div');
    actions.className = 'draft-actions';
    const send = document.createElement('button');
    send.type = 'button';
    send.className = 'button button-primary draft-send';
    send.textContent = '确认并发送';
    if (item.external_manual) {
      send.disabled = true;
      send.textContent = '请在原平台人工回复';
    }
    send.addEventListener('click', () => sendDraft(item.event_id, draft.value, send));
    const done = document.createElement('button');
    done.type = 'button';
    done.className = 'text-button';
    done.dataset.eventId = item.event_id;
    done.textContent = item.external_manual ? '已在原平台处理' : '已在飞鸽处理';
    done.addEventListener('click', () => markDone(item.event_id));
    actions.append(send, done);
    card.append(top, summary, reason, draftLabel, draft, actions);
    list.append(card);
  });
  if (focusedEvent) {
    const focused = list.querySelector(`textarea[data-event-id="${focusedEvent}"]`)
      || list.querySelector(`button[data-event-id="${focusedEvent}"]`);
    focused?.focus({ preventScroll: true });
    if (focused?.setSelectionRange && Number.isInteger(selectionStart)) {
      focused.setSelectionRange(selectionStart, selectionStart);
    }
  }
}

function renderMetrics() {
  $('onlineMetric').textContent = `${state.online} / ${state.configured}`;
  const replies = state.shops.reduce((sum, shop) => sum + cumulativeReplies(shop), 0);
  $('replyMetric').textContent = String(replies);
  $('version').textContent = state.version ? `v${state.version}` : '桌面控制台';
}

function render() {
  if(!visibleShops().some(s=>s.store_id===selectedId))selectedId=visibleShops()[0]?.store_id || null;
  renderShops();
  renderSelected();
  renderMetrics();
  renderNotifications();
  renderKnowledgeQueue();
}

async function refreshState(announce = false) {
  if (!apiReady) { render(); return; }
  try {
    const next = await window.pywebview.api.get_state();
    state = next;
    if (!state.shops.some((shop) => shop.store_id === selectedId)) selectedId = state.shops[0]?.store_id;
    $('syncLabel').textContent = `已更新 ${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`;
    render();
    const shop = state.shops.find((item) => item.store_id === selectedId);
    const embedded = state.embedded_browser || {};
    if ((shop?.state === 'online' && (!embedded.embedded || embedded.port !== shop.cdp_port)) || (!showsNativeChat(shop) && embedded.embedded)) {
      requestEmbeddedPage(false);
    }
    if (announce) showToast('状态已刷新');
  } catch (error) {
    $('syncLabel').textContent = '连接失败';
    if (announce) showToast('无法读取状态，请稍后重试。', true);
  }
}

async function syncEmbedBounds() {
  if (!apiReady) return;
  const rect = $('browserHost').getBoundingClientRect();
  try {
    await window.pywebview.api.set_embed_bounds(rect.left, rect.top, rect.width, rect.height);
  } catch (error) {
    // The placeholder remains usable if native embedding is unavailable.
  }
}

async function requestEmbeddedPage(announce = true) {
  const shop = state.shops.find((item) => item.store_id === selectedId);
  if (!apiReady || embedRequestInFlight) return;
  if(!showsNativeChat(shop) && state.embedded_browser?.embedded){
    try {
      const result=await window.pywebview.api.release_embedded_page();
      if(result.ok)state.embedded_browser={...state.embedded_browser,embedded:false};
    } catch(_){if(announce)showToast('无法恢复独立客服窗口，请重试',true);return;}
  }
  if(!shop)return;
  if (shop.connection?.runtime_available === false) {
    $('browserHint').textContent = '客服在独立窗口中接待。点击下方“查看接入与会话”，检查本店会话、草稿和接待设置。';
    if(announce){const result=await window.pywebview.api.open_workbench(selectedId);if(!result.ok)showToast(result.error,true);}
    return;
  }
  if (shop.state==='stopped') {
    $('browserHint').textContent='启动店铺后将在这里显示客服原页面。';
    return;
  }
  embedRequestInFlight = true;
  $('browserHint').textContent = '正在载入已验证的飞鸽页面…';
  try {
    await syncEmbedBounds();
    const result = await window.pywebview.api.embed_store(selectedId);
    if (!result.ok) {
      $('browserHint').textContent = `${result.error} 请先启动店铺，再重试。`;
      if (announce) showToast($('browserHint').textContent, true);
    } else {
      $('browserHint').textContent = '飞鸽原页面已载入。';
      if (announce) showToast('飞鸽原页面已重新载入');
    }
  } catch (error) {
    $('browserHint').textContent = '载入失败，请先启动店铺，再重试。';
    if (announce) showToast($('browserHint').textContent, true);
  } finally {
    embedRequestInFlight = false;
  }
}

async function startAll() {
  if (!apiReady) { showToast('预览模式不会启动浏览器'); return; }
  setBusy(true);
  $('startAllButton').querySelector('span').textContent = '正在启动…';
  try {
    const result = await window.pywebview.api.start_all();
    await refreshState();
    await requestEmbeddedPage(false);
    const skipped=(result.results || []).filter(row=>row.attempted===false).length;
    showToast(result.ok ? `已启动 ${result.online}/${result.attempted} 个店铺${skipped ? `；另 ${skipped} 个店铺待连接并开启接待` : ''}` : result.error, !result.ok);
  } catch (error) {
    showToast('无法启动店铺。检查网络后重试。', true);
  } finally {
    $('startAllButton').querySelector('span').textContent = '启动全部店铺';
    setBusy(false);
  }
}

async function toggleStore() {
  const shop = state.shops.find((item) => item.store_id === selectedId);
  if (!shop || busy) return;
  if (!apiReady) { showToast('预览模式不会操作店铺'); return; }
  setBusy(true);
  const action = $('storeActionButton').dataset.action;
  try {
    if(action!=='stop' && shop.connection?.runtime_available===false && !shop.can_start_store_reception) {
      const result=await window.pywebview.api.open_workbench(selectedId);
      showToast(result.ok ? '已打开该店铺的连接与验收页面' : result.error,!result.ok);
      return;
    }
    const result = action === 'stop'
      ? await window.pywebview.api.stop_store(selectedId)
      : await window.pywebview.api.start_store(selectedId);
    await refreshState();
    if(!result.ok && result.state==='connection_required')await window.pywebview.api.open_workbench(selectedId);
    if (result.ok && action !== 'stop' && result.embed && !result.embed.ok) {
      showToast(`店铺已启动；${result.embed.error}`, true);
    } else {
      showToast(result.ok ? (action === 'stop' ? (result.stopping ? '正在结束已开始的接待任务' : '已停止监听') : '店铺已启动') : result.error, !result.ok);
    }
  } catch (error) {
    showToast('无法操作该店铺。检查网络后重试。', true);
  } finally {
    setBusy(false);
  }
}

async function loginSelectedStore() {
  if(!apiReady || !selectedId){showToast('请在桌面程序中选择店铺后登录');return;}
  setBusy(true);
  try {
    let result=await window.pywebview.api.login_store(selectedId);
    if(result.needs_client_path){
      showToast(result.error,true);
      const chosen=await window.pywebview.api.choose_qianniu_client();
      result=chosen.ok ? await window.pywebview.api.login_store(selectedId) : chosen;
    }
    await refreshState();showToast(result.ok ? result.hint : result.error,!result.ok);
  } catch(_){showToast('无法打开登录入口，请检查对应客户端是否可用',true);}
  finally{setBusy(false);}
}

$('chooseQianniuButton').addEventListener('click',async()=>{
  if(!apiReady || busy)return;
  setBusy(true);
  try{
    const result=await window.pywebview.api.choose_qianniu_client();
    showToast(result.ok ? result.hint : result.error,!result.ok);
  }catch(_){showToast('无法选择千牛程序，请重新打开工具后重试',true);}
  finally{setBusy(false);}
});

const platformLoginHints={taobao:'淘宝使用千牛桌面客户端；实际收发需要核对客户端兼容性。',feige:'抖店使用本店独立的 Edge 登录窗口，登录后再核对客服页面与接待资料。',pinduoduo:'拼多多使用本店独立浏览器窗口，登录后再检查身份和消息连接。'};
function closeAddStore(){if(!$('submitAddStore').disabled)$('addStoreDialog').close();}
$('addStoreButton').addEventListener('click',()=>{
  $('addStoreError').textContent='';
  if(platformFilter!=='all')$('newStorePlatform').value=platformFilter;
  $('newStorePlatformHint').textContent=platformLoginHints[$('newStorePlatform').value];
  $('addStoreDialog').showModal();$('newStoreName').focus();
});
$('closeAddStore').addEventListener('click',closeAddStore);
$('addStoreDialog').addEventListener('cancel',event=>{if($('submitAddStore').disabled)event.preventDefault();});
$('newStorePlatform').addEventListener('change',()=>{$('newStorePlatformHint').textContent=platformLoginHints[$('newStorePlatform').value];});
$('addStoreForm').addEventListener('submit',async event=>{
  event.preventDefault();if(!apiReady){$('addStoreError').textContent='请在桌面程序中添加真实店铺。';return;}
  $('submitAddStore').disabled=true;$('addStoreError').textContent='';setBusy(true);
  try {
    const result=await window.pywebview.api.add_store($('newStorePlatform').value,$('newStoreName').value.trim(),$('newMerchantId').value.trim() || 'local',$('newStoreCategory').value.trim());
    if(!result.ok){$('addStoreError').textContent=result.error;return;}
    selectedId=result.store_id;platformFilter=result.platform;$('platformFilter').value=platformFilter;
    await refreshState();$('addStoreDialog').close();$('newStoreName').value='';showToast(result.hint);
  } catch(_){$('addStoreError').textContent='添加结果尚未确认，请刷新店铺列表后再检查，避免重复添加。';}
  finally{$('submitAddStore').disabled=false;setBusy(false);}
});
$('platformFilter').addEventListener('change',()=>{platformFilter=$('platformFilter').value;render();requestEmbeddedPage(false);});
$('loginStoreButton').addEventListener('click',loginSelectedStore);
$('storeKnowledgeButton').addEventListener('click',()=>openStoreSection(selectedId,'knowledge'));
$('connectionStoreButton').addEventListener('click',()=>openStoreSection(selectedId,'connection'));

async function markDone(eventId) {
  if (!apiReady) {
    state.notifications = state.notifications.filter((item) => item.event_id !== eventId);
    renderNotifications();
    showToast('已标记为处理完成');
    return;
  }
  try {
    const result = await window.pywebview.api.mark_notification_done(eventId);
    await refreshState();
    showToast(result.ok ? result.hint || '已标记为处理完成' : result.error, !result.ok);
  } catch (error) {
    showToast('无法更新消息状态，请重试。', true);
  }
}

async function sendDraft(eventId, text, button) {
  const draft = (text || '').trim();
  if (!draft) { showToast('草稿不能为空。', true); return; }
  if (!apiReady) {
    state.notifications = state.notifications.filter((item) => item.event_id !== eventId);
    draftEdits.delete(eventId);
    renderNotifications();
    showToast('草稿已确认发送');
    return;
  }
  button.disabled = true;
  button.textContent = '发送中…';
  try {
    const result = await window.pywebview.api.send_handoff_reply(eventId, draft);
    if (result.ok) {
      draftEdits.delete(eventId);
      await refreshState();
      showToast('草稿已发送，自动回复已恢复');
    } else {
      showToast(result.error || '草稿发送失败。', true);
      button.disabled = false;
      button.textContent = '确认并发送';
    }
  } catch (error) {
    showToast('草稿发送失败，请在飞鸽中处理。', true);
    button.disabled = false;
    button.textContent = '确认并发送';
  }
}

$('refreshButton').addEventListener('click', () => refreshState(true));
async function openExperience() {
  if (!apiReady) { showToast('请从桌面程序打开模拟体验。'); return; }
  try { const result=await window.pywebview.api.open_experience(); if(!result.ok)showToast(result.error,true); }
  catch (_) { showToast('无法打开体验窗口，请重试。',true); }
}
$('experienceButton').addEventListener('click',openExperience);
$('welcomeExperienceButton').addEventListener('click',openExperience);
$('workbenchButton').addEventListener('click', async () => {
  if (!apiReady) { showToast('请在桌面程序中打开接入与试聊'); return; }
  try {
    const result = await window.pywebview.api.open_workbench();
    if (!result.ok) showToast(result.error, true);
  } catch (_) { showToast('无法打开接入与试聊，请重试。', true); }
});
$('startAllButton').addEventListener('click', startAll);
$('storeActionButton').addEventListener('click', toggleStore);
$('embedRetryButton').addEventListener('click', () => requestEmbeddedPage(true));
$('toastClose').addEventListener('click', () => { $('toast').className = 'toast'; });

window.addEventListener('pywebviewready', async () => {
  apiReady = true;
  $('syncLabel').textContent = '正在连接…';
  await syncEmbedBounds();
  await refreshState();
  await requestEmbeddedPage(false);
  setInterval(() => refreshState(false), 3000);
});

const embedResizeObserver = new ResizeObserver(() => syncEmbedBounds());
embedResizeObserver.observe($('browserHost'));

render();
