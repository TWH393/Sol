// Local UI feedback only. Long bot tasks keep their existing task-center lane.
let clicked = null;
const active = new WeakSet();
document.addEventListener('click', event => {
  const control = event.target.closest?.('button,input[type=checkbox],select');
  if (!control) return;
  if (active.has(control) || control.dataset.solUnconfirmed) {
    event.preventDefault(); event.stopImmediatePropagation(); return;
  }
  clicked = control;
  queueMicrotask(() => { if (clicked === control) clicked = null; });
}, true);
export const actionControl = () => clicked;
export const busy = control => !!control && active.has(control);
export async function withBusy(control, work) {
  if (!control) return await work();
  if (active.has(control)) return;
  active.add(control);
  const nodes = control.matches('form') ? [...control.querySelectorAll('input,select,textarea,button')] : [control];
  const previous = nodes.map(node => [node, node.disabled]);
  control.classList.add('is-busy'); control.setAttribute('aria-busy', 'true');
  nodes.forEach(node => { node.disabled = true; });
  let uncertain = false;
  try { return await work(); }
  catch (error) { uncertain = !!error.unconfirmed; throw error; }
  finally {
    active.delete(control); control.classList.remove('is-busy'); control.removeAttribute('aria-busy');
    previous.forEach(([node, disabled]) => { node.disabled = disabled; });
    if (uncertain) {
      const button = control.matches('form') ? control.querySelector('button[type=submit]') : control;
      if (button) { button.disabled = true; button.dataset.solUnconfirmed = 'true'; button.setAttribute('aria-label', '操作结果未确认，请先查看活动记录'); }
    }
  }
}
export function regionLoading(region) {
  region.setAttribute('aria-busy', 'true');
  const shade = document.createElement('div');
  shade.className = 'region-wait'; shade.setAttribute('role', 'status');
  shade.innerHTML = '<span class="operation-spinner" aria-hidden="true"></span><span>正在加载…</span>';
  region.append(shade);
  const release = () => { shade.remove(); region.removeAttribute('aria-busy'); };
  release.attach = () => region.append(shade);
  return release;
}
