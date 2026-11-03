import {nativeValues} from './native-ui.js';
import {actionControl,busy,withBusy,regionLoading} from './busy.js';
import {wireConversations,refreshConversations} from './conversations.js';
import {modelEffort,wireModelSliders} from './models-ui.js';
import {$,$$,esc,number,bytes,time,icon,api,toast,modal,empty,heading,button,details,parse,hydrateIcons,field,mutationsPending} from './ui.js';
import {dashboard,populateDashboard,wireDashboard} from './dashboard.js';
import {tasksPage,taskModal,conversationsPage,filesPage,memoryPage,personaPage,modelsPage,usagePage,featuresPage,pluginsPage,activityPage,settingsPage} from './pages.js';
import {qqLoginModal} from './qq-login.js';
import {schedulesPage,wireSchedules} from './schedules-ui.js';
import {faultsPage,installEvidenceUI} from './diagnostics-ui.js';

const nav=[['overview','运行总览','overview'],['conversations','会话','chat'],['tasks','任务中心','tasks'],['files','文件库','files'],['memory','统一记忆','memory'],['persona','人格与成长','persona'],['models','模型与思考','models'],['usage','用量与额度','usage'],['features','功能中心','overview'],['plugins','插件与技能','plugins'],['activity','活动记录','activity']];
let config,state,gpu,route='overview',settingsTab='account',conversation,samples=[];
let rendering=false;
let themeSaving=false;
const loadedRevision=new URL(import.meta.url).pathname.split('/').find(p=>p.startsWith('r-'))?.slice(2);
let formDirty=false;
document.addEventListener('input',e=>{if(e.target.closest('form'))formDirty=true;});

function navigate(target){location.hash=target;}
nav.splice(nav.findIndex(n=>n[0]==='tasks')+1,0,['schedules','未来安排','tasks']);
nav.push(['faults','报错日志','activity']);
$('#navigation').innerHTML=nav.map(([key,label,type])=>`${key==='models'?'<div class="nav-separator"></div>':''}<button data-route="${key}">${icon(type)}<span>${label}</span>${key==='tasks'?'<span class="nav-count" id="task-count">0</span>':key==='faults'?'<span class="nav-count" id="fault-count" hidden>0</span>':''}</button>`).join('');
hydrateIcons();
installEvidenceUI(action,selection=>{conversation=selection;if(route==='conversations')render();else navigate('conversations');},taskModal);
$('#toggle-sidebar').onclick=()=>document.body.classList.toggle('sidebar-collapsed');
$('#close-modal').onclick=()=>$('#modal').close();
$('#modal').addEventListener('click',e=>{if(e.target===$('#modal'))$('#modal').close();});
$('#refresh-page').onclick=()=>action(()=>render(),'已刷新',false);
$('#toggle-theme').onclick=async()=>{
  if(themeSaving)return;
  const previous=window.solTheme.current();
  const next=previous==='light'?'dark':'light';
  const button=$('#toggle-theme');
  themeSaving=true;button.disabled=true;window.solTheme.apply(next);
  try{
    const saved=await api('settings',{app:{theme:next}});
    config=saved;
    window.solTheme.apply(saved.app.theme);
  }catch(error){window.solTheme.apply(previous);toast('外观保存失败：'+error.message,true);}
  finally{themeSaving=false;button.disabled=false;}
};
window.addEventListener('hashchange',()=>{route=location.hash.slice(1)||'overview';render();});
document.addEventListener('click',e=>{const target=e.target.closest('[data-route]');if(target)navigate(target.dataset.route);});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='k'){e.preventDefault();searchModal();}});
$('#global-search').onclick=searchModal;

