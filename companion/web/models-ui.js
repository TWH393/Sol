import {esc,field,button} from './ui.js';

export function modelCard(p,selected){
  const spec=p.reasoning||{kind:'unknown'},choices=['default',...(spec.values||[])];
  let control;
  if(spec.kind==='budget'){
    const isDefault=(p.effort||'default')==='default';
    control=`<label class="reasoning-slider">思考预算 <output data-effort-label="${esc(p.id)}">${esc(isDefault?'模型默认':p.effort)}</output><input type="range" data-model-effort="${esc(p.id)}" min="${spec.min}" max="${spec.max}" step="${spec.step}" value="${esc(isDefault?spec.min:p.effort)}" ${isDefault?'disabled':''}></label><label class="budget-default"><input type="checkbox" data-budget-default="${esc(p.id)}" ${isDefault?'checked':''}>模型默认</label>`;
  }else if(spec.kind==='levels'&&choices.length>1){
    const index=Math.max(0,choices.indexOf(p.effort||'default'));
    control=`<label class="reasoning-slider">思考调节 <output data-effort-label="${esc(p.id)}">${esc(index?choices[index]:'模型默认')}</output><input type="range" data-model-effort="${esc(p.id)}" min="0" max="${choices.length-1}" step="1" value="${index}"><span class="slider-ends"><span>模型默认</span><span>${esc(choices.at(-1))}</span></span></label>`;
  }else control='<p class="form-description">使用模型默认；此型号尚未声明可调范围，可在编辑中填写已知支持的档位。</p>';
  return `<article class="provider-card ${p.id===selected?'selected':''}"><span class="avatar">${esc((p.name||p.id).slice(0,1))}</span><div class="provider-info"><h3>${esc(p.name||p.id)} ${p.id===selected?'<span class="badge success">当前主模型</span>':''}</h3><p>${esc(p.model)} · ${esc(p.kind)}</p><div class="model-settings">${field('调节方式','mode-'+p.id,p.effort_mode||'fixed',[['fixed','固定设置'],['auto','自动调整']])}${control}<div class="actions">${button('保存设置',`data-save-effort="${esc(p.id)}"`,'small')}${button('恢复模型默认',`data-default-effort="${esc(p.id)}"`,'small ghost')}</div></div></div><div class="actions">${p.id!==selected?button('应用到全部会话',`data-select-provider="${esc(p.id)}"`,'small'):''}${button('编辑',`data-edit-provider="${esc(p.id)}"`,'small ghost')}</div></article>`;
}

export function modelEffort(p){
  const input=[...document.querySelectorAll('[data-model-effort]')].find(e=>e.dataset.modelEffort===p.id);
  if(!input)return 'default';
  if(p.reasoning?.kind==='budget')return [...document.querySelectorAll('[data-budget-default]')].find(e=>e.dataset.budgetDefault===p.id)?.checked?'default':input.value;
  return ['default',...(p.reasoning?.values||[])][Number(input.value)]||'default';
}

export function wireModelSliders(providers){
  const update=p=>{
    const value=modelEffort(p);
    const label=[...document.querySelectorAll('[data-effort-label]')].find(e=>e.dataset.effortLabel===p.id);
    if(label)label.textContent=value==='default'?'模型默认':value;
  };
  document.querySelectorAll('[data-model-effort]').forEach(input=>input.oninput=()=>{const p=providers.find(p=>p.id===input.dataset.modelEffort);if(p)update(p);});
  document.querySelectorAll('[data-budget-default]').forEach(box=>box.onchange=()=>{const input=[...document.querySelectorAll('[data-model-effort]')].find(e=>e.dataset.modelEffort===box.dataset.budgetDefault);if(input)input.disabled=box.checked;const p=providers.find(p=>p.id===box.dataset.budgetDefault);if(p)update(p);});
}
