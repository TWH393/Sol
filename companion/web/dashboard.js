import {$,$$,esc,number,bytes,time,relative,icon,api,toast,modal,empty,badge,heading,button,table,parse,chart} from './ui.js';

const definitions=[['services','连接与服务',12],['tasks','正在执行',8],['resources','本地资源',4],['usage','模型调用趋势',8],['persona','人格成长',4],['activity','近期活动',6],['storage','记忆与文件',6]];
export function dashboard(state,layout,samples,gpu){
  const totalIn=state.usage.reduce((sum,r)=>sum+(r.input||0),0),totalOut=state.usage.reduce((sum,r)=>sum+(r.output||0),0),calls=state.usage.reduce((sum,r)=>sum+r.calls,0);
  const counts=state.task_counts;
  const widgets=layout?.length?layout:definitions.map(([id,title,span])=>({id,span,visible:true}));
  return heading('运行总览','所有连接、任务和变化，都在这里。',button(icon('settings')+'自定义布局','id="customize-layout"')+button(icon('refresh')+'刷新','id="dashboard-refresh"'))+
    (!state.account.qq||!state.model?`<div class="setup-callout"><p>欢迎使用同伴。配置机器人账号、管理员和主模型后，即可开始连接。</p>${button('开始设置 '+icon('arrow'),'data-route="settings"','primary')}</div>`:'')+
    `<div class="summary-grid">${[
      ['机器人状态',state.online?'在线':'未连接',state.online?'接收消息与执行任务中':'等待账号配置或 QQ 连接'],
      ['进行中的任务',number((counts.running||0)+(counts.queued||0)),`${number(counts.running||0)} 执行 · ${number(counts.queued||0)} 等待`],
      ['近 24 小时 Token',state.usage.some(r=>r.input!=null||r.output!=null)?number(totalIn+totalOut):'—',`${number(calls)} 次调用 · 已知输入输出合计${state.usage.some(r=>r.unknown)?' · 存在未知用量':''}`],
      ['公共用量',number(state.public_usage.tokens),`${number(state.public_usage.calls)} 次 · 自主任务独立记账`],
    ].map(([label,value,caption])=>`<div class="summary"><div class="summary-label">${label}</div><div class="summary-value">${value}</div><div class="summary-caption">${caption}</div></div>`).join('')}</div>`+
    `<div class="dashboard" id="dashboard-grid">${widgets.filter(w=>w.visible!==false).map(w=>{
      const definition=definitions.find(d=>d[0]===w.id);if(!definition)return '';
      return `<section class="widget" draggable="true" data-widget="${w.id}" style="--span:${w.span||definition[2]}"><div class="widget-header"><h3>${definition[1]}</h3><div class="widget-menu"><button data-resize-widget="${w.id}" title="切换宽度">↔</button><button data-hide-widget="${w.id}" title="隐藏">×</button></div></div>${content(w.id,state,samples,gpu)}</section>`;
    }).join('')}</div><p class="widget-hint small-space">拖动模块标题可调整顺序 · ↔ 调整宽度 · 隐藏模块不会关闭功能</p>`;
}
function content(id,s,samples,gpu){
  if(id==='services')return `<div class="service-grid">${[
    ['QQ 接入',s.online,s.account.qq||'尚未设置账号'],['AstrBot 核心',s.core.running,s.core.running?'内部引擎运行中':s.core.error||'准备中'],['对话主模型',!!s.model,s.model||'尚未选择'],['本地辅助',s.local.ready,s.local.error||`${number(s.local.calls)} 次本地整理${s.local.ready?'':' · 尚未验证就绪'}`],['图片与代码','neutral','按任务加载，结束后释放资源'],['管理后台',true,'桌面端与浏览器共用 · 仅本机'],
  ].map(([name,ok,desc])=>`<div class="service-cell"><div class="service-name"><i class="dot ${ok===true?'online':''}"></i>${name}</div><div class="service-description" title="${esc(desc)}">${esc(desc)}</div></div>`).join('')}</div>`;
  if(id==='tasks')return `<div id="overview-tasks">${empty('暂无正在执行的任务','tasks')}</div>`;
  if(id==='resources')return `<div class="metric"><div class="metric-row"><span>CPU</span><b>${number(s.resources.cpu)}%</b></div><div class="progress"><span style="width:${Math.min(s.resources.cpu,100)}%"></span></div></div><div class="metric"><div class="metric-row"><span>内存</span><b>${bytes(s.resources.memory_used)} / ${bytes(s.resources.memory_total)}</b></div><div class="progress"><span style="width:${s.resources.memory_used/s.resources.memory_total*100}%"></span></div></div><div class="metric"><div class="metric-row"><span>显存</span><b>${gpu?.available?`${number(gpu.used_mb)} / ${number(gpu.total_mb)} MB`:'未取得数据'}</b></div><div class="progress"><span style="width:${gpu?.available?gpu.used_mb/gpu.total_mb*100:0}%"></span></div></div>${chart([{name:'CPU 实时占用',color:'#aaaac9',values:samples.map(r=>r.cpu)}],80)}`;
  if(id==='usage')return `<div id="overview-usage-chart">${empty('模型调用后显示实际用量趋势','usage')}</div><div class="preview-note">实际调用记账；缓存属于输入子集，思考属于输出子集，不重复相加。</div>`;
  if(id==='persona')return `<div style="font-size:12px;line-height:1.8;color:var(--muted)">${esc(s.persona?.reason||'尚未发生人格变化')}</div><div id="overview-persona-chart"></div><p class="preview-note">仅核心启动时复盘 · 无变化不生成总结</p>${button('查看成长记录','data-route="persona"','small ghost')}`;
  if(id==='activity')return s.events.length?`<div class="event-list">${s.events.slice(0,8).map(e=>`<div class="event-item"><i class="dot ${e.level==='error'?'':''}"></i><span class="event-text">${esc(e.title)}</span><span class="event-time">${relative(e.created)}</span></div>`).join('')}</div>`:empty('运行后记录实际活动','activity');
  if(id==='storage')return `<div class="summary-grid" style="grid-template-columns:1fr 1fr;margin-bottom:12px"><div><div class="summary-label">有效记忆</div><div class="summary-value">${number(s.memory_count)}</div></div><div><div class="summary-label">可用文件</div><div class="summary-value">${number(s.file_count.n)}</div></div></div><div class="metric-row"><span>逻辑文件总量</span><b>${bytes(s.file_count.bytes||0)}</b></div><div class="metric-row"><span>磁盘可用</span><b>${bytes(s.resources.disk_free)}</b></div><div class="actions small-space">${button('管理记忆','data-route="memory"','small')}${button('打开文件库','data-route="files"','small')}</div>`;
  return '';
}
export async function populateDashboard(){
  const [tasks,usage,personas]=await Promise.all([api('tasks'),api('usage'),api('persona')]);
  const live=tasks.filter(t=>['running','queued','waiting_approval','scheduled'].includes(t.status));
  if($('#overview-tasks'))$('#overview-tasks').innerHTML=live.length?table(['任务','阶段','状态'],live.slice(0,6).map(t=>`<tr class="clickable" data-task="${t.id}"><td><span class="ellipsis">${esc(t.title)}</span><span class="secondary">${esc(t.owner)} · ${relative(t.created)}</span></td><td>${esc(t.stage)}</td><td>${badge(t.status)}</td></tr>`)):empty('没有进行中的任务','tasks');
  const buckets=Array.from({length:12},(_,i)=>Date.now()/1000-(11-i)*7200);
  const input=buckets.map(t=>{const rows=usage.calls.filter(r=>r.created>=t-7200&&r.created<t);return rows.some(r=>r.input==null)?null:rows.reduce((s,r)=>s+r.input,0);});
  const output=buckets.map(t=>{const rows=usage.calls.filter(r=>r.created>=t-7200&&r.created<t);return rows.some(r=>r.output==null)?null:rows.reduce((s,r)=>s+r.output,0);});
  if($('#overview-usage-chart'))$('#overview-usage-chart').innerHTML=usage.calls.length?chart([{name:'输入 Token',color:'#a9a9ce',values:input},{name:'输出 Token',color:'#73b49c',values:output}],155):empty('还没有模型调用记录','usage');
  if($('#overview-persona-chart'))$('#overview-persona-chart').innerHTML=chart([{name:'直率',color:'#c3a78b',values:personas.map(p=>parse(p.dimensions).directness),times:personas.map(p=>p.created)},{name:'幽默',color:'#aaaace',values:personas.map(p=>parse(p.dimensions).humor),times:personas.map(p=>p.created)}],95);
}
export function wireDashboard(config,saveLayout){
  let widgets=config.layout.widgets?.length?structuredClone(config.layout.widgets):definitions.map(([id,title,span])=>({id,span,visible:true}));
  let dragging;
  $$('.widget').forEach(el=>{
    el.addEventListener('dragstart',e=>{if(e.target.closest('button')){e.preventDefault();return;}dragging=el.dataset.widget;el.classList.add('dragging');e.dataTransfer.effectAllowed='move';});
    el.addEventListener('dragend',()=>el.classList.remove('dragging'));
    el.addEventListener('dragover',e=>{e.preventDefault();el.classList.add('drag-over');});
    el.addEventListener('dragleave',()=>el.classList.remove('drag-over'));
    el.addEventListener('drop',async e=>{e.preventDefault();el.classList.remove('drag-over');const target=el.dataset.widget;if(!dragging||dragging===target)return;const from=widgets.findIndex(w=>w.id===dragging),to=widgets.findIndex(w=>w.id===target);widgets.splice(to,0,widgets.splice(from,1)[0]);await saveLayout(widgets);});
  });
  $$('[data-resize-widget]').forEach(b=>b.onclick=async()=>{const w=widgets.find(w=>w.id===b.dataset.resizeWidget);w.span=({4:6,6:8,8:12,12:4})[w.span]||6;await saveLayout(widgets);});
  $$('[data-hide-widget]').forEach(b=>b.onclick=async()=>{widgets.find(w=>w.id===b.dataset.hideWidget).visible=false;await saveLayout(widgets);});
  $('#customize-layout').onclick=()=>{
    modal('首页模块',`<p class="muted">调整显示内容，设置在桌面端和浏览器端共用。</p>${definitions.map(([id,title])=>`<label class="field-row"><strong>${title}</strong><input type="checkbox" data-visible-widget="${id}" ${widgets.find(w=>w.id===id)?.visible!==false?'checked':''}></label>`).join('')}<div class="actions small-space">${button('保存布局','id="save-layout"','primary')}${button('恢复默认','id="reset-layout"')}</div>`);
    $('#save-layout').onclick=async()=>{$$('[data-visible-widget]').forEach(b=>widgets.find(w=>w.id===b.dataset.visibleWidget).visible=b.checked);await saveLayout(widgets);$('#modal').close();};
    $('#reset-layout').onclick=async()=>{await saveLayout([]);$('#modal').close();};
  };
}
