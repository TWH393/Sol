import {$,$$,esc,time,api,modal,heading,button,table,empty,field} from './ui.js';

const labels={active:'等待执行',paused:'已暂停',needs_review:'送达状态待核对',completed:'已完成',cancelled:'已取消',skipped:'已跳过',sent:'已送达',sending:'发送中',unknown:'送达未确认'};
const status=value=>`<span class="badge ${value==='unknown'||value==='needs_review'?'warning':''}">${esc(labels[value]||value)}</span>`;
const when=value=>value?new Date(value*1000).toLocaleString('zh-CN',{hour12:false}):'—';
const cycle=r=>!r.interval?'一次性':`每 ${r.interval%86400===0?r.interval/86400+' 天':r.interval%3600===0?r.interval/3600+' 小时':r.interval/60+' 分钟'} · ${r.remaining?'剩余 '+r.remaining+' 次':['active','paused'].includes(r.status)?'持续重复':'已结束'}`;

export async function schedulesPage(){
  const {rows,policy}=await api('schedules');
  return heading('未来安排','提醒与周期安排会保留到下次启动；Sol运行且QQ在线时才能发送。')+`
    <section class="panel"><h2>错过后的补发策略</h2><form id="schedule-policy">
      ${field('一次性提醒：错过后补发','catch_up_once',policy.catch_up_once,'checkbox','关闭后，错过的一次性提醒直接标为已跳过')}
      ${field('周期提醒：错过后补发','catch_up_recurring',policy.catch_up_recurring,'checkbox','开启后，离线期间错过的多次合并补发一次，再等下一次；关闭后只等下一次')}
      ${button('保存补发设置','type="submit"','primary')}
    </form><p class="preview-note">保存后对尚未处理的错过提醒生效，已送达、已跳过或送达未确认的记录不会重新补发。修改此处不会调用模型。</p></section>
    <div class="filters"><input id="filter-text" placeholder="搜索提醒、创建人或对象"><select id="filter-status"><option value="">全部状态</option>${['active','paused','needs_review','completed','skipped','cancelled'].map(s=>`<option value="${s}">${labels[s]}</option>`).join('')}</select></div>
    <section class="panel">${rows.length?table(['提醒 / 来源','创建人 → 提醒对象','下次时间 / 周期','状态'],rows.map(r=>`<tr class="clickable" data-schedule="${esc(r.id)}" data-filter="${esc(r.text+' '+r.owner+' '+r.target+' '+r.scope+' '+r.id)}" data-status="${esc(r.status)}"><td>${esc(r.text)}<span class="secondary">${esc(r.scope)} · ${esc(r.id)}</span></td><td>${esc(r.owner)} → ${esc(r.target)}</td><td>${esc(when(r.next_due))}<span class="secondary">${esc(cycle(r))}</span></td><td>${status(r.status)}${r.error?`<span class="secondary error-text">${esc(r.error)}</span>`:''}</td></tr>`)):empty('在QQ里说“十分钟后提醒我……”即可创建未来安排','tasks')}</section>`;
}

function localTime(seconds){
  if(!seconds)return '';
  const d=new Date(seconds*1000);return new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,16);
}
async function scheduleModal(id,action){
  const {schedule:r,runs}=await api('schedules/'+id),live=['active','paused'].includes(r.status);
  modal('未来安排',`<div class="actions">${status(r.status)}<span class="muted">${esc(id)}</span></div><p>${esc(r.text)}</p>
    <p class="muted">创建人 ${esc(r.owner)} · 对象 ${esc(r.target)} · ${esc(r.scope)}</p><p>下次：${esc(when(r.next_due))} · ${esc(cycle(r))}</p>
    ${r.error?`<div class="diagnostic error-text">${esc(r.error)}</div>`:''}<div class="actions">
    ${live?button(r.status==='paused'?'恢复':'暂停',`data-arrangement-action="${r.status==='paused'?'resume':'pause'}"`):''}
    ${['active','paused','needs_review'].includes(r.status)?button('取消安排','data-arrangement-action="cancel"','danger'):''}</div>
    ${live?`<details class="small-space"><summary>修改内容与时间</summary><form id="schedule-edit" class="small-space">${field('提醒内容','text',r.text,'textarea')}${field('下次时间（本机时区）','next_due',localTime(r.next_due),'datetime-local')}${field('重复间隔（秒，0为一次性）','interval',r.interval,'number')}${field('剩余次数（周期填0为持续重复）','remaining',r.remaining,'number')}<p class="preview-note">修改不会改变创建人、提醒对象与来源会话；如需改对象，请取消后在QQ重新设置。</p>${button('保存安排','type="submit"','primary')}</form></details>`:''}
    <h3 class="small-space">执行记录</h3>${runs.length?table(['计划时间','处理时间','结果'],runs.map(run=>`<tr><td>${esc(when(run.due))}${run.count>1?`<span class="secondary">含 ${run.count} 个错过时点</span>`:''}</td><td>${time(run.finished||run.started)}</td><td>${status(run.status)}${run.error?`<span class="secondary">${esc(run.error)}</span>`:''}</td></tr>`)):'<p class="muted">还未到执行时间</p>'}
    <p class="preview-note">发送期间中断或QQ回执不明确时，不会自动重复发送。请先核对QQ，再重新创建需要的提醒。</p>`);
  $$('[data-arrangement-action]').forEach(b=>b.onclick=()=>{
    const op=b.dataset.arrangementAction;
    if(op==='cancel'&&!confirm('取消这条未来安排？执行记录保留。'))return;
    action(async()=>{await api('schedules/'+id,{action:op});$('#modal').close();},'安排已更新');
  });
  const edit=$('#schedule-edit');if(edit)edit.onsubmit=e=>{
    e.preventDefault();
    const data=Object.fromEntries(new FormData(edit));
    action(async()=>{await api('schedules/'+id,{action:'edit',changes:{text:data.text,next_due:new Date(data.next_due).getTime()/1000,interval:Number(data.interval),remaining:Number(data.remaining)}});$('#modal').close();},'安排已保存');
  };
}

export function wireSchedules(action){
  const form=$('#schedule-policy');if(form)form.onsubmit=e=>{
    e.preventDefault();action(()=>api('schedules/policy',{catch_up_once:form.elements.catch_up_once.checked,catch_up_recurring:form.elements.catch_up_recurring.checked}),'补发设置已保存');
  };
  $$('[data-schedule]').forEach(el=>el.onclick=()=>action(()=>scheduleModal(el.dataset.schedule,action),'已打开',false));
}
