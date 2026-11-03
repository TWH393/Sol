import {$,$$,esc,time,api,modal,heading,button,table,empty,parse,field} from './ui.js';

const labels={partial:'部分完成',running:'进行中',queued:'排队中',completed:'已完成',success:'成功',failed:'失败',cancelled:'已中断',interrupted:'已中断',skipped:'未使用 / 已跳过',waiting_approval:'等待确认',unknown:'送达未确认',sent:'已确认送达',open:'未解决',recovered:'已恢复',ignored:'已忽略'};
const state=s=>`<span class="badge ${s==='open'||s==='failed'?'error':s==='unknown'?'warning':s==='success'||s==='recovered'||s==='sent'?'success':''}">${esc(labels[s]||s)}</span>`;
const duration=(start,end)=>Math.max(0,(end||Date.now()/1000)-start).toFixed(2)+' 秒';
let action,openConversation;
const expanded=new Set(),panels=new Map();

export function processingHtml(processing){
  if(!processing)return '';
  return `<details class="request-processing" data-request-detail="${esc(processing.id)}" ${expanded.has(processing.id)?'open':''}><summary>处理详情 · ${esc(processing.stage)}</summary><div class="request-content">${panels.get(processing.id)||'<p class="muted">展开后加载实际处理记录</p>'}</div></details>`;
}
function requestHtml(data){
  const r=data.request,spans=data.spans,now=Date.now()/1000;
  const end=r.status==='running'?now:Math.max(r.finished||r.created,...spans.map(s=>s.finished||r.finished||r.created));
  const stages=[['batch','连续补充等待'],['local','本地整理'],['prepare','上下文 / 附件准备'],['memory','记忆检索'],['model_prepare','模型接入准备'],['model_queue','模型排队'],['model','模型调用'],['resource','工具资源等待'],['tool','工具执行'],['send_queue','发送排队'],['delivery','消息发送']];
  const notUsed=stages.filter(([key])=>!spans.some(s=>s.stage===key)).map(([,name])=>name);
  return `<div class="actions">${state(r.status)}<span class="muted">${esc(r.id)}</span>${r.task_id?button('关联任务',`data-evidence-task="${esc(r.task_id)}"`,'small'):''}</div>
    <p>本轮总耗时 ${duration(r.created,end)}</p><p class="preview-note">阶段可能并行，不能把耗时直接相加。这里只记录实际执行；未使用的步骤不计为成功。</p>
    ${r.reason?`<div class="diagnostic error-text">${esc(r.reason)}</div>`:''}
    ${table(['阶段','结果','耗时'],spans.map(s=>`<tr><td>${esc(s.name)}<span class="secondary">${time(s.started)}</span>${Object.keys(parse(s.detail)).length?`<details data-evidence-fold="${esc(s.id)}"><summary>步骤信息</summary><pre>${esc(JSON.stringify(parse(s.detail),null,2))}</pre></details>`:''}</td><td>${state(s.status)}${s.error?`<span class="secondary error-text">${esc(s.error)}</span>`:''}${s.fault_id?button('错误记录',`data-fault="${esc(s.fault_id)}"`,'small ghost'):''}</td><td>${duration(s.started,s.finished||end)}</td></tr>`))}
    ${notUsed.length?`<p class="preview-note">${r.status==='running'?'尚无执行记录':'本轮未使用'}：${esc(notUsed.join('、'))}</p>`:''}
    <h3 class="small-space">本轮记忆</h3><p class="preview-note">记录当时的快照。“已提供”表示请求收到响应，不代表能证明模型采纳。编辑或删除只影响后续检索，历史记录保留。</p>
    ${data.memories.map(m=>{const snap=parse(m.snapshot),expired=snap.valid_until&&snap.valid_until<now;return `<article class="diagnostic"><div class="actions"><span class="badge">${esc(({candidate:'检索候选',selected:'已选入上下文',submitting:'请求提交中',supplied:'已随请求提供',unconfirmed:'请求未确认'})[m.state]||m.state)}</span><span>${esc(snap.kind)} · ${esc(snap.visibility)}</span>${expired?'<span class="badge warning">已过有效期</span>':''}${!m.current?'<span class="badge">原记忆已删除</span>':''}</div><p>${esc(snap.text)}</p><p class="muted">提供者 ${esc(snap.owner)} · 来源 ${esc(snap.scope)} · ${time(snap.updated)}</p>${snap.supersedes?`<p class="muted">更正自 ${esc(snap.supersedes)}</p>`:''}${m.current&&m.current.text!==snap.text?`<details><summary>当前记忆已更正</summary><p>${esc(m.current.text)}</p></details>`:''}<div class="actions">${m.current?button('编辑记忆',`data-evidence-memory="${esc(m.memory_id)}"`,'small')+button('删除记忆',`data-evidence-delete-memory="${esc(m.memory_id)}"`,'small ghost'):''}</div>${m.sources.map(s=>`<details><summary>${esc(s.name)} · ${time(s.created)}</summary><p>${esc(s.text)}</p>${button('定位原消息',`data-source-scope="${esc(s.scope)}" data-source-message="${esc(s.id)}"`,'small')}</details>`).join('')}${!m.sources.length?`<p class="muted">来源编号：${esc(snap.evidence||'没有可定位的原消息')}</p>`:''}</article>`;}).join('')||'<p class="muted">本轮没有长期记忆检索记录</p>'}
    ${data.files?.length?`<h3 class="small-space">已保留文件</h3>${data.files.map(f=>`<div class="field-row"><span>${esc(f.name)} · ${esc(f.status)}</span>${f.kind==='output'?button('版本与来源',`data-artifact-file="${esc(f.id)}"`,'small'):''}${f.status==='ready'?`<a class="button small" href="/api/files/${encodeURIComponent(f.id)}/download">下载</a>`:''}</div>`).join('')}`:''}
    ${data.recovery?recoveryHtml(r.task_id,data.recovery):''}
    ${data.attempts?.length>1?`<h3 class="small-space">同任务历次处理</h3>${data.attempts.map(a=>`<div class="field-row"><span>${time(a.created)} · ${esc(labels[a.status]||a.status)}</span>${button('查看此轮',`data-request-open="${esc(a.id)}"`,'small')}</div>`).join('')}`:''}`;
}
export function recoveryHtml(id,options){
  if(!options?.can_continue&&!options?.can_resend)return '';
  return `<section class="diagnostic"><h3>原任务补救</h3><p>${esc(options.note)}</p><div class="actions">${options.can_continue?button('从失败处继续',`data-recover-task="${esc(id)}" data-recover-mode="continue"`,'small primary'):''}${options.can_resend?button('仅重发已有文件',`data-recover-task="${esc(id)}" data-recover-mode="resend" data-unknown="${options.deliveries.some(d=>d.status==='unknown')}"`,'small primary'):''}</div></section>`;
}
export async function loadProcessing(element,id){
  try{const opened=new Set($$('details[open]',element).map(el=>el.dataset.evidenceFold||$('summary',el)?.textContent));const data=await api('requests/'+encodeURIComponent(id));const html=requestHtml(data);panels.set(id,html);if(element.isConnected){element.innerHTML=html;$$('details',element).forEach(el=>{if(opened.has(el.dataset.evidenceFold||$('summary',el)?.textContent))el.open=true;});}}
  catch(e){if(element.isConnected)element.innerHTML=`<p class="error-text">${esc(e.message)}</p>`;}
}
export async function refreshProcessing(){
  for(const el of $$('details[data-request-detail][open]'))await loadProcessing($('.request-content',el),el.dataset.requestDetail);
}
export async function requestModal(id){
  const data=await api('requests/'+encodeURIComponent(id));modal('处理详情',button('刷新详情',`data-request-open="${esc(id)}"`,'small')+requestHtml(data));
}
export async function faultsPage(){
  const data=await api('faults');
  return heading('报错日志','未解决的问题优先显示；自动补救成功也保留。不弹窗，不发桌面通知。')+
    `<div class="actions"><span class="badge error">未解决 ${data.counts.open||0}</span><span class="badge success">已恢复 ${data.counts.recovered||0}</span><span class="badge">已忽略 ${data.counts.ignored||0}</span></div>
    <div class="filters small-space"><input id="filter-text" placeholder="搜索原因、阶段或错误编号"><select id="filter-status"><option value="">全部状态</option><option value="open">未解决</option><option value="recovered">已恢复</option><option value="ignored">已忽略</option></select></div><section class="panel">${data.rows.length?table(['问题 / 原因','状态 / 次数','首次 / 最近'],data.rows.map(f=>`<tr class="clickable" data-fault="${esc(f.id)}" data-filter="${esc(f.id+' '+f.title+' '+f.reason+' '+f.stage)}" data-status="${esc(f.status)}"><td>${esc(f.title)}<span class="secondary error-text">${esc(f.reason)}</span><span class="secondary">${esc(f.stage)} · ${esc(f.id)}</span></td><td>${state(f.status)}<span class="secondary">共 ${f.count} 次</span></td><td>${time(f.first_seen)}<span class="secondary">${time(f.last_seen)}</span></td></tr>`)):empty('暂无报错记录','activity')}</section>`;
}
async function faultModal(id){
  const {fault:f,occurrences}=await api('faults/'+encodeURIComponent(id));
  modal(f.title,`<div class="actions">${state(f.status)}<span>共 ${f.count} 次</span>${button(f.status==='ignored'?'重新关注':'忽略',`data-fault-action="${f.status==='ignored'?'reopen':'ignore'}" data-fault-id="${esc(id)}"`,'small')}</div><div class="diagnostic error-text">${esc(f.reason)}</div><p>${esc(f.suggestion)}</p><p class="preview-note">忽略只表示暂不处理，不代表修复。已恢复表示后续补救步骤完成，仍可查看原始失败记录。</p>${occurrences.map(o=>`<article class="diagnostic"><div class="actions">${state(o.status)}<span>${time(o.created)}</span>${o.request_id?button('处理详情',`data-request-open="${esc(o.request_id)}"`,'small'):''}${o.task_id?button('关联任务',`data-evidence-task="${esc(o.task_id)}"`,'small'):''}</div>${o.scope?`<p class="muted">来源 ${esc(o.scope)}</p>`:''}<details><summary>技术详情</summary><pre>${esc(parse(o.detail).technical||'没有额外技术信息')}</pre></details></article>`).join('')}`);
}
async function artifactModal(fileId){
  const data=await api('artifacts/'+encodeURIComponent(fileId));
  if(data.legacy){modal('现有成果 · '+data.file.name,`<p>这是更新前生成的文件。引用它继续修改时，会保留为初始版本，再生成新版。</p><div class="actions"><a class="button small" href="/api/files/${encodeURIComponent(data.file.id)}/download">取回原文件</a>${data.file.task_id?button('制作任务',`data-evidence-task="${esc(data.file.task_id)}"`,'small'):''}</div><h3 class="small-space">现有源文件与素材</h3>${data.sources.map(f=>`<div class="field-row"><span>${esc(f.name)} · ${esc(f.kind)}</span>${f.status==='ready'?`<a class="button small" href="/api/files/${encodeURIComponent(f.id)}/download">下载</a>`:'<span>原件不可用</span>'}</div>`).join('')||'<p class="muted">没有保留下来的单独源文件</p>'}<details><summary>原制作信息</summary><pre>${esc(JSON.stringify(data.recipe,null,2))}</pre></details>`);return;}
  const {artifact,versions,selected_file}=data;
  modal('成果版本 · '+artifact.title,`<p>默认继续修改最新成果；引用旧版消息时从旧版分支。各版独立保留，重复内容和素材共用存储。</p>${versions.map(v=>`<article class="diagnostic"><div class="actions"><strong>第 ${v.number} 版 · ${esc(v.name)}</strong>${v.file_id===artifact.latest_file?'<span class="badge success">最新版</span>':''}${v.file_id===selected_file?'<span class="badge">当前查看</span>':''}</div><p>${esc(v.summary)}</p><p class="muted">${time(v.created)}${v.parent_file?' · 基于第 '+esc(versions.find(p=>p.file_id===v.parent_file)?.number||v.parent_file)+' 版':' · 初始版本'}</p><div class="actions">${v.file_status==='ready'?`<a class="button small" href="/api/files/${encodeURIComponent(v.file_id)}/download">取回此版本</a>`:''}${button('制作任务',`data-evidence-task="${esc(v.task_id)}"`,'small')}</div><details><summary>源文件与素材（${v.source_files.length}）</summary>${v.source_files.map(s=>`<div class="field-row"><span>${esc(s.name)} · ${esc(s.kind)}</span>${s.status==='ready'?`<a class="button small" href="/api/files/${encodeURIComponent(s.id)}/download">下载</a>`:'<span>原件不可用</span>'}</div>`).join('')||'<p class="muted">没有单独的源文件；制作参数见下方配方</p>'}</details><details><summary>制作配方与参数</summary><pre>${esc(JSON.stringify(parse(v.recipe),null,2))}</pre></details></article>`).join('')}`);
}
async function memoryModal(id){
  const row=await api('memories/'+encodeURIComponent(id));
  if(!row)throw new Error('原记忆已删除');
  modal('更正长期记忆',`<form id="evidence-memory-form">${field('记忆正文','text',row.text,'textarea')}<p class="preview-note">修改后续检索内容，过去请求里的快照保留。</p>${button('保存更正','type="submit"','primary')}</form>`);
  $('#evidence-memory-form').onsubmit=e=>{e.preventDefault();const text=e.currentTarget.elements.text.value;action(async()=>{await api('memories/'+id,{text});$('#modal').close();},'记忆已更正',false);};
}
export function installEvidenceUI(handler,conversation,taskModal){
  action=handler;openConversation=conversation;
  document.addEventListener('toggle',e=>{
    const el=e.target;if(!el.matches?.('details[data-request-detail]'))return;
    if(el.open){expanded.add(el.dataset.requestDetail);loadProcessing($('.request-content',el),el.dataset.requestDetail);}else expanded.delete(el.dataset.requestDetail);
  },true);
  document.addEventListener('click',e=>{
    const el=e.target.closest('[data-request-open],[data-fault],[data-fault-action],[data-artifact-file],[data-evidence-task],[data-source-scope],[data-evidence-memory],[data-evidence-delete-memory],[data-recover-task]');
    if(!el)return;
    if(el.dataset.sourceScope){$('#modal').close();openConversation({scope:el.dataset.sourceScope,message:el.dataset.sourceMessage});return;}
    if(el.dataset.evidenceDeleteMemory){if(confirm('删除这条长期记忆？原消息和历史使用快照保留，以后仍可能重新学会。'))action(()=>api('memories/'+el.dataset.evidenceDeleteMemory,{}),'记忆已删除',false);return;}
    if(el.dataset.recoverTask){
      const mode=el.dataset.recoverMode,unknown=el.dataset.unknown==='true';
      if(mode==='continue'&&!confirm('在原任务继续处理？已完成步骤会复用，后续模型调用会产生用量。'))return;
      if(unknown&&!confirm('部分文件是否送达尚未确认。请先核对QQ；确定仍要重新发送这些文件？'))return;
      action(async()=>{await api('tasks/'+el.dataset.recoverTask+'/recover',{mode,confirm_unknown:unknown});$('#modal').close();},mode==='resend'?'已有文件已进入重发队列':'原任务已继续');return;
    }
    if(el.dataset.faultAction){action(async()=>{await api('faults/'+el.dataset.faultId,{action:el.dataset.faultAction});await faultModal(el.dataset.faultId);},'记录状态已更新',false);return;}
    action(()=>el.dataset.requestOpen?requestModal(el.dataset.requestOpen):el.dataset.fault?faultModal(el.dataset.fault):el.dataset.artifactFile?artifactModal(el.dataset.artifactFile):el.dataset.evidenceTask?taskModal(el.dataset.evidenceTask):memoryModal(el.dataset.evidenceMemory),'已打开',false);
  });
}