async function refreshState(){
  state=await api('state');
  if(loadedRevision&&state.web_revision&&loadedRevision!==state.web_revision){
    if(!formDirty&&!$('#modal').open){location.reload();return;}
    $('#notice').textContent='Sol已更新；请先保存当前编辑，再刷新网页。';$('#notice').classList.remove('hidden');
    return;
  }
  if(!themeSaving)window.solTheme.apply(state.theme||config?.app.theme||'light');
  window.chrome?.webview?.postMessage({type:'preferences',theme:state.theme||'light',autostart:!!config?.app.autostart});
  samples.push({cpu:state.resources.cpu});if(samples.length>60)samples.shift();
  $('#sidebar-name').textContent=state.account.qq?`Sol · ${state.account.qq}`:'机器人未配置';
  $('#sidebar-state').textContent=state.online?'QQ 在线 · 正常运行':state.core.error?'核心需要处理':'QQ 未连接';
  $('#connection-dot').classList.toggle('online',state.online);
  $('#global-status').innerHTML=`<i class="dot ${state.online?'online':''}"></i>${state.online?'QQ 已连接':'QQ 未连接'}`;
  $('#model-chip').innerHTML=esc(state.model||'选择主模型')+' <span>⌄</span>';
  $('#task-count').textContent=(state.task_counts.running||0)+(state.task_counts.queued||0);
  const faultCount=$('#fault-count');if(faultCount){faultCount.textContent=state.open_faults||0;faultCount.hidden=!state.open_faults;}
  $('#last-refresh').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN');
  $('#footer-status').textContent=`仅本机访问 · 已运行 ${Math.floor(state.uptime/60)} 分钟 · 同一套核心与数据`;
  const notice=$('#notice');
  if(state.core.error){notice.innerHTML=`内部核心未启动：${esc(state.core.error)} <button data-route="settings">打开设置</button>`;notice.classList.remove('hidden');}else notice.classList.add('hidden');
}

async function render(quiet=false){
  if(rendering)return;
  rendering=true;
  const requested=location.hash.slice(1)||'overview',release=regionLoading($('#page'));
  try{
    config=await api('settings');await refreshState();
    route=location.hash.slice(1)||'overview';
    $('#page-name').textContent=nav.find(n=>n[0]===route)?.[1]||'设置与运行环境';
    $$('nav [data-route]').forEach(b=>b.classList.toggle('active',b.dataset.route===route));
    let html;
    if(route==='overview')html=dashboard(state,config.layout.widgets,samples,gpu);
    else if(route==='tasks')html=await tasksPage();
    else if(route==='schedules')html=await schedulesPage();
    else if(route==='conversations')html=await conversationsPage(conversation);
    else if(route==='files')html=await filesPage();
    else if(route==='memory')html=await memoryPage();
    else if(route==='persona')html=await personaPage();
    else if(route==='models')html=await modelsPage(config);
    else if(route==='usage')html=await usagePage();
    else if(route==='features')html=await featuresPage(config);
    else if(route==='plugins')html=await pluginsPage();
    else if(route==='activity')html=await activityPage();
    else if(route==='faults')html=await faultsPage();
    else html=await settingsPage(config,settingsTab);
    $('#page').innerHTML=html;release.attach();hydrateIcons($('#page'));wire();
    if(route==='models'){
      let plan;try{plan=await api('plan/status');}catch(error){plan={error:'套餐状态读取失败：'+error.message};} 
      $('#plan-status').innerHTML=plan.error?esc(plan.error):plan.signed_in?`已连接 ${esc(plan.email||'GPT套餐账号')}${plan.paused?' · 调用已暂停 '+button('恢复尝试调用','id="resume-plan"','small'):''}`:'尚未连接套餐账号';
      $('#resume-plan')?.addEventListener('click',()=>action(()=>api('plan/resume',{}),'已解除本地暂停；供应商额度不变'));
    }
    if(route==='overview'){wireDashboard(config,async widgets=>{await api('settings',{layout:{widgets}});await renderAfter();});await populateDashboard();$('#dashboard-refresh').onclick=()=>render();}

    window.solDesktop?.ready();
  }catch(error){window.solDesktop?.fail(error.message);if(!quiet)toast(error.message,true);if(!$('#page').innerHTML)$('#page').innerHTML=heading('暂时无法取得数据','请确认核心服务已启动。')+empty(error.message,'activity');}
  finally{release();rendering=false;if((location.hash.slice(1)||'overview')!==requested)queueMicrotask(()=>render());}
}
async function renderAfter(){rendering=false;await render();}
async function action(work,success='已保存',refresh=true){const control=actionControl();if(busy(control))return;try{await withBusy(control,work);toast(success);if(refresh)await renderAfter();}catch(error){toast(error.message,true);}}

