export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const number = value => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:1});
export const bytes = value => value == null ? '—' : value >= 1073741824 ? (value/1073741824).toFixed(1)+' GB' : value >= 1048576 ? (value/1048576).toFixed(1)+' MB' : number(value)+' B';
export const time = value => value ? new Date(value*1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—';
export const relative = value => !value ? '—' : Date.now()/1000-value < 60 ? '刚刚' : Date.now()/1000-value < 3600 ? Math.floor((Date.now()/1000-value)/60)+' 分钟前' : time(value);
const paths = {
  overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  chat:'<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H6l-4 2 1.7-5A8.5 8.5 0 1 1 21 11.5Z"/><path d="M7 9h10M7 13h7"/>',
  tasks:'<rect x="5" y="4" width="15" height="17" rx="2"/><path d="M9 4V2h7v2M9 10l1 1 2-2M14 10h3M9 16l1 1 2-2M14 16h3"/>',
  files:'<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  memory:'<path d="M9 18H6a4 4 0 0 1-2-7 4 4 0 0 1 5-6 3 3 0 0 1 6 0 4 4 0 0 1 5 6 4 4 0 0 1-2 7h-3M9 5v16m6-16v16M6 11h3m6 3h4"/>',
  persona:'<path d="M12 3 15 9l6 3-6 3-3 6-3-6-6-3 6-3Z"/>',
  models:'<path d="m12 2 10 6-10 6L2 8Zm-9 11 9 5 9-5M3 18l9 5 9-5"/>',
  usage:'<path d="M4 20h17M7 16v-5M12 16V4M17 16V8"/>',
  plugins:'<path d="M8 3h4v4a3 3 0 1 1 4 0v4h5v4h-4a3 3 0 1 1 0 4h-5v-5H8a3 3 0 1 1 0-4H3V6h5Z"/>',
  settings:'<path d="m9 3 1-2h4l1 2 3 2 2-.2 2 3-1 2v4l1 2-2 3-2-.2-3 2-1 2h-4l-1-2-3-2-2 .2-2-3 1-2v-4L2 8l2-3 2 .2Z"/><circle cx="12" cy="12" r="3"/>',
  activity:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',
  panel:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5M5 7a8 8 0 0 1 13-2l2 2M4 17l2 2a8 8 0 0 0 13-2"/>',
  close:'<path d="m6 6 12 12M18 6 6 18"/>',
  plus:'<path d="M12 4v16M4 12h16"/>',
  arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  search:'<circle cx="10" cy="10" r="7"/><path d="m16 16 5 5"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="2"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>',
  bell:'<path d="M18 8a6 6 0 0 0-12 0v7l-2 3h16l-2-3ZM10 21h4"/>',
};
export const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]||paths.files}</svg>`;
export function hydrateIcons(root=document){$$('[data-icon]',root).forEach(el=>el.innerHTML=icon(el.dataset.icon));}
const mutations=new Map();
export const mutationsPending=()=>mutations.size>0;
export async function api(path, body, method){
  const verb=method||(body===undefined?'GET':'POST'),encoded=body===undefined?undefined:JSON.stringify(body);
  const mutation=!['GET','HEAD'].includes(verb),key=mutation?verb+' '+path+' '+encoded:null;
  if(key&&mutations.has(key))return mutations.get(key);
  const work=(async()=>{
    const controller=new AbortController();
    const seconds=path.startsWith('services/')?200:path.startsWith('extensions/')?240:path.endsWith('/resend')?150:path==='models/catalog'?110:path==='qq/qrcode'?45:path==='plan/login'?65:30;
    const timer=setTimeout(()=>controller.abort(),seconds*1000);
    try{
      const response=await fetch('/api/'+path,{method:verb,headers:body===undefined?{}:{'Content-Type':'application/json'},body:encoded,signal:controller.signal});
      const data=await response.json();
      if(!response.ok){const failure=new Error(data.error||`操作失败 (${response.status})`);failure.unconfirmed=mutation&&response.status>=500;throw failure;}
      return data;
    }catch(error){
      if(error.name==='AbortError'||error instanceof TypeError||error instanceof SyntaxError){
        const failure=new Error(mutation?'连接中断或等待超时，操作结果尚未确认。请先查看任务或活动记录，避免重复提交。':'数据读取超时或连接已中断，请重试或查看引擎状态。');
        failure.unconfirmed=mutation;throw failure;
      }
      throw error;
    }finally{clearTimeout(timer);}
  })();
  if(key)mutations.set(key,work);
  try{return await work;}finally{if(key&&mutations.get(key)===work)mutations.delete(key);}
}
export function toast(text,error=false){const el=$('#toast');el.textContent=text;el.classList.remove('hidden');el.style.borderColor=error?'var(--red)':'var(--line)';clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.add('hidden'),error?7000:3500);}
export function modal(title,html){$('#modal-title').textContent=title;$('#modal-body').innerHTML=html;$('#modal').showModal();hydrateIcons($('#modal'));}
export const empty=(text='暂时没有记录',type='files')=>`<div class="empty">${icon(type)}<span>${esc(text)}</span></div>`;
const statuses={partial:['部分完成','warning'],queued:['排队中','info'],running:['执行中','info'],completed:['已完成','success'],failed:['失败','error'],cancelled:['已取消','warning'],interrupted:['已中断','warning'],waiting_approval:['等待确认','warning'],scheduled:['等待提醒','info'],success:['成功','success'],ready:['可用','success'],expired:['已清理',''],integrated:['完整接入','success'],basic:['基础兼容','warning'],needs_adaptation:['需要适配','error'],skill:['技能说明','info']};
export function badge(value){const [label,color]=statuses[value]||[value||'未知',''];return `<span class="badge ${color}">${esc(label)}</span>`;}
export function heading(title,description,actions=''){return `<div class="page-heading"><div><h1>${esc(title)}</h1><p>${esc(description)}</p></div><div class="actions">${actions}</div></div>`;}
export function button(text,attributes='',style=''){return `<button class="button ${style}" ${attributes}>${text}</button>`;}
export function table(columns,rows){return `<div class="table-wrap"><table><thead><tr>${columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;}
export function details(value){return `<pre>${esc(typeof value==='string'?value:JSON.stringify(value,null,2))}</pre>`;}
export function parse(value,fallback={}){try{return typeof value==='string'?JSON.parse(value):value||fallback;}catch{return fallback;}}
export function field(label,name,value,type='text',hint=''){
  let control;
  if(type==='checkbox')control=`<label class="switch"><input name="${esc(name)}" type="checkbox" ${value?'checked':''}></label>`;
  else if(type==='textarea')control=`<textarea name="${esc(name)}">${esc(value)}</textarea>`;
  else if(Array.isArray(type))control=`<select name="${esc(name)}">${type.map(o=>{const pair=Array.isArray(o)?o:[o,o];return `<option value="${esc(pair[0])}" ${String(value)===String(pair[0])?'selected':''}>${esc(pair[1])}</option>`;}).join('')}</select>`;
  else control=`<input name="${esc(name)}" type="${type}" value="${type==='password'?'':esc(value)}" ${type==='number'?'step="any"':''}>`;
  return type==='checkbox'?`<div class="field-row"><div><strong>${esc(label)}</strong>${hint?`<p>${esc(hint)}</p>`:''}</div>${control}</div>`:`<label>${esc(label)}${control}${hint?`<span class="form-description">${esc(hint)}</span>`:''}</label>`;
}
export function chart(series,height=140){
  const all=series.flatMap(s=>s.values).filter(Number.isFinite);
  if(!all.length)return empty('有实际数据后显示曲线','activity');
  const max=Math.max(...all,1),width=600,pad=10;
  const times=series.flatMap(s=>s.times||[]).filter(Number.isFinite),begin=times.length?Math.min(...times):null,end=times.length?Math.max(...times):null;
  const lines=[.25,.5,.75,1].map(f=>`<line class="chart-grid" x1="0" x2="600" y1="${height-f*(height-20)}" y2="${height-f*(height-20)}"/>`).join('');
  return `<svg class="chart ${height>150?'chart-large':''}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${lines}${series.map(s=>{
    const valid=s.values.map((v,i)=>[i,v]).filter(([,v])=>Number.isFinite(v));
    const point=([i,v])=>`${pad+(s.times&&begin!=null?(s.times[i]-begin)/Math.max(end-begin,1):i/Math.max(s.values.length-1,1))*(width-pad*2)},${height-pad-v/max*(height-pad*2)}`;
    let segments=[],segment=[];s.values.forEach((v,i)=>{if(Number.isFinite(v))segment.push([i,v]);else if(segment.length){segments.push(segment);segment=[];}});if(segment.length)segments.push(segment);
    return segments.map(points=>`<polyline fill="none" stroke="${s.color}" stroke-width="2" points="${points.map(point).join(' ')}"/>`).join('')+valid.map(p=>`<circle cx="${point(p).split(',')[0]}" cy="${point(p).split(',')[1]}" r="${valid.length===1?3:1.5}" fill="${s.color}"><title>${esc(s.name)}：${number(p[1])}${s.times?' · '+time(s.times[p[0]]):''}</title></circle>`).join('');
  }).join('')}</svg>${times.length?`<div class="metric-row muted"><small>${time(begin)}</small><small>${time(end)}</small></div>`:''}<div class="legend">${series.map(s=>`<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join('')}</div>`;
}
