/* Loaded before ES modules: failed imports must not leave an empty window. */
(() => {
  const boot=new URLSearchParams(location.search).get('boot')||'';
  let ready=false,failed=false;
  const post=(type,message)=>window.chrome?.webview?.postMessage({type,boot,message});
  window.solDesktop={
    ready(){
      if(ready||failed)return;
      const done=()=>{if(!ready&&!failed){ready=true;post('page-ready');}};
      requestAnimationFrame(()=>requestAnimationFrame(done));
      setTimeout(done,300);
    },
    fail(message){if(ready||failed)return;failed=true;post('page-error',String(message||'页面初始化失败').slice(0,600));}
  };
  window.addEventListener('error',event=>{
    const target=event.target;
    if(!event.message&&!['SCRIPT','LINK'].includes(target?.tagName))return;
    const path=target?.src||target?.href;
    window.solDesktop.fail(event.message||(path?'界面资源加载失败：'+new URL(path,location.href).pathname:'界面脚本加载失败'));
  },true);
  window.addEventListener('unhandledrejection',event=>window.solDesktop.fail(event.reason?.message||event.reason||'界面初始化请求未完成'));
})();
