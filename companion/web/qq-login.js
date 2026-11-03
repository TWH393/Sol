import {$,esc,api,modal,button} from './ui.js';

// Poll only while the user has this login dialog open. Never starts a login on page refresh.
export async function qqLoginModal(){
  modal('登录机器人 QQ',`<div class="qq-login-layout"><div><div id="qq-login-state" class="diagnostic">正在读取连接状态…</div><p class="form-description small-space">只使用指定机器人账号扫码。关闭窗口不会退出已登录的账号。</p><div class="actions small-space">${button('启动并扫码登录','id="qq-start"','primary')}${button('刷新二维码','id="qq-refresh"')}${button('停止服务','id="qq-stop"')}</div><div id="qq-login-error" class="error-text small-space" role="alert"></div></div><div class="qq-code"><img id="qq-login-image" alt="登录二维码" hidden><p id="qq-code-hint">点击“启动并扫码登录”获取二维码</p></div></div>`);
  const dialog=$('#modal'),body=$('#modal-body');
  $('#qq-login-error').insertAdjacentHTML('afterend',`<details class="small-space"><summary>登录遇到问题</summary><div class="actions small-space">${button('显示 QQ 诊断窗口','id="qq-show-window"')}${button('隐藏 QQ 窗口','id="qq-hide-window"')}</div></details>`);
  let closed=false,timer,expires=0,busy=false;
  const active=()=>!closed&&dialog.open&&body.contains($('#qq-start'));
  const cleanup=()=>{closed=true;clearTimeout(timer);};
  dialog.addEventListener('close',cleanup,{once:true});
  const error=text=>{if(active())$('#qq-login-error').textContent=text;};
  async function status(){
    const s=await api('qq/status');if(!active())return;
    $('#qq-login-state').innerHTML=`<strong>指定账号：${esc(s.expected||'尚未填写')}</strong><br>${esc(s.error||(s.online?(s.connected?'QQ已登录，机器人已连接':'QQ已登录，等待机器人连接'):s.stage||'QQ服务未启动'))}${s.actual?'<br>当前账号：'+esc(s.actual):''}`;

    if(s.online){$('#qq-login-image').hidden=true;$('#qq-code-hint').textContent=s.error?'账号不符，请停止服务后处理':'已登录，无需再次扫码';}
    else if(['restoring_login','needs_scan','qrcode_failed','error','offline','starting_session','stopped'].includes(s.phase)){$('#qq-login-image').hidden=true;$('#qq-code-hint').textContent=s.phase==='restoring_login'?'正在自动恢复指定账号的登录，无需扫码':s.phase==='starting_session'?'登录已确认，正在准备消息接入':s.phase==='needs_scan'?'需要重新扫码，请点击刷新二维码':'二维码不可用，请按左侧提示处理';}
    else if(expires&&Date.now()>expires){$('#qq-login-image').hidden=true;$('#qq-code-hint').textContent='二维码已过期，请点击刷新二维码';}
    return s;
  }
  async function qrcode(){
    expires=0;$('#qq-login-image').hidden=true;$('#qq-code-hint').textContent='正在获取二维码…';
    const q=await api('qq/qrcode',{});if(!active())return;
    if(q.online){await status();return;}
    $('#qq-login-image').src='data:image/png;base64,'+q.image;$('#qq-login-image').hidden=false;
    expires=Date.now()+q.expires_in*1000;
    $('#qq-code-hint').textContent=`请用 ${q.expected} 的手机QQ扫码并确认，二维码约 ${q.expires_in} 秒内有效`;
  }
  function wire(id,work){$(id).onclick=async()=>{if(busy)return;busy=true;const buttons=[...body.querySelectorAll('button')];buttons.forEach(button=>button.disabled=true);$(id).classList.add('is-busy');error('');try{await work();}catch(e){error(e.message);}finally{busy=false;if(active()){buttons.forEach(button=>button.disabled=false);$(id).classList.remove('is-busy');}}};}
  wire('#qq-start',async()=>{await api('services/qq/start',{});await status();await qrcode();});
  wire('#qq-refresh',qrcode);
  wire('#qq-stop',async()=>{await api('services/qq/stop',{});expires=0;$('#qq-login-image').hidden=true;$('#qq-code-hint').textContent='QQ服务已停止';await status();});
  wire('#qq-show-window',()=>api('services/qq/show-window',{}));
  wire('#qq-hide-window',()=>api('services/qq/hide-window',{}));

  async function poll(){if(!active())return;try{if(!busy)await status();}catch(e){error(e.message);}if(active())timer=setTimeout(poll,3000);}
  // Stop unattended polling after five minutes; the user can reopen the dialog.
  setTimeout(cleanup,300000);
  await poll();
}