function filter(){
  const text=($('#filter-text')?.value||'').toLowerCase();
  const status=$('#filter-status')?.value||$('#memory-kind')?.value||$('#file-status')?.value||'';
  $$('[data-filter]').forEach(el=>el.classList.toggle('hidden',!el.dataset.filter.toLowerCase().includes(text)||(status==='favorite'?el.dataset.favorite!=='1':status&&el.dataset.status!==status)));
}
function values(form){
  const data={};
  $$('input,select,textarea',form).forEach(input=>{if(!input.name)return;data[input.name]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value.trim();});
  return data;
}
function bindForm(id,handler){const form=$(id);if(form)form.onsubmit=async e=>{e.preventDefault();if(busy(form))return;const data=values(form);try{await withBusy(form,()=>handler(data));formDirty=false;toast('已保存');await renderAfter();}catch(error){toast(error.message,true);}};}

function wire(){
  wireSchedules((work,message,refresh=true)=>action(async()=>{await work();if(refresh)formDirty=false;},message,refresh));
  if(route==='settings'&&window.chrome?.webview&&!$('#desktop-actions')){
    $('#page').insertAdjacentHTML('beforeend',`<section id="desktop-actions" class="panel"><h2>桌面与运行环境</h2><div class="actions">${button('打开数据目录','data-desktop-command="open-data"')}${button('查看引擎日志','data-desktop-command="open-logs"')}${button('停止内部引擎','data-desktop-command="stop-engine"')}${button('重启内部引擎','data-desktop-command="restart-engine"')}${button('退出并停止机器人','data-desktop-command="exit"')}</div></section>`);
    $$('[data-desktop-command]').forEach(b=>b.onclick=()=>window.chrome.webview.postMessage({type:b.dataset.desktopCommand}));
  }

  $('#filter-text')?.addEventListener('input',filter);
  ['#filter-status','#memory-kind','#file-status'].forEach(s=>$(s)?.addEventListener('change',filter));
  $$('[data-task]').forEach(el=>el.onclick=()=>action(()=>taskModal(el.dataset.task),'已打开',false));
  wireConversations(scope=>{conversation=scope;render();});
  $$('[data-preview]').forEach(b=>b.onclick=()=>previewFile(b.dataset.preview));
  $$('[data-favorite-file]').forEach(b=>b.onclick=()=>action(()=>api(`files/${b.dataset.favoriteFile}/favorite`,{value:b.dataset.value==='true'})));
  $$('[data-resend]').forEach(b=>b.onclick=()=>action(async()=>{const path=`files/${b.dataset.resend}`,s=await api(path+'/resend-state');if(s.unknown&&!confirm('上次送达未确认。请先核对QQ；确定仍要重发此文件？'))return;await api(path+'/resend',{confirm_unknown:s.unknown});},'文件重发操作已结束'));
  $$('[data-delete-memory]').forEach(b=>b.onclick=()=>{if(confirm('删除这条记忆？原消息保留，以后仍可能重新学会。'))action(()=>api('memories/'+b.dataset.deleteMemory,{}),'记忆已删除');});
  $$('[data-edit-memory]').forEach(b=>b.onclick=()=>editMemory(b.dataset.editMemory));
  $('#edit-persona')?.addEventListener('click',editPersona);
  $$('[data-persona-version]').forEach(b=>b.onclick=()=>personaVersion(b.dataset.personaVersion));
  $$('[data-restore-persona]').forEach(b=>b.onclick=()=>{if(confirm('将这个版本恢复为当前人格？变化历史继续保留。'))action(()=>api('persona',{restore:b.dataset.restorePersona}),'已恢复人格版本');});
  $('#add-provider')?.addEventListener('click',()=>providerModal());
  $$('[data-provider-preset]').forEach(b=>b.onclick=()=>providerModal(null,b.dataset.providerPreset));
  $$('[data-edit-provider]').forEach(b=>b.onclick=()=>providerModal(b.dataset.editProvider));
  $$('[data-select-provider]').forEach(b=>b.onclick=()=>action(()=>api('settings',{models:{selected:b.dataset.selectProvider}}),'已应用到后续新请求'));
  $$('[data-save-effort]').forEach(b=>b.onclick=()=>{const id=b.dataset.saveEffort;const list=structuredClone(config.models.providers);const p=list.find(p=>p.id===id);p.effort_mode=document.getElementsByName('mode-'+id)[0].value;p.effort=modelEffort(p);action(()=>api('settings',{models:{providers:list}}));});

  wireModelSliders(config.models.providers);
  $$('[data-default-effort]').forEach(b=>b.onclick=()=>{const list=structuredClone(config.models.providers);const p=list.find(p=>p.id===b.dataset.defaultEffort);p.effort='default';p.effort_mode='fixed';action(()=>api('settings',{models:{providers:list}}),'已恢复模型默认');});

  $$('[data-reset-user]').forEach(b=>b.onclick=()=>{if(confirm('重置这个归属在本软件的当期限额？不会改变供应商额度。'))action(()=>api('usage/reset',{owner:b.dataset.resetUser}),'本地额度已重置');});
  $('#reset-all-usage')?.addEventListener('click',()=>{if(confirm('重置所有本地用户与公共限额？不会改变GPT套餐或API余额。'))action(()=>api('usage/reset',{}),'本地额度已重置');});
  bindForm('#quota-form',async v=>{const overrides={};v.overrides.split('\n').filter(Boolean).forEach(line=>{const [id,n]=line.split('=');if(!/^\d+$/.test(id?.trim()||'')||!Number.isFinite(Number(n))||Number(n)<0)throw new Error('个人覆盖值格式应为 QQ号=非负限额');overrides[id.trim()]=Number(n);});await api('settings',{usage:{user_daily:v.user_daily,public_daily:v.public_daily,unknown_blocks:v.unknown_blocks,overrides}});});
  $$('[data-usage]').forEach(b=>b.onclick=()=>usageDetail(b.dataset.usage));
  $$('[data-feature]').forEach(b=>b.onchange=()=>action(()=>api('settings',{features:{[b.dataset.feature]:b.checked}}),'功能状态已更新'));
  bindForm('#native-form',v=>api('settings',{native:nativeValues(v)}));
  $('#prepare-docker-tools')?.addEventListener('click',()=>action(()=>api('tools/docker/prepare',{}),'已开始后台准备常用工具，结果见活动记录'));
  $('#native-upload-skill')?.addEventListener('click',()=>{if(confirm('将所选技能包上传到当前官方API账号？上传后会记住云端ID，不执行技能测试。'))action(()=>api('native/skills/upload',{extension_id:$('#native-skill-picker').value}),'技能已上传');});
  bindForm('#search-form',async v=>{if(v.key)await api('secrets',{name:'search',value:v.key});await api('settings',{search:{endpoint:v.endpoint}});});
  bindForm('#images-form',async v=>{if(v.key)await api('secrets',{name:'images',value:v.key});delete v.key;v.order=v.order.split(',').map(s=>s.trim()).filter(Boolean);if(v.order.some(s=>!['local','remote','native'].includes(s)))throw new Error('顺序只支持local、remote、native');await api('settings',{images:v});});
  bindForm('#local-form',v=>api('settings',{models:{local_url:v.local_url,local_model:v.local_model},video:{whisper_model:v.whisper_model,ffmpeg:v.ffmpeg}}));
  $('#install-extension')?.addEventListener('click',()=>{modal('安装插件或技能',`<form id="install-form"><div class="form-grid">${field('仓库 HTTPS 链接','source','')}${field('仓库内子目录（可选）','subpath','')}</div><p class="form-description small-space">安装后显示实际兼容级别。涉及已有组件升级时仍由管理员决定。</p>${button('下载并安装','type="submit"','primary small-space')}</form>`);bindForm('#install-form',async v=>{const result=await api('extensions/install',v);toast(result.enabled?'扩展已启用':'扩展已下载，请查看兼容与依赖状态');$('#modal').close();});});
  $$('[data-extension]').forEach(b=>b.onchange=()=>action(()=>api(`extensions/${b.dataset.extension}/toggle`,{enabled:b.checked})));
  $$('[data-extension-detail]').forEach(b=>b.onclick=async()=>{const rows=await api('extensions');const r=rows.find(r=>r.id===b.dataset.extensionDetail);modal(r.name,details(parse(r.manifest))+(r.error?details(r.error):''));});
  $$('[data-extension-config]').forEach(b=>b.onclick=()=>action(async()=>{const id=b.dataset.extensionConfig,data=await api(`extensions/${id}/config`);modal('插件配置',`<details class="diagnostic"><summary>字段说明</summary>${details(data.schema)}</details><form id="extension-config-form">${field('配置 JSON','values',JSON.stringify(data.values,null,2),'textarea')}${button('保存配置','type="submit"','primary small-space')}</form>`);bindForm('#extension-config-form',async v=>{await api(`extensions/${id}/config`,JSON.parse(v.values));$('#modal').close();});},'已打开',false));
  $$('[data-extension-update]').forEach(b=>b.onclick=()=>{if(confirm('下载这个扩展的新版本？旧源码会保留；涉及已有依赖变更仍需另行确认。'))action(()=>api(`extensions/${b.dataset.extensionUpdate}/update`,{}),'源码已更新，请查看兼容与依赖状态');});
  $$('[data-extension-rollback]').forEach(b=>b.onclick=()=>action(()=>api(`extensions/${b.dataset.extensionRollback}/rollback`,{}),'已恢复旧源码，请检查依赖后启用'));
  $$('[data-extension-remove]').forEach(b=>b.onclick=()=>{if(confirm('停用并移除这个扩展？安装来源和源码归档会保留。'))action(()=>api(`extensions/${b.dataset.extensionRemove}/uninstall`,{}),'扩展已移除');});
  $$('[data-extension-deps]').forEach(b=>b.onclick=async()=>{try{const data=await api(`extensions/${b.dataset.extensionDeps}/dependencies`,{});if(data.needs_admin){modal('依赖变更需要确认',`<p>这个扩展需要修改已有组件：</p>${details(data.upgrades)}${button('允许这些更新','id="approve-dependencies"','primary')}`);$('#approve-dependencies').onclick=()=>action(async()=>{await api(`extensions/${b.dataset.extensionDeps}/dependencies`,{allow_updates:true});$('#modal').close();},'依赖已准备');}else toast('依赖已准备');}catch(e){toast(e.message,true);}});
  $$('[data-event]').forEach(b=>b.onclick=async()=>{const rows=await api('events');const row=rows.find(r=>r.id===b.dataset.event);modal(row.title,`<p class="muted">${time(row.created)} · ${esc(row.kind)}</p>`+details(parse(row.detail)));});
  $$('[data-settings-tab]').forEach(b=>b.onclick=()=>{settingsTab=b.dataset.settingsTab;render();});
  bindForm('#settings-form',async v=>{const section=$('#settings-form').dataset.section;const payload={};if('app.autostart' in v){payload.app={autostart:v['app.autostart']};delete v['app.autostart'];}['admins','users','groups'].forEach(k=>{if(k in v)v[k]=v[k].split(/[,，\s]+/).filter(Boolean);});if('type_limits' in v)v.type_limits=JSON.parse(v.type_limits);if('send_min' in v){v.send_interval=[v.send_min,v.send_max];delete v.send_min;delete v.send_max;}payload[section]=v;const result=await api('settings',payload);if(result.restart_required)toast('已保存，账号接入配置需要重启内部核心后生效');});
  bindForm('#social-form',v=>{v.quiet_hours=[v.quiet_start,v.quiet_end];delete v.quiet_start;delete v.quiet_end;return api('settings',{social:v});});
  bindForm('#backup-policy-form',v=>api('settings',{app:v}));
  bindForm('#group-form',v=>{const id=v.group;if(!/^\d+$/.test(id))throw new Error('请输入有效群号');delete v.group;return api('settings',{access:{group_overrides:{['group:'+id]:v}}});});
  bindForm('#qq-secret-form',v=>api('secrets',{name:'llbot_auth',value:v.token}));
  $$('[data-service]').forEach(b=>b.onclick=()=>action(()=>api('services/'+b.dataset.service,{}),'服务操作已完成'));
  $('#qq-login')?.addEventListener('click',qqLoginModal);
  $('#create-backup')?.addEventListener('click',()=>action(()=>api('backups',{}),'备份已保存'));
  $$('[data-restore-backup]').forEach(b=>b.onclick=()=>{if(confirm('准备恢复此备份？下次启动应用时生效，旧任务不会续跑。'))action(()=>api('backups/restore',{name:b.dataset.restoreBackup}),'恢复点已准备，请退出后重新打开应用');});
}

