import {esc,field,button} from './ui.js';

export function nativePanel(config){
  const value=config.native,provider=config.models.providers.find(p=>p.id===config.models.selected);
  const rows=config.effective_capabilities||{};
  return `<section class="panel small-space"><div class="panel-heading"><h2>工具能力与执行方式</h2><span class="badge">${esc(provider?.name||'先选择模型')}</span></div><p class="form-description">Sol 本地工具独立于模型服务。这里显示配置具备的能力，不会为检查状态而启动浏览器、Docker 或调用模型；实际运行结果在任务中心记录。</p><div class="actions small-space">${button("准备 Docker 常用工具",'id="prepare-docker-tools" type="button"',"small")}</div><form id="native-form"><div class="feature-grid small-space">${Object.entries(rows).map(([key,item])=>`<article class="feature-card"><div class="panel-heading"><h3>${esc(item.label)}</h3><span class="badge">${esc(item.status)}</span></div>${field('允许按需使用','feature_'+key,value.features[key]!==false,'checkbox')}<p class="form-description"><strong>${esc(item.execution)}</strong></p><p class="form-description">${esc(item.reason)}</p>${item.selectable?field('执行方式','backend_'+key,value.backends?.[key]||'auto',[['auto','自动选择已配置的方式'],['local','Sol 本地工具'],['native','服务商原生']]):''}${item.selectable?`<p class="form-description">${item.native_supported?'当前服务商声明支持原生方式，账号待验收':'当前服务商未声明对应原生方式；可用 Sol 本地工具'}</p>`:''}</article>`).join('')}</div><details class="small-space"><summary>搜索与服务商原生功能</summary><div class="form-grid small-space">${field('允许服务商原生工具','enabled',value.enabled,'checkbox')}${field('搜索方式','search_mode',value.search_mode,[['auto','优先已接入的原生搜索'],['native','只使用当前服务商原生搜索'],['tavily','使用已配置的 Tavily']])}</div><p class="form-description">服务商原生工具可能另行计费。模型切换后，Docker、文档检索、Skills、MCP和匿名浏览仍由 Sol 提供。已开始的任务保留原模型。</p></details><details class="small-space"><summary>MCP 服务与技能配置</summary><p class="form-description">公开 MCP 可自主寻找和登记；已有服务的范围不被自动扩大。audience 为 admins 或 all，allowed_tools 填工具名，公开服务可填 ["*"]。凭据仅在此配置并加密保存，不回显。</p>${field('MCP 服务配置','mcp_servers',JSON.stringify(value.mcp_servers,null,2),'textarea','[{"id":"wiki","url":"https://示例/mcp","audience":"all","allowed_tools":["search"]}]')}${value.mcp_servers.map(s=>field('MCP '+s.id+' 授权凭据','mcp_token_'+s.id,'','password','留空保留，不回显')).join('')}${field('可选云端 Skill ID','skill_ids',value.skill_ids.join('\n'),'textarea','本地技能无需填写；只用于另行配置的原生云端 Skills。')}${field('浏览器允许域名（可选）','browser_domains',value.browser_domains.join('\n'),'textarea','每行一个公开域名。留空可浏览公开网站；不登录、不使用个人浏览器账号。')}</details>${button('保存工具设置','type="submit"','primary small-space')}</form>${(config.native_skills||[]).length?`<details class="small-space"><summary>上传技能到已配置的官方 API</summary><p class="form-description">通常直接使用 Sol 本地技能即可；此项是可选云端方式。</p><div class="actions"><select id="native-skill-picker">${config.native_skills.map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('')}</select>${button('上传并记住云端 ID','id="native-upload-skill" type="button"','small')}</div></details>`:''}</section>`;
}

export function nativeValues(values){
  const features={},backends={};
  for(const [key,value] of Object.entries(values)){
    if(key.startsWith('feature_'))features[key.slice(8)]=value;
    if(key.startsWith('backend_'))backends[key.slice(8)]=value;
  }
  const servers=JSON.parse(values.mcp_servers||'[]');
  if(!Array.isArray(servers))throw new Error('MCP配置必须为数组');
  for(const server of servers){const token=values['mcp_token_'+server.id];if(token)server.token=token;}
  const lines=text=>(text||'').split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
  return {enabled:values.enabled,search_mode:values.search_mode,features,backends,mcp_servers:servers,skill_ids:lines(values.skill_ids),browser_domains:lines(values.browser_domains)};
}
