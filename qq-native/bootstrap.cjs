// Sol's local official-QQ SDK adapter. No NapCat/LLBot runtime or remote license.
// Public SDK signatures are referenced in THIRD_PARTY.md; implementation is Sol's.
'use strict';
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const http=require('node:http'),crypto=require('node:crypto'),{app,BrowserWindow}=require('electron');
function trace(text){try{fs.appendFileSync(process.env.SOL_QQ_STAGE_FILE,new Date().toISOString()+' '+text+'\n');}catch{}}
const expected=process.env.SOL_QQ_EXPECTED,profile=process.env.SOL_QQ_PROFILE,key=process.env.SOL_QQ_RPC_KEY;
if(!/^\d+$/.test(expected||'')||!profile||!key)throw new Error('Sol QQ启动参数不完整');
if(globalThis.__solQqAdapter)module.exports=globalThis.__solQqAdapter;
else {
  if(!app.isReady())app.setPath('userData',profile);
  // Affect only this isolated QQ process, never the user's ordinary QQ windows.
  let diagnosticWindowVisible=false;
  function concealWindow(window){if(!diagnosticWindowVisible&&!window.isDestroyed()){window.setSkipTaskbar(true);window.hide();}}
  app.on('browser-window-created',(_event,window)=>{
    concealWindow(window);window.on('show',()=>concealWindow(window));window.on('ready-to-show',()=>concealWindow(window));
  });
  for(const window of BrowserWindow.getAllWindows())concealWindow(window);
  const state={expected,actual:'',online:false,driver:'native',stage:'正在初始化QQ原生接口',pid:process.pid,received:0,sent:0,error:'',phase:'initializing'};
  const peers=new Map(),events=[],seen=new Set(),sentWaiters=new Map(),mediaRefs=new Map(),mediaWaiters=new Map(),mediaJobs=new Map(),references=[],messageRefs=new Map();
  const bootTime=Math.floor(Date.now()/1000);
  let paceTail=Promise.resolve(),lastSubmitted=0;
  let wrapper,login,session,startupSession,msgService,version,appid,dataRoot,initialized=false,connected=false,locked=false,closed=false,sessionStarted=false;
  let qrPending=null,sessionTimer=null;
  const autoLoginEnabled=process.env.SOL_QQ_AUTO_LOGIN==='1';
  let autoLoginAttempted=false,autoLoginPending=false,autoLoginTimer=null;
  let sessionStep='尚未开始',sessionSignal='无',msfSignals=0;
  function admitted(){return !locked&&!closed&&state.online&&state.actual===expected;}
  function fail(stage,message){clearTimeout(sessionTimer);state.online=false;state.phase='error';state.stage=stage;state.error=message;trace(stage+'：'+message);}
  function errorCode(args){return args.filter(x=>typeof x==='number'||typeof x==='string'&&/^-?\d{1,12}$/.test(x)).slice(0,3).join('/');}
  function listener(handlers){
    // SDK invokes optional callbacks too. Retain objects for native callback lifetime.
    const target=new Proxy(handlers,{get:(o,k)=>k==='then'?undefined:k in o?o[k]:()=>{}});
    references.push(target);return target;
  }
  function rejectQr(message){if(qrPending){clearTimeout(qrPending.timer);qrPending.reject(new Error(message));qrPending=null;}}
  function cancelSends(message){for(const waiter of sentWaiters.values())waiter.reject(new Error(message));sentWaiters.clear();}
  function cancelMedia(message){for(const waiter of mediaWaiters.values())waiter.reject(new Error(message));mediaWaiters.clear();mediaRefs.clear();}
  function offline(message){
    connected=false;state.online=false;state.phase='offline';state.stage='QQ连接已断开';state.error=message;
    clearTimeout(autoLoginTimer);autoLoginPending=false;
    clearTimeout(sessionTimer);rejectQr(message);cancelSends(message);cancelMedia(message);events.length=0;peers.clear();messageRefs.clear();
  }
  function metadata(){
    const base=path.join(process.resourcesPath,'app');
    const manifest=JSON.parse(fs.readFileSync(path.join(base,'package.json'),'utf8'));
    version=manifest.version;
    if(!/^\d+\.\d+\.\d+-\d+$/.test(version))throw new Error('当前QQ程序版本格式尚未适配');
    // Public AppID comes from this exact binary, never a guessed other version.
    const binary=fs.readFileSync(path.join(base,'major.node')),marker=Buffer.from('QQAppId/');
    for(let offset=0;offset<binary.length;){
      const at=binary.indexOf(marker,offset);if(at<0)break;
      const start=at+marker.length,end=binary.indexOf(0,start);if(end<0)break;offset=end+1;
      if(end-start<16){const value=binary.subarray(start,end).toString('ascii');if(/^\d{6,12}$/.test(value)){appid=value;break;}}
    }
    if(!appid)throw new Error('未能从此版本QQ程序读取公开AppID，尚不能初始化登录');
    return {base,qua:`V1_WIN_${version}_${manifest.buildVersion}_GW_B`};
  }
  function prepareSession(){
    // Create before engine/login initialization. A pre-created native object
    // does not mean a QR login has succeeded; use sessionStarted for that.
    try{
      if(typeof wrapper.NodeIQQNTStartupSessionWrapper?.create!=='function'||typeof wrapper.NodeIQQNTWrapperSession.getNTWrapperSession!=='function')throw new Error('startup API unavailable');
      startupSession=wrapper.NodeIQQNTStartupSessionWrapper.create();
      session=wrapper.NodeIQQNTWrapperSession.getNTWrapperSession('nt_1');
      if(!startupSession||typeof startupSession.start!=='function'||typeof session?.init!=='function')throw new Error('startup session unavailable');
      trace('消息会话对象已预创建（startup）');
    }catch{
      startupSession=null;session=wrapper.NodeIQQNTWrapperSession.create();
      if(typeof session?.init!=='function'||typeof session.startNT!=='function')throw new Error('此QQ版本没有可用的原生消息会话接口');
      trace('消息会话对象已预创建（session）');
    }
  }
  function prepareDataRoot(){
    // Never follow a registry/known-folder result outside this private profile.
    let candidate='';
    try{candidate=wrapper.NodeQQNTWrapperUtil?.getNTUserDataInfoConfig?.()||'';}catch{}
    const own=path.resolve(profile),resolved=typeof candidate==='string'&&candidate?path.resolve(candidate):'';
    const relative=resolved?path.relative(own,resolved):'..';
    dataRoot=resolved&&relative!=='..'&&!relative.startsWith('..'+path.sep)&&!path.isAbsolute(relative)?resolved:path.join(own,'sdk');
    const globalPath=path.join(dataRoot,'nt_qq','global');
    fs.mkdirSync(globalPath,{recursive:true});
    trace('SDK资料根目录（相对独立资料目录）：'+(path.relative(own,dataRoot)||'.')+'；登录与会话共用此根目录');
    return globalPath;
  }
  function machineGuid(){
    const raw=String(login.getMachineGuid()||'').replace(/[{}-]/g,'');
    if(!/^[a-f0-9]{32}$/i.test(raw))throw new Error('QQ设备标识格式不受支持，未启动消息会话');
    return raw.slice(0,8)+'-'+raw.slice(8,12)+'-'+raw.slice(12,16)+'-'+raw.slice(16,20)+'-'+raw.slice(20);
  }
  function needsScan(reason){
    clearTimeout(autoLoginTimer);autoLoginPending=false;
    if(closed||locked||sessionStarted)return;
    state.phase='needs_scan';state.stage=reason+'；请在Sol内扫码登录指定机器人账号';state.error='';
    trace('自动恢复登录未完成，等待本人扫码（不自动重试）');
  }
  async function restoreLogin(){
    if(!autoLoginEnabled||autoLoginAttempted||closed||locked||sessionStarted)return;
    autoLoginAttempted=true;autoLoginPending=true;
    state.phase='restoring_login';state.stage='正在恢复指定机器人的已保存登录';state.error='';
    trace('开始一次指定账号的本地登录恢复');
    autoLoginTimer=setTimeout(()=>{
      if(!autoLoginPending)return;
      needsScan('自动登录20秒内未完成');
      try{login.cancel();}catch{}
    },20000);
    try{
      if(typeof login.getLoginList!=='function'||typeof login.quickLoginWithUin!=='function'){
        needsScan('当前QQ版本没有可用的登录恢复接口');return;
      }
      const history=await login.getLoginList();
      if(!autoLoginPending||closed||locked||sessionStarted)return;
      const record=(Array.isArray(history?.LocalLoginInfoList)?history.LocalLoginInfoList:[]).find(item=>String(item.uin)===expected);
      // Never choose the first account, another QQ, or manufacture a credential.
      if(!record||!(record.isQuickLogin||record.isAutoLogin)||typeof record.uid!=='string'||!record.uid.startsWith('u_')){
        needsScan('指定账号没有可恢复的有效登录记录');return;
      }
      const result=await login.quickLoginWithUin(expected);
      if(!autoLoginPending||closed||locked||sessionStarted)return;
      if(String(result?.result)!=='0'||result?.loginErrorInfo?.errMsg){
        const code=errorCode([result?.result]);needsScan('QQ未接受已保存登录'+(code?'，代码 '+code:''));return;
      }
      // Some versions emit the login-success callback, others return success
      // here. UID is from the SDK's stored record for this exact requested UIN.
      trace('QQ已接受指定账号的登录恢复，准备消息会话');
      startSession({uin:expected,uid:record.uid});
    }catch{
      if(autoLoginPending)needsScan('读取或恢复已保存登录失败');
    }
  }
  function rememberMessage(row){
    if(!row?.msgId||![1,2].includes(Number(row.chatType))||!row.peerUid)return null;
    const item={id:String(row.msgId),seq:String(row.msgSeq||'0'),clientSeq:String(row.clientSeq||'0'),time:String(row.msgTime||'0'),
      sender:String(row.senderUin||'0'),senderUid:String(row.senderUid||''),name:String(row.sendMemberName||row.sendNickName||row.senderUin||'未知'),
      text:(row.elements||[]).filter(e=>e.elementType===1).map(e=>e.textElement?.content||'').join(''),
      has_media:(row.elements||[]).some(e=>[2,3].includes(e.elementType)),
      peer:{chatType:Number(row.chatType),peerUid:String(row.peerUid)},target:String(row.peerUin||row.peerUid)};
    messageRefs.set(item.id,item);if(messageRefs.size>1200)messageRefs.delete(messageRefs.keys().next().value);return item;
  }
  function quoteOf(row){
    const reference=(row.elements||[]).find(e=>e.elementType===7)?.replyElement;
    if(!reference)return null;
    const same=item=>item?.peer.chatType===Number(row.chatType)&&item.peer.peerUid===String(row.peerUid);
    let found=messageRefs.get(String(reference.replayMsgId));if(!same(found))found=null;
    if(!found)found=[...messageRefs.values()].find(item=>same(item)&&item.seq===String(reference.replayMsgSeq)&&item.seq!=='0');
    if(!found){
      const record=(row.records||[]).find(item=>String(item.msgId)===String(reference.sourceMsgIdInRecords)&&Number(item.chatType)===Number(row.chatType)&&String(item.peerUid)===String(row.peerUid));
      if(record)found=rememberMessage(record);
    }
    if(found)return {id:found.id,sender:found.sender,name:found.name,text:found.text||(found.has_media?'[原消息包含附件，附件需实际取得后才能读取]':''),has_media:found.has_media,missing:false};
    const id=[reference.replayMsgId,reference.sourceMsgIdInRecords].map(String).find(id=>/^[1-9]\d*$/.test(id))||'';
    return {id,sender:'',name:'未知',text:'引用原消息未取得，不能拿最近消息代替',missing:true};
  }
  function receive(rows){
    if(!admitted())return;
    for(const row of Array.isArray(rows)?rows:rows?.msgList||[]){
      const type=Number(row.chatType),sender=String(row.senderUin||'');
      if(![1,2].includes(type)||!/^\d+$/.test(sender)||sender===expected)continue;
      if(!Number.isFinite(Number(row.msgTime))||Number(row.msgTime)<bootTime)continue; // no offline replay after restart
      const target=String(type===2?row.peerUin:sender),uid=String(row.peerUid||'');
      if(!/^\d+$/.test(target)||!uid||!row.msgId)continue;
      const id=String(row.msgId),eventKey=`${type}:${target}:${id}`;if(seen.has(eventKey))continue;
      rememberMessage(row);const quote=quoteOf(row);
      const elements=(row.elements||[]).filter(e=>e.elementType===1),media=[];
      for(const element of row.elements||[]){
        const kind=element.elementType===2?'image':element.elementType===3?'file':null;
        if(!kind)continue;
        if(media.length>=16){media.push({kind:'file',name:'其余附件',error:'单条消息超过16个附件，剩余附件未自动保存'});break;}
        const source=kind==='image'?element.picElement:element.fileElement;
        const token=crypto.randomUUID(),name=path.basename(String(source?.fileName||(kind==='image'?'图片.jpg':'文件')).replace(/\\/g,'/'));
        const ref={token,kind,name,size:Number(source?.fileSize)||0,msgId:id,chatType:type,peerUid:uid,elementId:String(element.elementId||''),source:kind==='image'?source?.sourcePath:source?.filePath};
        mediaRefs.set(token,ref);if(mediaRefs.size>3200)mediaRefs.delete(mediaRefs.keys().next().value);
        media.push({token,kind,name,size:ref.size});
      }
      const text=elements.map(e=>e.textElement?.content||'').join('');if(!text&&!media.length&&!quote)continue;
      seen.add(eventKey);if(seen.size>1000)seen.delete(seen.values().next().value);
      peers.set((type===2?'group:':'private:')+target,{chatType:type,peerUid:uid});
      if(peers.size>500)peers.delete(peers.keys().next().value);
      const segments=elements.map(e=>{const t=e.textElement||{};
        return t.atType&&[t.atUid,t.atNtUid].map(String).includes(state.uid)?{type:'at',data:{qq:expected}}:{type:'text',data:{text:t.content||''}};
      });
      if(quote&&!quote.missing&&/^\d+$/.test(quote.id))segments.unshift({type:'reply',data:{id:quote.id}});
      events.push({key:eventKey,id,sender,group:type===2?target:null,text,segments,media,quote,name:String(row.sendMemberName||row.sendNickName||sender),created:Number(row.msgTime)||Math.floor(Date.now()/1000)});
      if(events.length>200){events.shift();state.warning='未交付消息超过200条，较旧消息已丢弃；请查看接入连接状态';}
      state.received++;
    }
  }
  async function localMedia(file,maxBytes){
    if(typeof file!=='string'||!file)throw new Error('QQ没有返回已下载的附件路径');
    const root=await fs.promises.realpath(profile),actual=await fs.promises.realpath(file),relative=path.relative(root,actual);
    if(relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative))throw new Error('QQ附件路径不在Sol独立资料目录内，已拒绝读取');
    const stat=await fs.promises.stat(actual);
    if(!stat.isFile()||stat.size===0)throw new Error('QQ返回的附件为空或未下载完成');
    if(stat.size>maxBytes)throw new Error('附件超过Sol当前文件大小限制');
    return {path:actual,size:stat.size};
  }
  function mediaCompleted(info){
    for(const waiter of mediaWaiters.values()){
      if(String(info?.msgId)!==waiter.ref.msgId||String(info?.msgElementId)!==waiter.ref.elementId)continue;
      if(info.chatType!==undefined&&Number(info.chatType)!==waiter.ref.chatType)continue;
      const code=String(info.fileErrCode??'0');
      if(code!=='0')waiter.reject(new Error('QQ附件下载失败，代码 '+code));
      else waiter.resolve(info.filePath);
    }
  }
  function downloadMedia(input){
    if(mediaJobs.has(input.token))return mediaJobs.get(input.token);
    const job=fetchMedia(input);mediaJobs.set(input.token,job);
    const cleanup=()=>mediaJobs.delete(input.token);job.then(cleanup,cleanup);
    return job;
  }
  async function fetchMedia(input){
    if(!admitted())throw new Error('指定机器人尚未连接，无法下载附件');
    const ref=mediaRefs.get(input.token),maxBytes=Number(input.max_bytes);
    if(!ref)throw new Error('附件引用已过期，请重新发送该附件');
    if(!Number.isSafeInteger(maxBytes)||maxBytes<=0)throw new Error('附件大小限制无效');
    if(ref.size>maxBytes)throw new Error('附件超过Sol当前文件大小限制');
    if(ref.source){try{return await localMedia(ref.source,maxBytes);}catch(error){if(error.code!=='ENOENT')throw error;}}
    if(!ref.elementId)throw new Error('QQ没有提供附件元素编号，无法下载原件');
    if(mediaWaiters.size>=2)throw new Error('附件下载队列已满，请稍后重发');
    const folder=path.join(profile,'sdk','incoming');await fs.promises.mkdir(folder,{recursive:true});
    const extension=path.extname(ref.name).slice(0,16).replace(/[^.a-zA-Z0-9]/g,'');
    const destination=path.join(folder,crypto.randomUUID()+extension);
    let resolve,reject;const completion=new Promise((a,b)=>{resolve=a;reject=b;});completion.catch(()=>{});
    const timer=setTimeout(()=>reject(new Error('QQ在45秒内未完成附件下载，可能已过期或不可访问')),45000);
    mediaWaiters.set(ref.token,{ref,resolve,reject});
    try{
      Promise.resolve(msgService.downloadRichMedia({fileModelId:'0',downSourceType:0,downloadSourceType:0,triggerType:1,msgId:ref.msgId,chatType:ref.chatType,peerUid:ref.peerUid,elementId:ref.elementId,thumbSize:0,downloadType:1,filePath:destination})).catch(reject);
      const local=await localMedia(await completion,maxBytes);
      return {...local,temporary:path.resolve(local.path)===path.resolve(destination)};
    }finally{clearTimeout(timer);mediaWaiters.delete(ref.token);}
  }
  function sentUpdates(rows){
    for(const row of Array.isArray(rows)?rows:rows?.msgList||[]){
      const waiter=sentWaiters.get(String(row.guildId));
      if(!waiter||Number(row.chatType)!==waiter.peer.chatType||String(row.peerUid)!==waiter.peer.peerUid)continue;
      if(Number(row.sendStatus)===2){
        rememberMessage({...row,senderUin:row.senderUin||expected,senderUid:row.senderUid||state.uid});
        if(/^\d+$/.test(String(row.msgId)))waiter.resolve({message_id:String(row.msgId)});
        else waiter.reject(new Error('QQ成功回调缺少有效消息编号，发送状态未确认，不自动重发'));
      }
      else if(Number(row.sendStatus)===0)waiter.reject(new Error('QQ返回发送失败，请手动决定是否重发'));
    }
  }
  function startSession(info){
    if(closed||locked)return;
    if(!info||typeof info!=='object'){fail('QQ登录回调异常','QQ没有返回账号信息，未启用消息接入');return;}
    state.actual=String(info.uin||'');
    if(state.actual!==expected){
      locked=true;fail('登录账号不符',`扫码账号 ${state.actual||'未知'} 不是指定机器人 ${expected}，已拒绝消息接入，请停止服务后重新扫码`);
      clearTimeout(autoLoginTimer);autoLoginPending=false;cancelSends(state.error);cancelMedia(state.error);
      rejectQr(state.error);try{login.abortPolling();}catch{}return;
    }
    if(sessionStarted)return;
    clearTimeout(autoLoginTimer);autoLoginPending=false;
    if(typeof info.uid!=='string'||!info.uid.startsWith('u_')){fail('账号标识缺失','QQ登录回调没有提供有效UID，未启用消息收发');return;}
    state.uid=info.uid;state.phase='starting_session';state.stage='扫码已确认，正在初始化消息会话';state.error='';rejectQr('账号已确认，请等待消息会话就绪');
    try {
      const downloads=path.join(profile,'sdk','downloads');
      fs.mkdirSync(downloads,{recursive:true});
      const config={selfUin:expected,selfUid:info.uid,desktopPathConfig:{account_path:dataRoot},clientVer:version,
        a2:'',d2:'',d2Key:'',machineId:'',platform:3,platVer:os.release(),appid,
        rdeliveryConfig:{appKey:'',systemId:0,appId:'',logicEnvironment:'',platform:3,language:'',sdkVersion:'',userId:'',appVersion:'',osVersion:'',bundleId:'',serverUrl:'',fixedAfterHitKeys:['']},
        defaultFileDownloadPath:downloads,deviceInfo:{guid:machineGuid(),buildVer:version,localId:2052,devName:os.hostname(),devType:'Windows',vendorName:'',osVer:os.release(),vendorOsName:'Windows',setMute:false,vendorType:0},
        deviceConfig:'{"appearance":{"isSplitViewMode":true},"msg":{}}'};
      let nativeReady=false,started=false,attaching=false;
      const valid=()=>!closed&&!locked&&connected&&state.phase==='starting_session';
      const ready=async()=>{
        nativeReady=true;
        if(!started||attaching||!valid())return;
        attaching=true;
        try{
          // Native callbacks can arrive within start(). Wait until the startup
          // call has unwound before obtaining services from the created session.
          await new Promise(resolve=>setImmediate(resolve));
          for(let attempt=0;attempt<10&&valid();attempt++){
            const service=session.getMsgService();
            if(service&&typeof service.addKernelMsgListener==='function'){
              service.addKernelMsgListener(listener({onRecvMsg:receive,onRecvActiveMsg:receive,onMsgInfoListUpdate:sentUpdates,onRichMediaDownloadComplete:mediaCompleted,
                onKickedOffLine:()=>offline('QQ已让此登录失效，请由本人重新扫码；不会自动反复登录')}));
              if(!valid())return;
              msgService=service;clearTimeout(sessionTimer);state.online=true;state.phase='ready';
              state.stage='指定QQ消息会话和接收监听已就绪';state.error='';trace('指定账号消息会话与监听已初始化');return;
            }
            await new Promise(resolve=>setTimeout(resolve,200));
          }
          if(valid())fail('QQ消息服务不可用','会话已返回初始化信号，但当前QQ版本未提供消息服务；接入尚未完成，请查看组件日志');
        }catch(error){trace('注册消息监听失败：'+String(error.message).slice(0,300));if(valid())fail('消息监听注册失败','QQ原生消息接口初始化异常，尚不能收发；请查看QQ组件日志');}
      };
      sessionStarted=true;sessionStep='调用session.init';trace('指定账号扫码已确认；'+sessionStep);
      sessionTimer=setTimeout(()=>fail('消息会话初始化超时',`扫码已确认，45秒内未收到消息就绪信号；最后步骤：${sessionStep}；最后回调：${sessionSignal}。请停止服务后重试`),45000);
      session.init(config,listener({onMSFStatusChange:(...args)=>{if(msfSignals++<8)trace('消息网络状态代码：'+(errorCode(args)||'未提供数字代码'));},onMSFSsoError:code=>{trace('消息网络错误代码：'+errorCode([code]));offline('QQ消息网络错误，代码 '+(errorCode([code])||'未知'));}}),listener({}),
        listener({onNTSessionCreate:()=>{sessionSignal='onNTSessionCreate';trace('收到会话创建回调');},onOpentelemetryInit:info=>{sessionSignal='onOpentelemetryInit';if(!valid())return;trace('收到消息初始化回调：'+(info?.is_init===true?'就绪':'未就绪'));if(info?.is_init)ready();else{clearTimeout(sessionTimer);fail('消息会话初始化失败','QQ原生会话报告初始化失败');}},onSessionInitComplete:()=>{sessionSignal='onSessionInitComplete';trace('收到会话初始化完成回调');if(valid())ready();},onUserOnlineResult:()=>{sessionSignal='onUserOnlineResult';trace('收到用户在线结果回调（仍等待消息服务就绪）');}}));
      sessionStep='session.init已返回';trace(sessionStep);
      if(!valid())return;
      sessionStep=startupSession?'调用startupSession.start':'调用session.startNT';trace(sessionStep);
      if(startupSession)startupSession.start();else{try{session.startNT(0);}catch{session.startNT();}}
      sessionStep='消息会话启动调用已返回';trace(sessionStep);
      started=true;
      if(nativeReady)ready();
    }catch(error){clearTimeout(sessionTimer);trace('原生会话初始化异常：'+String(error.message).slice(0,300));fail('消息会话初始化失败','QQ原生会话未能启动，尚不能收发；请查看QQ组件日志');}
  }
  async function initialize(){
    try {
      await app.whenReady();if(closed)return;
      const info=metadata();
      wrapper=require(path.join(info.base,'wrapper.node'));
      const engine=wrapper.NodeIQQNTWrapperEngine.get();login=wrapper.NodeIKernelLoginService.get();
      prepareSession();const globalPath=prepareDataRoot();
      engine.initWithDeskTopConfig({base_path_prefix:'',platform_type:3,app_type:4,app_version:version,os_version:os.release(),use_xlog:false,qua:info.qua,
        global_path_config:{desktopGlobalPath:globalPath},thumb_config:{maxSide:324,minSide:48,longLimit:6,density:2}},listener({}));
      trace('原生引擎初始化已返回');
      login.initConfig({machineId:'',appid,platVer:os.release(),commonPath:globalPath,clientVer:version,hostName:os.hostname(),externalVersion:false});
      trace('原生登录配置已返回');
      login.addKernelLoginListener(listener({
        onLoginConnecting:()=>{state.stage='正在连接QQ登录服务';},
        onLoginConnected:()=>{connected=true;if(!sessionStarted&&!locked&&!autoLoginPending&&state.phase!=='needs_scan'){state.stage='登录服务已连接，请获取二维码';state.phase='waiting_qrcode';state.error='';restoreLogin();}},
        onLoginDisConnected:()=>offline('QQ登录服务连接断开，请停止服务后重新启动'),
        onLogoutSucceed:()=>offline('QQ已退出登录'),
        onQRCodeGetPicture:arg=>{
          if(!qrPending||locked||closed||sessionStarted)return;
          const image=String(arg?.pngBase64QrcodeData||'').replace(/^data:image\/png;base64,/,'');
          if(image.length>2000000||!Buffer.from(image,'base64').subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))){rejectQr('QQ返回的二维码不是有效PNG');return;}
          const pending=qrPending;qrPending=null;clearTimeout(pending.timer);
          state.stage='请用指定机器人账号扫码并确认';state.phase='waiting_scan';state.error='';
          try{login.startPolling();}catch{pending.reject(new Error('QQ无法开始等待扫码，请刷新二维码'));return;}
          pending.resolve({online:false,expected,image,expires_in:120,kind:'native_qrcode'});
        },
        onQRCodeSessionUserScaned:()=>{if(sessionStarted||locked||closed)return;state.phase='waiting_confirmation';state.stage='二维码已扫描，请在手机上确认账号';},
        onQRCodeLoginSucceed:startSession,
        onQRCodeSessionFailed:(...args)=>{if(sessionStarted||locked||closed)return;const code=errorCode(args);state.phase='qrcode_failed';state.stage='二维码已失效，请刷新';state.error='QQ扫码会话失败'+(code?'，代码 '+code:'');rejectQr(state.error);},
        onQRCodeSessionQuickLoginFailed:(...args)=>{if(autoLoginPending)needsScan('QQ已保存登录失效'+(errorCode(args)?'，代码 '+errorCode(args):''));},
        onLoginFailed:(...args)=>{if(autoLoginPending){needsScan('QQ未能恢复登录'+(errorCode(args)?'，代码 '+errorCode(args):''));return;}fail('QQ登录失败','QQ拒绝本次登录'+(errorCode(args)?'，代码 '+errorCode(args):'，请检查手机QQ提示'));rejectQr(state.error);},
        onUserLoggedIn:(uin)=>{if(autoLoginPending&&String(uin)===expected)return;if(!sessionStarted)fail('QQ账号已在其他会话登录','客户端报告已有登录，未接管现有账号，请停止服务后重新扫码');}
      }));
      initialized=true;state.phase='connecting';state.stage='正在连接QQ登录服务';
      if(login.connect()===false)fail('QQ登录服务连接失败','QQ原生登录接口拒绝连接，请查看QQ组件日志');
      trace('Sol原生登录接口已初始化（尚未登录）');
    }catch(error){fail('原生登录接口初始化失败',String(error.message).slice(0,200));}
  }
  async function qrcode(){
    if(admitted())return {online:true,expected};
    if(locked||closed)throw new Error(state.error||'QQ服务已停止');
    if(sessionStarted)throw new Error(state.error||'账号已确认，正在初始化消息会话，请稍候');
    if(qrPending)return qrPending.promise;
    const deadline=Date.now()+12000;
    while(!connected&&Date.now()<deadline&&!closed){
      if(state.phase==='error'||state.phase==='offline')throw new Error(state.error);
      await new Promise(resolve=>setTimeout(resolve,200));
    }
    while(autoLoginPending&&Date.now()<deadline&&!closed){await new Promise(resolve=>setTimeout(resolve,200));}
    if(admitted())return {online:true,expected};
    if(autoLoginPending)throw new Error('正在恢复指定账号的已保存登录，请稍候；失败后会提示扫码');
    if(sessionStarted)throw new Error(state.error||'登录已确认，正在准备消息接入，请稍候');
    if(locked||closed)throw new Error(state.error||'QQ服务已停止');
    if(!initialized||!connected||closed)throw new Error(state.error||'QQ登录服务未在12秒内连接，请稍后手动刷新二维码');
    if(qrPending)return qrPending.promise;
    let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});
    qrPending={resolve,reject,promise,timer:setTimeout(()=>rejectQr('QQ在12秒内没有返回二维码，请手动刷新'),12000)};
    state.phase='generating_qrcode';state.stage='正在获取二维码';state.error='';
    try{if(login.getQRCodePicture()===false)rejectQr('QQ拒绝生成二维码，请稍后手动刷新');}catch(error){rejectQr('获取QQ二维码失败：'+String(error.message).slice(0,160));}
    return promise;
  }
  function within(root,candidate){
    const relative=path.relative(path.resolve(root),path.resolve(candidate));
    return relative!==''&&relative!=='..'&&!relative.startsWith('..'+path.sep)&&!path.isAbsolute(relative);
  }
  async function outboundElement(item,maxBytes){
    if(item.type==='text'){
      if(typeof item.text!=='string'||!item.text||item.text.length>10000)throw new Error('QQ文字消息需要1至10000字符');
      return {elementType:1,elementId:'',textElement:{content:item.text,atType:0,atUid:'',atNtUid:'',atTinyId:''}};
    }
    if(!['image','file'].includes(item.type))throw new Error('QQ附件类型不支持');
    if(!Number.isSafeInteger(maxBytes)||maxBytes<=0)throw new Error('QQ附件大小限制无效');
    if(typeof item.path!=='string'||!path.isAbsolute(item.path))throw new Error('QQ附件缺少绝对路径');
    const source=await fs.promises.realpath(item.path);
    const roots=await Promise.all([path.join(profile,'..','..','files'),path.join(profile,'..','outgoing')].map(root=>fs.promises.realpath(root).catch(()=>null)));
    if(!roots.some(root=>root&&within(root,source)))throw new Error('QQ附件不在Sol文件库或发送暂存目录，拒绝读取');
    const stat=await fs.promises.stat(source);
    if(!stat.isFile()||stat.size<=0||stat.size>maxBytes||stat.size!==item.size)throw new Error('QQ附件为空、大小已变化或超过限制');
    const name=String(item.name||'附件').replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').slice(0,180);
    if(item.type==='file')return {elementType:3,elementId:'',fileElement:{fileName:name,folderId:'',filePath:source,fileSize:String(stat.size)}};
    if(!Number.isSafeInteger(item.width)||!Number.isSafeInteger(item.height)||item.width<=0||item.height<=0||item.width*item.height>40000000||![1000,1001,1002,1005,2000].includes(item.pic_type))throw new Error('QQ图片尺寸或格式无效');
    const hash=crypto.createHash('md5');for await(const chunk of fs.createReadStream(source))hash.update(chunk);
    const md5=hash.digest('hex');if(md5!==item.md5)throw new Error('QQ图片内容已变化，未发送');
    if(typeof msgService.getRichMediaFilePathForGuild!=='function')throw new Error('当前QQ版本缺少图片发送缓存接口');
    const destination=msgService.getRichMediaFilePathForGuild({md5HexStr:md5,fileName:name,elementType:2,elementSubType:0,thumbSize:0,needCreate:true,downloadType:1,file_uuid:''});
    if(typeof destination!=='string'||!path.isAbsolute(destination)||!within(profile,destination))throw new Error('QQ返回的图片缓存目录不属于Sol独立资料目录');
    await fs.promises.mkdir(path.dirname(destination),{recursive:true});
    const parent=await fs.promises.realpath(path.dirname(destination));
    if(!within(profile,parent))throw new Error('QQ图片缓存目录包含外部链接，拒绝写入');
    // Exclusive copy: an existing content-addressed cache must have the same bytes.
    try{await fs.promises.copyFile(source,destination,fs.constants.COPYFILE_EXCL);}
    catch(error){
      if(error.code!=='EEXIST')throw error;
      const cached=await fs.promises.realpath(destination);
      if(!within(profile,cached))throw new Error('QQ图片缓存文件指向外部目录');
      const cachedHash=crypto.createHash('md5');for await(const chunk of fs.createReadStream(cached))cachedHash.update(chunk);
      if(cachedHash.digest('hex')!==md5)throw new Error('QQ图片缓存与原件不一致，未覆盖或发送');
    }
    return {elementType:2,elementId:'',picElement:{md5HexStr:md5,fileName:name,fileSize:String(stat.size),sourcePath:destination,
      picWidth:item.width,picHeight:item.height,picType:item.pic_type,picSubType:0,original:true,fileUuid:'',fileSubId:'',thumbFileSize:0,thumbPath:new Map(),summary:''}};
  }
  async function replyElement(id,peer){
    let source=messageRefs.get(String(id));
    if(source&&(source.peer.chatType!==peer.chatType||source.peer.peerUid!==peer.peerUid))throw new Error('引用消息不属于当前会话，拒绝引用');
    if(!source&&typeof msgService.getMsgsByMsgId==='function'){
      let timer;
      try{
        const data=await Promise.race([msgService.getMsgsByMsgId(peer,[String(id)]),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('读取引用消息超时')),3000);})]);
        const row=data?.msgList?.find(row=>String(row.msgId)===String(id)&&Number(row.chatType)===peer.chatType&&String(row.peerUid)===peer.peerUid);
        if(row)source=rememberMessage(row);
      }catch{trace('未取得原引用消息，本次按普通消息发送，不猜测替代目标');}
      finally{clearTimeout(timer);}
    }
    if(!source||source.seq==='0')return null;
    return {elementType:7,elementId:'',replyElement:{replayMsgSeq:source.seq,replayMsgId:source.id,senderUin:source.sender,
      senderUinStr:source.sender,senderUidStr:source.senderUid,replyMsgClientSeq:source.clientSeq,replyMsgTime:source.time,_replyMsgPeer:peer}};
  }
  async function submitSend(peer,unique,elements,interval,cancelled){
    if(!Array.isArray(interval)||interval.length!==2||interval.some(n=>typeof n!=='number'||!Number.isFinite(n)||n<0)||interval[1]<interval[0])throw new Error('QQ发送间隔无效');
    const previous=paceTail;let release;paceTail=new Promise(resolve=>{release=resolve;});await previous;
    try{
      const gap=(interval[0]+Math.random()*(interval[1]-interval[0]))*1000;
      await new Promise(resolve=>setTimeout(resolve,Math.max(0,lastSubmitted+gap-Date.now())));
      if(!admitted()||cancelled())throw new Error('QQ已断开或发送已取消，未提交旧消息');
      const pending=Promise.resolve(msgService.sendMsg('0',{...peer,guildId:unique},elements,new Map()));
      pending.catch(()=>{});
      lastSubmitted=Date.now();return {pending}; // release pacing without waiting for upload
    }finally{release();}
  }
  async function sendMessage(input,cancelled){
    if(!admitted())throw new Error('指定机器人尚未登录，已拒绝发送');
    const items=Array.isArray(input.elements)?input.elements:[{type:'text',text:input.text}];
    if(!items.length||items.length>32)throw new Error('QQ单条消息需要1至32个内容段');
    const scope=(input.group?'group:':'private:')+String(input.target);
    const permit=input.persistent_peer;
    if(permit&&(permit.expected!==expected||permit.scope!==scope||permit.peer?.chatType!==(input.group?2:1)||typeof permit.peer?.peerUid!=='string'||!permit.peer.peerUid||permit.peer.peerUid.length>128))throw new Error('持久提醒会话授权与当前账号或目标不符');
    // A reminder carries its own previously observed peer. Never insert it in
    // the general reply map or grant access to unrelated historical sessions.
    const peer=peers.get(scope)||(permit?permit.peer:null);
    if(!peer)throw new Error('原型仅可回复本次实际收到过消息的会话，尚未取得该会话标识');
    const elements=[];for(const item of items)elements.push(await outboundElement(item,input.max_bytes));
    const reply=input.reply_to?await replyElement(input.reply_to,peer):null;
    if(reply)elements.unshift(reply);
    if(!admitted())throw new Error('附件准备期间QQ已断开，未提交发送');
    const unique=String(msgService.generateMsgUniqueId(peer.chatType,session.getMSFService().getServerTime()));
    if(!/^\d+$/.test(unique))throw new Error('QQ消息编号接口返回异常，尚未发送');
    let resolve,reject;const ack=new Promise((a,b)=>{resolve=a;reject=b;});ack.catch(()=>{});
    const seconds=items.some(item=>item.type==='file')?120:items.some(item=>item.type==='image')?60:10;
    const detail=seconds>10?'文件已保留，不自动重发或重新生成':'不自动重发';
    let timer;
    sentWaiters.set(unique,{resolve,reject,peer});
    try {
      const {pending}=await submitSend(peer,unique,elements,input.send_interval,cancelled);
      timer=setTimeout(()=>reject(new Error(`QQ在${seconds}秒内未确认发送成功，状态未确认；${detail}`)),seconds*1000);
      const result=await Promise.race([pending,ack]);
      if(result?.result!==undefined&&Number(result.result)!==0)throw new Error('QQ拒绝发送消息，代码 '+String(result.result)+' '+String(result.errMsg||''));
      const sent=await ack;state.sent++;return {...sent,quoted:!!reply};
    }finally{clearTimeout(timer);sentWaiters.delete(unique);}
  }
  async function route(url,input,cancelled){
    if(url==='/status')return {...state,uid:undefined,profile:app.getPath('userData')};
    if(url==='/qrcode')return qrcode();
    if(url==='/window'){
      if(typeof input.visible!=='boolean')throw new Error('窗口显示参数无效');
      diagnosticWindowVisible=input.visible;
      for(const window of BrowserWindow.getAllWindows()){
        if(window.isDestroyed())continue;
        window.setSkipTaskbar(!input.visible);if(input.visible)window.show();else window.hide();
      }
      return {visible:input.visible};
    }
    if(url==='/send')return sendMessage(input,cancelled);
    if(url==='/peer'){
      const scope=String(input.scope||''),peer=peers.get(scope);
      if(!admitted()||!peer)throw new Error('尚未取得本轮实际会话标识，不能保存未来提醒');
      return {expected,scope,peer:{chatType:peer.chatType,peerUid:peer.peerUid}};
    }
    if(url==='/message'){
      const item=messageRefs.get(String(input.id));if(!admitted()||!item)throw new Error('引用消息不在本次会话缓存，原文未取得');
      return {message_id:item.id,time:Number(item.time),self_id:Number(expected),user_id:Number(item.sender),real_id:item.id,
        message_type:item.peer.chatType===2?'group':'private',sub_type:item.peer.chatType===2?'normal':'friend',
        ...(item.peer.chatType===2?{group_id:Number(item.target)}:{}),sender:{user_id:Number(item.sender),nickname:item.name},
        message:[{type:'text',data:{text:item.text}}],raw_message:item.text,font:0};
    }
    if(url==='/media')return downloadMedia(input);
    if(url==='/events')return admitted()?events.slice(0,50):[];
    if(url==='/events/ack'){
      const keys=new Set(Array.isArray(input.keys)?input.keys.slice(0,50):[]);
      for(let i=events.length-1;i>=0;i--)if(keys.has(events[i].key)){for(const item of events[i].media||[])mediaRefs.delete(item.token);events.splice(i,1);}
      return {ok:true};
    }
    throw new Error('原型不支持此操作');
  }
  const server=http.createServer(async(req,res)=>{
    const credential=Buffer.from(typeof req.headers['x-sol-qq']==='string'?req.headers['x-sol-qq']:''),wanted=Buffer.from(key);
    if(credential.length!==wanted.length||!crypto.timingSafeEqual(credential,wanted)){res.writeHead(403);res.end();return;}
    const url=new URL(req.url,'http://127.0.0.1').pathname;
    if((url==='/qrcode'||url==='/send'||url==='/peer'||url==='/media'||url==='/message'||url==='/window'||url==='/events/ack')&&req.method!=='POST'){res.writeHead(405);res.end();return;}
    try{
      let body='';for await(const chunk of req){body+=chunk;if(body.length>64000)throw new Error('请求过大');}
      const result=await route(url,body?JSON.parse(body):{},()=>res.destroyed);
      if(res.destroyed)return;
      res.setHeader('Content-Type','application/json');res.setHeader('Cache-Control','no-store');res.end(JSON.stringify(result));
    }catch(error){if(!res.destroyed){res.writeHead(400,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({error:String(error.message)}));}}
  });
  server.listen(24766,'127.0.0.1',()=>trace('Sol本地IPC监听已建立'));
  server.on('error',error=>fail('Sol本地接口无法启动','本地端口不可用：'+error.code));
  app.on('before-quit',()=>{closed=true;clearTimeout(sessionTimer);clearTimeout(autoLoginTimer);autoLoginPending=false;rejectQr('QQ服务已停止');cancelSends('QQ服务已停止');cancelMedia('QQ服务已停止');server.close();});
  globalThis.__solQqAdapter={state};module.exports=globalThis.__solQqAdapter;
  initialize();
}