async function previewFile(id){const rows=await api('files');const f=rows.find(f=>f.id===id);if(!f)return;if(f.status!=='ready'){modal(f.name,'<p>文件已清理，记录仍然保留。</p>');return;}const url=`/api/files/${id}/preview`;modal(f.name,/\.(png|jpe?g|webp)$/i.test(f.name)?`<img class="modal-image" src="${url}" alt="${esc(f.name)}">`:/\.pdf$/i.test(f.name)?`<iframe class="preview-file" src="${url}" title="PDF预览"></iframe>`:/\.(mp4|webm)$/i.test(f.name)?`<video class="modal-image" src="${url}" controls></video>`:`<p>${esc(f.name)} · ${bytes(f.size)}</p><a class="button" href="/api/files/${id}/download">下载文件</a>`);}
async function editMemory(id){const row=(await api('memories')).find(r=>r.id===id);modal('编辑记忆',`<form id="memory-form">${field('内容','text',row.text,'textarea')}<p class="form-description small-space">保留原始提供者与来源，不改写别人的原始发言。</p>${button('保存','type="submit"','primary small-space')}</form>`);bindForm('#memory-form',async v=>{await api('memories/'+id,v);$('#modal').close();});}
async function editPersona(){const row=(await api('persona')).at(-1);modal('编辑当前人格',`<form id="persona-form">${field('人格说明','text',row.text,'textarea')}${field('修改原因','reason','管理员编辑')}<div class="form-grid small-space">${Object.entries(parse(row.dimensions)).map(([key,value])=>field(key,'dimension:'+key,value,'number')).join('')}</div>${button('保存新版本','type="submit"','primary small-space')}</form>`);bindForm('#persona-form',async v=>{const dimensions={};Object.keys(v).filter(k=>k.startsWith('dimension:')).forEach(k=>{dimensions[k.slice(10)]=v[k];delete v[k];});await api('persona',{...v,dimensions});$('#modal').close();});}
async function personaVersion(id){const rows=await api('persona');const row=rows.find(r=>r.id===id);const old=rows.find(r=>r.id===row.parent);modal('人格版本对比',`<p>${esc(row.reason)}</p><div class="two-columns"><div><h3>此前</h3><div class="diagnostic">${esc(old?.text||'初始版本')}</div></div><div><h3>此版本</h3><div class="diagnostic">${esc(row.text)}</div></div></div><h3 class="small-space">变化依据</h3>${details(parse(row.evidence,[]))}<h3>维度</h3>${details(parse(row.dimensions))}`);}
async function usageDetail(id){const row=(await api('usage')).calls.find(r=>r.id===id);const breakdown=parse(row.breakdown);modal('模型调用明细',`<div class="form-grid"><div><p>实际输入：${number(row.input)} Token</p><p>实际输出：${number(row.output)} Token</p></div><div><p>缓存输入：${number(row.cached)}</p><p>思考输出：${number(row.reasoning)}</p></div></div><h3>输入来源字符统计</h3><p class="form-description">这是本地可观测的字符数量，不是供应商实际 Token 分摊，不能与实际输入直接相加比较。</p>${details(breakdown)}${breakdown.non_token_api?'<p class="form-description">本条为按请求计费的服务商工具接口，输入和输出Token为0不代表免费；未返回的工具费用不估算为实际金额。</p>':''}<p class="form-description">未返回的用量显示未知，不等同于零。</p>`);}
function providerModal(id,preset){
  const defaults={responses:{name:'OpenAI API',kind:'responses',base_url:'https://api.openai.com/v1'},plan:{name:'GPT 套餐',kind:'plan',base_url:'http://127.0.0.1:24762/v1'},deepseek:{name:'DeepSeek',kind:'openai',base_url:'https://api.deepseek.com/v1'},gemini:{name:'Gemini',kind:'gemini',base_url:'https://generativelanguage.googleapis.com'}};
  const row=config.models.providers.find(p=>p.id===id)||{id:'',name:'',kind:'openai',model:'',base_url:'',effort:'default',effort_mode:'fixed',efforts:[],remember_key:true,...defaults[preset]};
  modal(id?'编辑模型':'添加模型',`<form id="provider-form"><div class="form-grid">${field('显示名称','name',row.name)}${field('接口类型','kind',row.kind,[['openai','OpenAI兼容'],['responses','Responses API'],['plan','GPT套餐桥'],['gemini','Gemini原生']])}${field('API地址','base_url',row.base_url)}</div><div class="api-secret small-space">${field('API Key','key','','password','已保存时留空即可复用；输入新值会在保存时更换。')}${field('记住此服务商密钥，添加其他型号时复用','remember_key',row.remember_key!==false,'checkbox')}</div><p id="credential-status" class="form-description" role="status"></p><div class="form-grid small-space">${field('模型型号','model',row.model,'text','从列表选择，也可手动填写。')}<div class="catalog-fetch">${button('获取模型列表','id="fetch-model-catalog" type="button"')}<p class="form-description">只读取可用型号，不发送测试问题。</p></div></div><p id="model-catalog-status" class="form-description" role="status"></p><select id="model-catalog-picker" aria-label="选择可用模型" hidden></select><datalist id="model-catalog-options"></datalist><details class="small-space"><summary>高级配置</summary><div class="form-grid small-space">${field('模型配置ID（留空自动生成）','id',row.id)}${field('自定义支持的档位','efforts',(row.efforts||[]).join(','),'text','通常无需填写；服务商已知型号会自动识别。仅填写该型号实际支持的原生值。')}</div></details>${button('保存模型','type="submit"','primary small-space')}</form>`);
  const form=$('#provider-form'),picker=$('#model-catalog-picker'),fetchButton=$('#fetch-model-catalog'),status=$('#model-catalog-status'),credential=$('#credential-status');
  const active=()=>$('#provider-form')===form&&$('#modal').open;
  form.elements.namedItem('key').setAttribute('autocomplete','new-password');
  const modelInput=form.elements.namedItem('model');modelInput.setAttribute('list','model-catalog-options');
  let catalog=[],credentialTimer,credentialVersion=0;
  const catalogSpec=()=>({kind:form.elements.namedItem('kind').value,base_url:form.elements.namedItem('base_url').value.trim(),key:form.elements.namedItem('key').value,id});
  const credentialStatus=async()=>{
    const spec=catalogSpec(),version=++credentialVersion,plan=spec.kind==='plan';
    form.querySelector('.api-secret').hidden=plan;
    if(plan){credential.textContent='GPT套餐的授权由Sol管理，请先登录套餐账号。';return true;}
    try{const data=await api('models/credential-status',{kind:spec.kind,base_url:spec.base_url,id});if(active()&&version===credentialVersion)credential.textContent=data.saved?'已保存此服务的密钥，可留空复用。':'此服务尚未保存密钥，请填写。';return data.saved;}
    catch(e){if(active()&&version===credentialVersion)credential.textContent=e.message;return false;}
  };
  ['kind','base_url'].forEach(name=>form.elements.namedItem(name).addEventListener('input',()=>{clearTimeout(credentialTimer);credentialTimer=setTimeout(credentialStatus,250);}));
  form.elements.namedItem('kind').addEventListener('change',()=>{
    const kind=form.elements.namedItem('kind').value;
    const address=form.elements.namedItem('base_url');
    if(kind==='plan')address.value='http://127.0.0.1:24762/v1';
    else if(kind==='gemini')address.value='https://generativelanguage.googleapis.com';
    else if(address.value.includes('127.0.0.1:24762')||address.value.includes('generativelanguage.googleapis.com'))address.value='';
    catalog=[];picker.hidden=true;credentialStatus();
  });
  const chooseModel=value=>{const item=catalog.find(m=>m.id===value);if(!item)return;modelInput.value=item.id;form.elements.namedItem('efforts').value=(item.efforts||[]).join(',');formDirty=true;};
  picker.onchange=()=>chooseModel(picker.value);
  modelInput.addEventListener('change',()=>chooseModel(modelInput.value));
  fetchButton.onclick=async()=>{
    const spec=catalogSpec(),signature=JSON.stringify(spec);fetchButton.disabled=true;status.textContent='正在获取模型列表…';status.classList.remove('error-text');picker.hidden=true;
    try{
      const data=await api('models/catalog',spec);if(!active())return;
      if(JSON.stringify(catalogSpec())!==signature){status.textContent='接口或密钥已变化，请重新获取模型列表。';return;}
      catalog=data.models;picker.innerHTML='<option value="">请选择模型</option>'+catalog.map(m=>`<option value="${esc(m.id)}">${esc(m.name===m.id?m.id:m.name+' · '+m.id)}</option>`).join('');
      $('#model-catalog-options').innerHTML=catalog.map(m=>`<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
      picker.value=catalog.some(m=>m.id===modelInput.value)?modelInput.value:'';picker.hidden=false;
      status.textContent=`已获取 ${catalog.length} 个型号，请下拉选择。${data.warning||''}`;
    }catch(e){if(active()){status.textContent=e.message;status.classList.add('error-text');}}
    finally{fetchButton.disabled=false;}
  };
  bindForm('#provider-form',async v=>{
    if(!v.model)throw new Error('请选择或填写模型型号');
    if(v.kind==='plan')v.base_url='http://127.0.0.1:24762/v1';
    else if(!v.key&&!await credentialStatus())throw new Error('请填写API Key，或使用已保存密钥的服务地址');
    v.efforts=v.efforts.split(/[,，]/).map(s=>s.trim()).filter(Boolean);
    const listed=catalog.find(m=>m.id===v.model);
    if(listed?.reasoning&&listed.reasoning.kind!=='unknown')v.reasoning=listed.reasoning;
    else if(v.efforts.length)v.reasoning={kind:'levels',values:v.efforts,source:'manual',model:v.model.toLowerCase().replace(/^models\//,'')};
    v.effort=v.model===row.model?row.effort:'default';v.effort_mode=row.effort_mode;v.enabled=true;
    if(!v.id)v.id=v.kind+'-'+v.model.replace(/[^a-zA-Z0-9_-]/g,'-');
    if(config.models.providers.some(p=>p.id===v.id&&p.id!==id))throw new Error('此配置ID已存在，可在高级配置中换一个ID');
    if(!v.key)delete v.key;
    const list=config.models.providers.filter(p=>p.id!==id);list.push(v);
    await api('settings',{models:{providers:list,selected:id&&config.models.selected===id?v.id:config.models.selected||v.id}});$('#modal').close();
  });
  credentialStatus();
}
async function searchModal(){modal('搜索工作空间',`<input id="search-input" placeholder="搜索消息、记忆、任务和文件" autofocus><div id="search-results" class="small-space">${empty('输入关键词开始搜索','search')}</div>`);let timer;$('#search-input').oninput=()=>{clearTimeout(timer);timer=setTimeout(async()=>{const q=$('#search-input').value.trim();if(!q)return;try{const rows=await api('search?q='+encodeURIComponent(q));$('#search-results').innerHTML=rows.map(r=>`<button class="field-row" style="width:100%;text-align:left" data-search-route="${esc(r.route)}"><span><strong>${esc(r.title)}</strong><small>${esc(r.detail)}</small></span><span class="badge">${esc(r.kind)}</span></button>`).join('')||empty('没有找到相关内容','search');$$('[data-search-route]').forEach(b=>b.onclick=()=>{$('#modal').close();navigate(b.dataset.searchRoute);});}catch(e){toast(e.message,true);}},250);};}

document.addEventListener('click',e=>{const cancel=e.target.closest('[data-cancel-task]');if(cancel&&confirm('停止这个任务？已有产物会保留。'))action(async()=>{await api(`tasks/${cancel.dataset.cancelTask}/cancel`,{});$('#modal').close();},'任务已停止');});
setInterval(async()=>{if(rendering||document.hidden||mutationsPending())return;try{await refreshState();if(route==='conversations'&&!$('#modal').open)await refreshConversations();if(['schedules','faults'].includes(route)&&!$('#modal').open&&!formDirty&&!$('#filter-text')?.value&&!$('#filter-status')?.value)await render(true);if(route==='overview'&&!$('#modal').open&&!$('.widget.dragging')){config=await api('settings');$('#page').innerHTML=dashboard(state,config.layout.widgets,samples,gpu);wireDashboard(config,async widgets=>{await api('settings',{layout:{widgets}});await renderAfter();});wire();await populateDashboard();$('#dashboard-refresh').onclick=()=>render();} }catch{}},5000);
setInterval(async()=>{try{gpu=await api('gpu');}catch{}},15000);
await render();
