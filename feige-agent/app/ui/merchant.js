'use strict';
let merchantInfo=null, editContext=null, editKey='', merchantStore='', cardKey='', merchantHead='';
function merchantNode(tag,text,className){const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node;}
function productChoice(choice){return JSON.stringify({product_id:choice.product_id,variant_index:choice.variant_index});}
function installMerchantLayout(){
  const advanced=$('advancedTools');
  function tuck(node,title){if(!node)return;if(node.tagName==='DETAILS'){node.open=false;advanced.append(node);return;}const box=document.createElement('details');box.append(merchantNode('summary',title),node);advanced.append(box);}
  tuck($('modelSettings'));
  tuck($('createForm').closest('details'));
  tuck($('multiTitle').closest('section'),'连接与跨店设置');
  tuck($('knowledgeTitle').closest('section'),'资料导入、历史版本与回退');
  tuck($('salesPanel'),'接待策略');
  tuck($('syncForm').closest('section'),'资料同步');
  tuck($('learningMetrics'),'接待统计');
  tuck($('memorySummary').closest('details'));
  tuck($('advancedCorrection'));
  $('chatTitle').closest('.columns').append($('correctionTitle').closest('section'));
  $('chatTitle').closest('.columns').classList.add('merchant-main');
  $('demoOrder').closest('label').hidden=true;
  $('bridgeHint').hidden=true;
}
function syncMerchantControls(){ $('quickSave').disabled=pending||!editContext; }
async function loadEditContext(force=false){
  const id=$('quickInteraction').value;
  const key=currentStore+'|'+id;
  if(!id){editContext=null;editKey='';$('quickOriginal').textContent='先试聊一个问题，再改正回复。';syncMerchantControls();return;}
  const changed=key!==editKey;
  const result=await invoke('get_correction_context',currentStore,Number(id));
  if(currentStore+'|'+$('quickInteraction').value!==key)return;
  editContext=result;editKey=key;
  $('quickOriginal').textContent='原回复：'+(result.reply.length>140?result.reply.slice(0,140)+'…':result.reply);
  const before=$('quickProduct').value;
  $('quickProduct').replaceChildren(new Option('本店通用',''),...result.choices.map(choice=>new Option(choice.title,productChoice(choice))));
  if(changed||force){
    const choices=result.choices.filter(c=>c.product_id===result.product_id);
    if(choices.length===1)$('quickProduct').value=productChoice(choices[0]);
    $('quickKind').value=result.suggested_kind;$('quickValue').value='';$('quickResult').textContent='';
  }else if(Array.from($('quickProduct').options).some(o=>o.value===before))$('quickProduct').value=before;
  updateQuickKind();syncMerchantControls();
}
function updateQuickKind(){
  const kind=$('quickKind').value;
  $('quickValueLabel').textContent=kind==='stock'?'现在有多少件':kind==='price'?'现在的售价（元）':'应该怎么答';
  $('quickValue').placeholder=kind==='stock'?'卖完填 0':kind==='price'?'填写实际售价':'直接写正确答案';
  $('quickValue').inputMode=kind==='faq'?'text':'decimal';
  $('quickValue').rows=kind==='faq'?3:1;
}
async function renderMerchant(info){
  if(merchantStore!==currentStore){editKey='';editContext=null;merchantStore=currentStore;cardKey='';merchantHead='';$('quickValue').value='';$('quickResult').textContent='';}
  merchantInfo=info;
  $('examples').replaceChildren(...info.examples.slice(0,3).map(text=>{const button=merchantNode('button',text);button.type='button';button.addEventListener('click',()=>{$('question').value=text;$('question').focus();});return button;}));
  fillSelect('quickInteraction',info.interactions.map(row=>new Option(row.question,String(row.id))),'先在左侧问一个问题');
  const head=String(info.interactions[0]?.id||'');
  if(head!==merchantHead){$('quickInteraction').value=head;merchantHead=head;}
  await loadEditContext();
  const cards=(info.cards||[]).filter(c=>!['done','not_applicable','dev','merged','observing'].includes(c.status));
  $('quickCards').hidden=cards.length===0;
  fillSelect('quickCardSelect',cards.map(c=>new Option(c.latest_question||'需要补充资料',String(c.id))),'暂无待补资料');
  renderQuickCard();
  const known=info.merchant_knowledge||{};
  const nodes=[merchantNode('p',`已整理 ${info.products} 件商品。`)];
  if((info.catalog||[]).length)nodes.push(merchantNode('p',info.catalog.slice(0,20).map(p=>p.title).join('、')+(info.catalog.length>20?'等':'')));
  for(const row of known.faq||[]){const item=merchantNode('div','', 'known-answer');item.append(merchantNode('strong',row.question),merchantNode('p',row.answer));nodes.push(item);}
  $('merchantKnowledge').replaceChildren(...nodes);
  const labels=[info.demo?'模拟店铺 · 不发送消息':'试聊 · 不发送消息',`${info.products} 件商品`,info.model_connected?'智能理解已连接':'离线试聊'];
  $('readiness').replaceChildren(...labels.map(text=>merchantNode('span',text)));
}
function renderQuickCard(){
  const id=$('quickCardSelect').value;
  const card=(merchantInfo?.cards||[]).find(c=>String(c.id)===id);
  const key=currentStore+'|'+id;if(key!==cardKey){$('quickCardValue').value='';$('quickCardResult').textContent='';cardKey=key;}
  if(!card){$('quickCardHint').textContent='目前没有需要补充的资料。';$('quickCardForm').hidden=true;return;}
  const unavailable=['semantic','platform_data','media','product_link'].includes(card.card_type);
  $('quickCardHint').textContent=unavailable?'这条需要检查商品关联或消息读取，已保留待处理。':'补上这条资料，系统检查后会用于后续回复。';
  $('quickCardForm').hidden=unavailable;
}
$('quickInteraction').addEventListener('change',()=>operation(()=>loadEditContext(true)));
$('quickKind').addEventListener('change',()=>{$('quickValue').value='';updateQuickKind();});
$('quickCorrectionForm').addEventListener('submit',event=>{event.preventDefault();if(!editContext)return;operation(async()=>{
  const selected=$('quickProduct').value?JSON.parse($('quickProduct').value):{product_id:'',variant_index:null};
  const result=await invoke('save_reply_edit',currentStore,editContext.interaction_id,$('quickKind').value,$('quickValue').value,selected.product_id,selected.variant_index,editContext.versions);
  await refreshInfo();$('quickResult').textContent=result.message;$('quickResult').classList.toggle('edit-error',!result.applied);feedback('');
});});
$('quickCardSelect').addEventListener('change',renderQuickCard);
$('quickCardForm').addEventListener('submit',event=>{event.preventDefault();operation(async()=>{
  const result=await invoke('save_card_answer',currentStore,Number($('quickCardSelect').value),$('quickCardValue').value);
  await refreshInfo();$('quickCardResult').textContent=result.message;$('quickCardResult').classList.toggle('edit-error',!result.applied);feedback('');
});});
installMerchantLayout();
