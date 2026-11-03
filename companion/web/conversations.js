import {$,esc,api,heading,empty,time,relative,icon,modal} from './ui.js';
import {processingHtml,refreshProcessing} from './diagnostics-ui.js';

let view=null,searchTimer;
const selectedName=()=>view?.items.find(c=>c.scope===view.scope)?.name||'聊天记录';
function attachmentHtml(a){
  if(!a.id||a.status!=='ready')return `<div class="qq-attachment unavailable">${icon('files')}<span>${esc(a.name)} · ${esc(a.error||'附件暂不可用，bot尚未取得原件')}</span></div>`;
  const read=a.input;
  let label='已保存 · 尚无模型读取记录';
  if(read?.state==='prepared')label='正在提交模型请求';
  else if(read?.state==='unconfirmed')label='请求未完成 · 未确认模型读取';
  else if(read?.state==='included')label=({pixels:'模型请求已包含图片',vision_note:'模型请求使用了识图笔记',file_reference:'仅提交文件标识 · 尚未读取正文',document_extract:'模型请求已包含提取内容',native_file_reference:'文件已交给云端原生工具'})[read.mode]||'已有模型请求记录';
  const context=read?.content?`<details><summary>${read.mode==='vision_note'?'查看实际使用的识图笔记':read.mode==='native_file_reference'?'查看云端文件使用记录':'查看实际提交的提取内容'}</summary><pre>${esc(read.content)}</pre></details>`:'';
  return `<div class="qq-artifact"><a class="qq-attachment" ${a.image?`data-history-preview="${esc(a.id)}"`:''} href="/api/files/${encodeURIComponent(a.id)}/download" title="文件ID ${esc(a.id)} · SHA256 ${esc(a.hash)}">${a.image?`<img src="/api/files/${encodeURIComponent(a.id)}/preview" alt="${esc(a.name)}" loading="lazy">`:icon('files')}<span>${esc(a.name)} · ${Math.max(1,Math.round(a.size/1024))} KB</span></a><div class="qq-input-status">${esc(label)}${read?`<br>${esc(read.model)} · ${time(read.updated)}`:''}${read?.error?`<br>${esc(read.error)}`:''}${context}</div></div>`;
}
function rowsHtml(rows){
  return rows.map(m=>`<article class="qq-message ${m.role==='assistant'?'assistant':''}" data-message-id="${esc(m.id)}"><span class="avatar" title="${esc(m.name)}">${esc(m.name?.slice(0,1)||'?')}</span><div class="qq-message-main"><div class="message-meta"><strong>${esc(m.name)}</strong><span>${esc(m.sender)}</span><time>${time(m.created)}</time></div><div class="qq-bubble">${m.status==='failed'?`<p class="error-text">发送失败 · ${esc(m.error)}</p>`:''}${m.quote_id?`<div class="qq-quote">${m.quote?`<strong>${esc(m.quote.name)}</strong><span>${esc(m.quote.text||'[附件]')}</span>`:'引用消息不在当前记录中'}</div>`:''}${m.text?`<div class="message-text">${esc(m.text)}</div>`:''}${(m.sources?.length>1||m.sources?.some(p=>p.quote_id))?`<details><summary>查看本轮合并消息及引用来源</summary>${m.sources.map(p=>`<div class="qq-quote"><strong>${esc(p.name)} · ${esc(p.sender)}</strong><span>${esc(p.text)}</span>${p.quote_id?`<small>引用 ${esc(p.quote?.name||'未知')} · ${esc(p.quote?.sender||'')} · ${esc(p.quote_id)}</small><span>${esc(p.quote?.missing?'引用原消息未取得':p.quote?.text||'[附件]')}</span>`:''}</div>`).join('')}</details>`:''}${(m.attachments||[]).map(attachmentHtml).join('')}${processingHtml(m.processing)}</div></div></article>`).join('');
}
function listHtml(){
  const filter=($('#conversation-search')?.value||'').toLowerCase();
  return view.items.filter(c=>(c.name+' '+c.scope+' '+c.preview).toLowerCase().includes(filter)).map(c=>`<button data-conversation="${esc(c.scope)}" class="qq-contact ${c.scope===view.scope?'active':''}"><span class="avatar">${esc(c.private?c.name.slice(0,1):'群')}</span><span class="qq-contact-content"><strong>${esc(c.name)}</strong><small>${esc(c.preview||'[附件]')}</small></span><time>${relative(c.latest)}</time></button>`).join('')||empty('尚无会话记录','chat');
}
async function fetchRows(before){
  const params=new URLSearchParams({scope:view.scope,q:view.query});
  if(before){params.set('before',before.created);params.set('before_id',before.id);}
  else if(view.anchor)params.set('anchor',view.anchor);
  return (await api('messages?'+params)).reverse();
}
export async function conversationsPage(selected){
  const items=await api('conversations'),scope=(typeof selected==='object'?selected.scope:selected)||items[0]?.scope;
  view={items,scope,anchor:typeof selected==='object'?selected.message:null,query:'',rows:[],more:false,busy:false,latest:items.find(c=>c.scope===scope)?.latest_id||''};
  if(scope){view.rows=await fetchRows();view.more=view.rows.length===100;}
  return heading('会话','查看Sol收到和发出的真实消息。')+`<div id="history-root" class="qq-history"><aside class="qq-contacts"><div class="qq-contact-search"><input id="conversation-search" placeholder="搜索会话" aria-label="搜索会话"></div><div id="conversation-items">${listHtml()}</div></aside><section class="qq-thread"><header class="qq-thread-header"><div><strong id="conversation-title">${esc(selectedName())}</strong><small>${esc(scope||'选择一个会话')} · 只读</small></div><input id="history-search" placeholder="搜索当前聊天记录" aria-label="搜索当前聊天记录"></header><div id="history-error" class="error-text" role="status"></div><div id="messages" class="qq-messages" aria-label="聊天记录"><button id="history-more" class="button small" ${view.more?'':'hidden'}>加载更早记录</button><div id="history-rows">${rowsHtml(view.rows)||empty('没有可显示的消息','chat')}</div><div class="readonly-caption">仅展示记录 · 不提供发送框</div></div><button id="history-new" class="button history-new" hidden>有新消息，查看最新</button></section></div>`;
}
function showError(e){if($('#history-error'))$('#history-error').textContent=e?.message||'';}
function paint(scroll){
  $('#history-rows').innerHTML=rowsHtml(view.rows)||empty('没有找到相关消息','chat');
  $('#history-more').hidden=!view.more;
  if(scroll){const box=$('#messages');box.scrollTop=box.scrollHeight;$('#history-new').hidden=true;}
}
export function wireConversations(select){
  if(!$('#history-root'))return;
  $('#history-rows').onclick=e=>{const image=e.target.closest('[data-history-preview]');if(image){e.preventDefault();modal('查看图片',`<img class="modal-image" src="/api/files/${encodeURIComponent(image.dataset.historyPreview)}/preview" alt="聊天图片"><a class="button small-space" href="${image.href}">保存图片</a>`);}};
  $('#conversation-items').onclick=e=>{const b=e.target.closest('[data-conversation]');if(b)select(b.dataset.conversation);};
  $('#conversation-search').oninput=()=>{$('#conversation-items').innerHTML=listHtml();};
  $('#history-search').oninput=()=>{clearTimeout(searchTimer);const current=view;searchTimer=setTimeout(async()=>{if(current!==view||!$('#history-root'))return;view.query=$('#history-search').value.trim();const query=view.query;try{const data=await fetchRows();if(current!==view||view.query!==query||!$('#history-root'))return;view.rows=data;view.more=data.length===100;paint(true);showError();}catch(e){showError(e);}},350);};
  $('#history-more').onclick=async()=>{
    if(view.busy||!view.rows.length)return;const current=view,query=view.query,box=$('#messages'),height=box.scrollHeight,top=box.scrollTop;view.busy=true;
    try{const older=await fetchRows(view.rows[0]);if(view!==current||view.query!==query||!$('#history-root'))return;view.rows=[...older,...view.rows];view.more=older.length===100;paint(false);box.scrollTop=top+box.scrollHeight-height;showError();}catch(e){showError(e);}finally{current.busy=false;}
  };
  $('#history-new').onclick=async()=>{const current=view;try{view.anchor=null;view.rows=await fetchRows();if(view!==current||!$('#history-root'))return;view.more=view.rows.length===100;paint(true);showError();}catch(e){showError(e);}};
  const box=$('#messages');box.scrollTop=box.scrollHeight;
  if(view.anchor){const target=[...document.querySelectorAll('[data-message-id]')].find(el=>el.dataset.messageId===view.anchor);target?.scrollIntoView({block:'center'});if(target)target.classList.add('source-highlight');$('#history-new').hidden=false;}
}
export async function refreshConversations(){
  if(!$('#history-root')||!view||view.busy)return;
  const current=view,items=await api('conversations');if(current!==view||!$('#history-root'))return;
  view.items=items;const list=$('#conversation-items'),top=list.scrollTop;list.innerHTML=listHtml();list.scrollTop=top;
  const latest=items.find(c=>c.scope===view.scope)?.latest_id||'';
  if(!view.scope)return;
  const changed=latest!==view.latest;
  view.latest=latest;
  const box=$('#messages'),bottom=!view.anchor&&box.scrollHeight-box.scrollTop-box.clientHeight<100;
  if(!bottom&&changed)$('#history-new').hidden=false;
  const rows=await fetchRows();if(current!==view||!$('#history-root'))return;
  const replacements=new Map(rows.map(m=>[m.id,m])),ids=new Set(view.rows.map(m=>m.id));
  const boxTop=box.scrollTop,previousRows=JSON.stringify(view.rows);view.rows=view.rows.map(m=>replacements.get(m.id)||m);
  if(bottom)view.rows.push(...rows.filter(m=>!ids.has(m.id)));
  if(JSON.stringify(view.rows)===previousRows){await refreshProcessing();return;}
  paint(bottom);if(!bottom)box.scrollTop=boxTop;
}
