"""Loopback ChatGPT plan adapter with explicitly selected DeepSeek API support."""
from __future__ import annotations

import asyncio
import base64
import copy
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import uuid
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
import uvicorn
from usage_log import UsageLog
from request_metrics import request_metrics, purpose_of
import deepseek_adapter

PORT = int(os.environ.get('QQ_PLAN_PORT', '24662'))
ORIGIN = f'http://127.0.0.1:{PORT}'
CALLBACK = ORIGIN + '/auth/callback'
API = 'https://api.openai.com/v1'
ISSUER = 'https://auth.openai.com'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
APP_NAME = 'Sol' if os.environ.get('COMPANION_MANAGED') == '1' else 'QQ ChatGPT Plan Bridge'
DATA = Path(os.environ.get('QQ_PLAN_DATA_DIR', str(Path(os.environ.get('COMPANION_DATA', str(Path(__file__).resolve().parents[2] / 'state'))) / 'plan')).replace(chr(92) * 2, chr(92))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
usage_log = UsageLog(DATA / 'usage.sqlite3')


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data: bytes, decrypt: bool = False) -> bytes:
    """DPAPI binds secrets to this Windows user; no plaintext credential file."""
    if os.name != 'nt':
        raise RuntimeError('This prototype requires Windows DPAPI.')
    buf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    crypt = ctypes.windll.crypt32
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dest))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(src), ctypes.c_wchar_p('QQ ChatGPT Plan Bridge'), None, None, None, 1, ctypes.byref(dest))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(dest.pbData, dest.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dest.pbData)


def load_protected(name: str, default: dict) -> dict:
    path = DATA / name
    return json.loads(dpapi(path.read_bytes(), True)) if path.exists() else default


def save_protected(name: str, value: dict):
    path = DATA / name
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes(dpapi(json.dumps(value).encode()))
    os.replace(tmp, path)


settings = load_protected('settings.bin', {
    'host_id': 'urn:uuid:' + str(uuid.uuid4()), 'bridge_key': secrets.token_urlsafe(32),
    'selected_model': 'gpt-6-luna', 'paused': False, 'web_search_enabled': True,
})
if not (DATA / 'settings.bin').exists():
    save_protected('settings.bin', settings)
credentials = load_protected('credentials.bin', {'accounts': {}, 'active': ''})
deepseek_config = load_protected('deepseek.bin', {})
def reload_deepseek_config():
    # Configuration may be imported after startup. Refresh without requiring
    # another process restart, and never replace it with an unrelated provider.
    global deepseek_config
    deepseek_config = load_protected('deepseek.bin', {})


transactions: dict[str, dict] = {}
sessions: dict[str, float] = load_protected('dashboard-sessions.bin', {})
refresh_lock = asyncio.Lock()
inference_lock = asyncio.Semaphore(6) if os.environ.get('COMPANION_MANAGED') == '1' else asyncio.Lock()
usage_lock = asyncio.Lock()
last_error = ''
last_test: dict = {}
metadata: dict = {}
model_cache: dict = {'models': [], 'time': 0, 'account': ''}
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class BridgeError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str = ''):
        self.status, self.code, self.message, self.request_id = status, code, message, request_id
        super().__init__(message)


@app.exception_handler(BridgeError)
async def bridge_error(_request, exc):
    global last_error
    last_error = exc.message
    return JSONResponse({'error': {'message': exc.message, 'type': 'chatgpt_plan_error',
        'code': exc.code, 'request_id': exc.request_id}}, status_code=exc.status)


@app.exception_handler(httpx.HTTPError)
async def connection_error(_request, _exc):
    global last_error
    last_error = '连接 OpenAI 暂时失败，请稍后重试。不会切换到收费 API。'
    return JSONResponse({'error': {'message': last_error, 'type': 'connection_error',
        'code': 'connection_failed'}}, status_code=502)


@app.middleware('http')
async def local_only(request: Request, call_next):
    # No wildcard Host/CORS: prevent hostile websites and DNS rebinding using this listener.
    if request.headers.get('host') != f'127.0.0.1:{PORT}':
        return JSONResponse({'error': 'Invalid local host'}, status_code=403)
    if request.client and request.client.host != '127.0.0.1':
        return JSONResponse({'error': 'Loopback only'}, status_code=403)
    origin = request.headers.get('origin')
    if origin and origin != ORIGIN:
        return JSONResponse({'error': 'Cross-origin requests denied'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


def admin(request: Request):
    if sessions.get(request.cookies.get('qq_plan_session'), 0) <= time.time():
        raise HTTPException(401, '本地页面会话已过期，请刷新页面；ChatGPT 授权仍然保留。')


def api_auth(request: Request):
    persisted = load_protected('settings.bin', settings)
    settings.update(persisted)
    expected = 'Bearer ' + settings['bridge_key']
    if not secrets.compare_digest(request.headers.get('authorization', ''), expected):
        raise HTTPException(401, 'Invalid local bridge key.')


async def oidc_metadata() -> dict:
    global metadata
    if not metadata:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(ISSUER + '/.well-known/openid-configuration')
            response.raise_for_status()
            value = response.json()
        if value.get('issuer') != ISSUER:
            raise BridgeError(502, 'invalid_issuer', 'OpenAI 登录服务信息验证失败。')
        for field in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            if not value.get(field, '').startswith(ISSUER + '/'):
                raise BridgeError(502, 'invalid_metadata', 'OpenAI 登录服务地址验证失败。')
        metadata = value
    return metadata


def upstream_error(response: httpx.Response, body: dict | None = None) -> BridgeError:
    if body is None:
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
    error = body.get('error', {})
    if isinstance(error, str):
        error = {'message': error, 'code': error}
    code = error.get('code') or f'upstream_{response.status_code}'
    message = error.get('message') or body.get('detail') or f'OpenAI 返回 HTTP {response.status_code}。'
    if code == 'subscription_sharing_usage_limit_exceeded':
        settings['paused'] = True
        save_protected('settings.bin', settings)
        message = 'ChatGPT 套餐或本应用额度已达上限，已暂停。请在 ChatGPT 使用量设置中查看；不会切换收费 API。'
    return BridgeError(response.status_code, code, str(message), response.headers.get('x-request-id', ''))


def active_account() -> dict:
    account = credentials['accounts'].get(credentials.get('active'))
    if not account or not account.get('access_token'):
        raise BridgeError(401, 'sign_in_required', '请先在本地页面使用 ChatGPT 登录。')
    if 'chatgpt.tokens.use.direct' not in account.get('scopes', []):
        raise BridgeError(403, 'plan_permission_missing', '登录成功，但尚未授权使用 ChatGPT 套餐额度。')
    return account


async def access_token() -> str:
    async with refresh_lock:
        account = active_account()
        if account.get('expires_at', 0) > time.time() + 90:
            return account['access_token']
        if not account.get('refresh_token'):
            raise BridgeError(401, 'sign_in_required', '登录已过期，请重新登录。')
        meta = await oidc_metadata()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(meta['token_endpoint'], data={
                'grant_type': 'refresh_token', 'client_id': account['client_id'],
                'refresh_token': account['refresh_token'], 'resource': API,
            })
        if response.status_code != 200:
            exc = upstream_error(response)
            if exc.code in {'invalid_grant', 'invalid_refresh_token', 'token_expired',
                'refresh_token_expired', 'refresh_token_invalidated', 'refresh_token_reused'}:
                for key in ('access_token', 'refresh_token', 'id_token'):
                    account.pop(key, None)
                save_protected('credentials.bin', credentials)
            raise exc
        value = response.json()
        if not value.get('access_token') or not value.get('refresh_token'):
            raise BridgeError(502, 'invalid_refresh', '刷新结果缺少必要凭证。')
        account.update(access_token=value['access_token'], refresh_token=value['refresh_token'],
            expires_at=time.time() + int(value.get('expires_in', 3600)),
            scopes=value.get('scope', ' '.join(account['scopes'])).split())
        if value.get('id_token'):
            account['id_token'] = value['id_token']
        save_protected('credentials.bin', credentials)
        return account['access_token']


async def models(force=False) -> list[dict]:
    persisted_settings = load_protected('settings.bin', settings)
    if persisted_settings != settings:
        settings.update(persisted_settings)
        model_cache.update(models=[], time=0, account='')
    token = await access_token()
    account_id = credentials['active']
    if not force and model_cache['account'] == account_id and time.time() - model_cache['time'] < 300:
        return model_cache['models']
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(API + '/models', headers={'Authorization': 'Bearer ' + token})
    if response.status_code != 200:
        raise upstream_error(response)
    value = response.json()
    if not isinstance(value.get('models'), list):
        raise BridgeError(502, 'unexpected_catalog', '未收到 ChatGPT 套餐模型列表，停止调用。')
    choices = [m for m in value['models'] if m.get('visibility') == 'list' and m.get('slug')]
    # Account catalog can lag rollout. Only add explicitly selected models after
    # a completed inference using this same account's plan OAuth credentials.
    verified = settings.get('verified_models', {}).get(account_id, {})
    listed = {m['slug'] for m in choices}
    for slug, evidence in verified.items():
        if evidence.get('ok') and evidence.get('model') == slug and slug not in listed:
            choices.insert(0, {'slug': slug, 'display_name': evidence.get('name', slug),
                'visibility': 'list'})
    model_cache.update(models=choices, time=time.time(), account=account_id)
    return choices


UNSUPPORTED = {'background', 'conversation', 'max_output_tokens', 'max_tool_calls',
    'metadata', 'moderation', 'multi_agent', 'prompt', 'prompt_cache_retention',
    'safety_identifier', 'temperature', 'top_logprobs', 'top_p', 'truncation', 'user',
    'previous_response_id', 'service_tier'}
ALLOWED = {'model', 'input', 'instructions', 'reasoning', 'text', 'tools',
    'tool_choice', 'parallel_tool_calls', 'include'}
ALLOWED_TOOLS = {'function', 'custom', 'namespace', 'web_search', 'web_search_preview'}


def normalize_payload(body: dict) -> dict:
    result = copy.deepcopy({k: v for k, v in body.items() if k in ALLOWED and k not in UNSUPPORTED})
    result.update(store=False, stream=True)
    if os.environ.get('COMPANION_MANAGED') != '1' and settings.get('web_search_enabled', True) and purpose_of(body) not in {'image_plan','image_review','context_summary'}:
        tools = result.setdefault('tools', [])
        if not any(t.get('type') in {'web_search', 'web_search_preview'} for t in tools):
            tools.append({'type': 'web_search'})
    if isinstance(result.get('input'), str):
        result['input'] = [{'role': 'user', 'content': result['input']}]
    if not isinstance(result.get('input'), list):
        raise BridgeError(400, 'invalid_input', 'input 必须为消息列表。')
    for item in result['input']:
        if isinstance(item, dict):
            # Response output metadata is not accepted as request input by the
            # ChatGPT plan endpoint. Preserve content/call IDs and reasoning.
            item.pop('status', None)
            if item.get('role') == 'system':
                item['role'] = 'developer'
    def validate_tools(tools):
        for tool in tools:
            kind = tool.get('type')
            if kind not in ALLOWED_TOOLS:
                raise BridgeError(400, 'unsupported_tool', f'套餐接入目前不支持工具：{kind}')
            if kind == 'namespace':
                validate_tools(tool.get('tools', []))
    validate_tools(result.get('tools', []))
    return result


async def infer(body: dict, source: str = 'bot_bridge', verify_candidate: bool = False, identity=None) -> dict:
    # Keep check, upstream call and accounting together: queued requests must see
    # the preceding completed request's actual usage before passing the quota.
    if os.environ.get('COMPANION_MANAGED') == '1':
        return await _accounted_infer(body,source,verify_candidate,identity)
    async with usage_lock:
        return await _accounted_infer(body,source,verify_candidate,identity)


async def _accounted_infer(body,source,verify_candidate,identity):
    reason=None if os.environ.get('COMPANION_MANAGED') == '1' else await asyncio.to_thread(usage_log.quota_error,identity)
    if reason:
        # 403 avoids SDK retries for an intentional per-user quota denial.
        raise BridgeError(403,'qq_daily_limit',reason)
    account='deepseek' if body.get('model') in deepseek_adapter.MODELS else hashlib.sha256(credentials.get('active','').encode()).hexdigest()[:16]
    metrics=await asyncio.to_thread(request_metrics,body)
    identifier=await asyncio.to_thread(usage_log.begin,body.get('model',''),source,account,metrics,identity)
    try:
        result=await _infer(body, verify_candidate=verify_candidate)
    except BaseException as exc:
        code=exc.code if isinstance(exc,BridgeError) else type(exc).__name__
        await asyncio.to_thread(usage_log.finish,identifier,None,str(code)[:100])
        raise
    await asyncio.to_thread(usage_log.finish,identifier,result)
    return result


async def _infer(body: dict, verify_candidate: bool = False) -> dict:
    if str(body.get('model','')).startswith(('deepseek','gemini')) or body.get('model')=='bridge-active':
        raise BridgeError(400,'gpt_only','此桥仅接入 GPT 套餐；请在 AstrBot 模型插件中选择 DeepSeek 或 Gemini。')
    if settings.get('paused'):
        raise BridgeError(429, 'plan_paused', '额度调用已暂停，请先查看 ChatGPT 使用量并在本地页面恢复。')
    payload = normalize_payload(body)
    choices = await models()
    if payload.get('model') not in {m['slug'] for m in choices} and not (verify_candidate and payload.get('model')=='gpt-6.1-sol'):
        raise BridgeError(400, 'model_unavailable', '该模型不在当前 ChatGPT 套餐模型列表中。')
    token = await access_token()
    async with inference_lock:
        if settings.get('paused'):
            raise BridgeError(429, 'plan_paused', '套餐调用已暂停。')
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
            async with client.stream('POST', API + '/responses',
                headers={'Authorization': 'Bearer ' + token}, json=payload) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise upstream_error(response)
                response.encoding = 'utf-8'
                event_lines = []
                completed_items = {}
                async for line in response.aiter_lines():
                    if line.startswith('data:'):
                        event_lines.append(line[5:].lstrip())
                    elif not line and event_lines:
                        raw = '\n'.join(event_lines)
                        event_lines = []
                        if raw == '[DONE]':
                            continue
                        event = json.loads(raw)
                        kind = event.get('type')
                        if kind == 'response.output_item.done':
                            completed_items[event['output_index']] = event['item']
                        if kind == 'response.completed':
                            result = event['response']
                            # Some plan streams omit output in the terminal envelope.
                            # Use completed item events, but only after response.completed.
                            if not result.get('output') and completed_items:
                                result['output'] = [completed_items[k] for k in sorted(completed_items)]
                            return result
                        if kind in {'response.failed', 'response.incomplete', 'error'}:
                            value = event.get('response', event)
                            error = value.get('error') or event.get('error') or {
                                'code': kind, 'message': '回复未完整完成，未作为成功回复返回。'}
                            status = 429 if error.get('code') == 'subscription_sharing_usage_limit_exceeded' else 502
                            exc = upstream_error(response, {'error': error})
                            exc.status = status
                            raise exc
    raise BridgeError(502, 'stream_interrupted', '连接在回复完成前中断，请稍后重试。')


@app.get('/', response_class=HTMLResponse)
async def dashboard(request: Request):
    cookie = request.cookies.get('qq_plan_session')
    if sessions.get(cookie, 0) <= time.time():
        cookie = secrets.token_urlsafe(32)
    sessions.update({cookie: time.time() + 86400})
    expired = [key for key, expiry in sessions.items() if expiry <= time.time()]
    for key in expired:
        sessions.pop(key)
    save_protected('dashboard-sessions.bin', sessions)
    response = HTMLResponse((Path(__file__).parent / 'dashboard.html').read_text(encoding='utf-8'))
    response.set_cookie('qq_plan_session', cookie, httponly=True, samesite='lax', max_age=86400)
    return response


@app.get('/health')
async def health():
    settings.update(load_protected('settings.bin', settings))
    reload_deepseek_config()
    return {'ok': True, 'billing':'chatgpt_plan_only','selection_owner':'astrbot', 'paid_fallback': False, 'build': '20261023-astrbot-model-owner', 'pid': os.getpid(), 'config_files': {n: {'exists': (DATA/n).exists(), 'size': (DATA/n).stat().st_size if (DATA/n).exists() else 0} for n in ('settings.bin','deepseek.bin')}, 'bot_model': active_bot_model(), 'deepseek_configured': bool(deepseek_config.get('key')), 'data_dir': str(DATA), 'key_fingerprint': hashlib.sha256(settings['bridge_key'].encode()).hexdigest()[:12]}


@app.get('/admin/status')
async def status(request: Request):
    admin(request)
    account = credentials['accounts'].get(credentials.get('active'), {})
    persisted = load_protected('settings.bin', settings)
    settings.update(persisted)
    reload_deepseek_config()
    return {'signed_in': bool(account.get('access_token')), 'email': account.get('email', ''),
        'plan_enabled': 'chatgpt.tokens.use.direct' in account.get('scopes', []),
        'paused': settings.get('paused', False), 'model': settings.get('selected_model', ''),
        'model_switch_supported':False,'selection_owner':'astrbot', 'bot_model':'',
        'bot_reasoning_effort': bot_reasoning_effort(active_bot_model() or settings.get('selected_model', '')),
        'reasoning_options': reasoning_options(active_bot_model() or settings.get('selected_model','')),
        'web_search_enabled': settings.get('web_search_enabled', True) and active_bot_model()!='deepseek-flash',
        'deepseek_configured': bool(deepseek_config.get('key')),
        'deepseek_models':[],
        'external_search_enabled':False,
        'provider': 'deepseek' if active_bot_model() in deepseek_adapter.MODELS else 'chatgpt_plan',
        'last_error': last_error, 'last_test': persisted.get('verified_test', {}) if
            persisted.get('verified_account') == credentials.get('active') else {},
        'accounts': [{'id': key, 'email': val.get('email', ''), 'label': val.get('label', key[:12])}
            for key, val in credentials['accounts'].items()], 'active': credentials.get('active', '')}


@app.get('/admin/usage')
async def usage(request: Request, window: str = '5h'):
    admin(request)
    if window not in ('5h','today','all'):
        raise HTTPException(400,'统计范围无效。')
    return await asyncio.to_thread(usage_log.report,window)


@app.get('/v1/qq-usage')
async def qq_usage(request: Request,window: str='today'):
    api_auth(request)
    if window not in ('today','5h','all'): raise HTTPException(400,'统计范围无效。')
    return await asyncio.to_thread(usage_log.user_report,window)


@app.post('/v1/qq-usage/policy')
async def qq_usage_policy(request: Request):
    api_auth(request)
    body=await request.json()
    if not isinstance(body,dict): raise HTTPException(400,'设置格式无效。')
    try: await asyncio.to_thread(usage_log.save_policy,body)
    except ValueError as exc: raise HTTPException(400,str(exc)) from exc
    return {'saved':True}


@app.post('/v1/qq-usage/reset')
async def qq_usage_reset(request: Request):
    api_auth(request)
    body = await request.json()
    try:
        # Wait for the current model request to finish before resetting its quota.
        try:
            await asyncio.wait_for(usage_lock.acquire(), timeout=10)
        except TimeoutError as exc:
            raise HTTPException(409, '模型请求仍在处理中，请完成后再重置额度。') from exc
        try:
            return await asyncio.to_thread(usage_log.reset_quota, body)
        finally:
            usage_lock.release()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get('/auth/start')
async def auth_start(request: Request, new: bool = False):
    admin(request)
    meta = await oidc_metadata()
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    account = None if new else credentials['accounts'].get(credentials.get('active'))
    client_id = account['client_id'] if account else 'dynamic_agent_client'
    transactions[state] = {'nonce': nonce, 'verifier': verifier, 'client_id': client_id,
        'account': credentials.get('active') if account else None,
        'session': request.cookies['qq_plan_session'], 'expires': time.time() + 600}
    # Expire abandoned attempts without storing codes or tokens on disk.
    for key in list(transactions):
        if transactions[key]['expires'] < time.time():
            transactions.pop(key, None)
    args = {'client_id': client_id, 'ext_agent_host_id': settings['host_id'],
        'response_type': 'code', 'redirect_uri': CALLBACK, 'resource': API,
        'scope': SCOPES, 'state': state, 'nonce': nonce, 'code_challenge_method': 'S256',
        'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
    if not account:
        args['agent_name_hint'] = APP_NAME
    elif account.get('id_token'):
        args['id_token_hint'] = account['id_token']
    return RedirectResponse(meta['authorization_endpoint'] + '?' + urlencode(args), 302)


@app.get('/companion/start')
async def companion_start():
    if os.environ.get('COMPANION_MANAGED') != '1':
        raise HTTPException(404)
    cookie = secrets.token_urlsafe(32)
    sessions[cookie] = time.time()+86400
    response = RedirectResponse('/auth/start', 303)
    # This cookie must be set in the actual browser that receives the OAuth callback.
    response.set_cookie('qq_plan_session', cookie, httponly=True, samesite='lax')
    return response


@app.get('/companion/complete', response_class=HTMLResponse)
async def companion_complete(request: Request):
    admin(request)
    import html
    message = last_error or 'GPT 套餐账号已连接，可以关闭此窗口，回到 Sol 选择模型。'
    return '<!doctype html><meta charset="utf-8"><style>body{background:#212121;color:#ddd;font:16px/1.8 system-ui;padding:48px}button{padding:12px 20px}</style><p>'+html.escape(message)+'</p><button onclick="window.close()">关闭登录窗口</button>'


@app.get('/auth/callback')
async def auth_callback(request: Request):
    global last_error
    query = request.query_params
    transaction = transactions.pop(query.get('state', ''), None)
    if not transaction or transaction['expires'] < time.time() or not secrets.compare_digest(
        transaction['session'], request.cookies.get('qq_plan_session', '')):
        raise HTTPException(400, '登录请求已过期或验证失败，请回到本地页面重新开始。')
    if query.get('error'):
        last_error = '官方登录未完成：' + query['error']
        return RedirectResponse('/companion/complete' if os.environ.get('COMPANION_MANAGED') == '1' else '/', 303)
    client_id = query.get('client_id') or transaction['client_id']
    if client_id == 'dynamic_agent_client' or (
        transaction['client_id'] != 'dynamic_agent_client' and client_id != transaction['client_id']):
        raise HTTPException(400, '登录返回的应用编号验证失败。')
    if not query.get('code'):
        raise HTTPException(400, '登录未返回授权码。')
    meta = await oidc_metadata()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(meta['token_endpoint'], data={
                'grant_type': 'authorization_code', 'client_id': client_id, 'code': query['code'],
                'code_verifier': transaction['verifier'], 'redirect_uri': CALLBACK, 'resource': API})
            if response.status_code != 200:
                raise upstream_error(response)
            value = response.json()
            keys = (await client.get(meta['jwks_uri'])).json()['keys']
        header = jwt.get_unverified_header(value['id_token'])
        if header.get('alg') not in {'RS256', 'ES256'}:
            raise ValueError('Unexpected signature algorithm')
        key_data = next(k for k in keys if k.get('kid') == header.get('kid'))
        key = jwt.PyJWK.from_dict(key_data).key
        claims = jwt.decode(value['id_token'], key, algorithms=[header['alg']],
            audience=client_id, issuer=meta['issuer'], options={'require': ['exp', 'iss', 'aud', 'sub', 'nonce']})
        if not secrets.compare_digest(claims['nonce'], transaction['nonce']):
            raise ValueError('Nonce mismatch')
        if transaction['account']:
            existing = credentials['accounts'][transaction['account']]
            if existing['subject'] != claims['sub']:
                raise ValueError('Returning account identity mismatch')
        identity = hashlib.sha256((client_id + ':' + claims['sub']).encode()).hexdigest()
        old = credentials['accounts'].get(identity, {})
        account = {'client_id': client_id, 'subject': claims['sub'], 'issuer': claims['iss'],
            'email': claims.get('email', ''), 'label': claims.get('email', 'ChatGPT') + ' / ' + client_id[-8:],
            'access_token': value['access_token'], 'refresh_token': value.get('refresh_token'),
            'id_token': value['id_token'], 'scopes': value.get('scope', '').split(),
            'expires_at': time.time() + int(value.get('expires_in', 3600)), 'welcome_seen': old.get('welcome_seen', False)}
        credentials['accounts'][identity] = account
        credentials['active'] = identity
        save_protected('credentials.bin', credentials)
        settings.update(paused=False)
        settings.setdefault('selected_model', 'gpt-6-luna')
        save_protected('settings.bin', settings)
        last_error = '' if 'chatgpt.tokens.use.direct' in account['scopes'] else '登录成功，但未获准使用套餐额度。'
    except BridgeError as exc:
        last_error = exc.message
    except Exception:
        # Never log the authorization code, token response, or JWT.
        last_error = '登录凭证验证失败，未保存本次登录。请重新登录。'
    return RedirectResponse('/companion/complete' if os.environ.get('COMPANION_MANAGED') == '1' else '/', 303)


@app.post('/admin/models')
async def admin_models(request: Request):
    admin(request)
    return {'models':[{'slug':m['slug'],'name':m.get('display_name',m['slug'])} for m in await models(True)]}


def active_bot_model():
    return ''  # Legacy persisted selections are inert; AstrBot owns selection.


def reasoning_options(model):
    return [{'value':k,'label':v} for k,v in (
        [('none','关闭思考'),('low','低'),('high','高'),('max','最高')] if model in deepseek_adapter.MODELS else
        [('low','低'),('medium','中'),('high','高')])]


def bot_reasoning_effort(model):
    account=credentials.get('active') or ('deepseek' if model in deepseek_adapter.MODELS else '')
    saved = settings.get('bot_reasoning_by_account', {}).get(account, {})
    effort = saved.get(model) if isinstance(saved, dict) else None
    if model in deepseek_adapter.MODELS and effort=='medium':return 'high'
    if effort in {item['value'] for item in reasoning_options(model)}:
        return effort
    return {'gpt-6-luna': 'high', 'gpt-6.1-sol': 'medium','deepseek-flash':'low','deepseek-v4-pro':'high'}.get(model, '')


@app.post('/admin/reasoning')
async def apply_reasoning(request: Request):
    admin(request)
    raise HTTPException(410,'模型与思考强度已迁移到 AstrBot 的插件页面，请在那里修改。')


@app.post('/admin/model')
async def apply_model(request: Request):
    admin(request)
    raise HTTPException(410,'模型与思考强度已迁移到 AstrBot 的插件页面，请在那里修改。')


@app.post('/admin/test')
async def admin_test(request: Request):
    global last_test, last_error
    admin(request)
    body = await request.json()
    selected = body.get('model') or settings.get('selected_model') or 'gpt-6-luna'
    result = await infer({'model': selected, 'input': [{'role': 'user', 'content': '请只回复：你好，连接成功。'}]}, source='dashboard_test', verify_candidate=True)
    text = '\n'.join(c.get('text', '') for item in result.get('output', [])
        if item.get('type') == 'message' for c in item.get('content', []) if c.get('type') == 'output_text')
    if not text:
        raise BridgeError(502, 'no_text', '模型已完成请求，但没有返回文字，尚未通过聊天测试。')
    last_test = {'ok': True, 'model': selected, 'text': text, 'time': int(time.time()), 'response_id': result.get('id')}
    if selected in deepseek_adapter.MODELS:
        last_error=''
        return last_test
    settings['verified_account'] = credentials['active']
    settings['verified_test'] = last_test
    settings.setdefault('verified_models', {}).setdefault(credentials['active'], {})[selected] = {
        'ok': True, 'model': selected, 'name': 'GPT-6.1 Sol' if selected=='gpt-6.1-sol' else selected,
        'time': int(time.time()), 'response_id': result.get('id')}
    model_cache.update(models=[],time=0,account='')
    save_protected('settings.bin', settings)
    last_error = ''
    return last_test


@app.post('/admin/search')
async def configure_search(request: Request):
    admin(request)
    body = await request.json()
    enabled = body.get('enabled')
    if not isinstance(enabled, bool):
        raise HTTPException(400, 'enabled must be a boolean')
    settings.update(load_protected('settings.bin', settings))
    settings['web_search_enabled'] = enabled
    save_protected('settings.bin', settings)
    return {'web_search_enabled': enabled}


@app.post('/admin/web-search')
async def external_web_search(request: Request):
    admin(request)
    raise HTTPException(410,'独立搜索已迁移到 AstrBot 插件。')


@app.post('/admin/repair-local-key')
async def repair_local_key(request: Request):
    admin(request)
    body = await request.json()
    key = body.get('key', '')
    if not isinstance(key, str) or len(key) < 24 or len(key) > 256:
        raise HTTPException(400, 'Invalid local key format')
    settings['bridge_key'] = key
    save_protected('settings.bin', settings)
    return {'ok': True}


@app.post('/admin/resume')
async def resume(request: Request):
    admin(request)
    settings['paused'] = False
    save_protected('settings.bin', settings)
    return {'ok': True}


@app.post('/admin/account')
async def switch_account(request: Request):
    admin(request)
    identity = (await request.json()).get('id')
    if identity not in credentials['accounts']:
        raise HTTPException(400, 'Account not found')
    credentials['active'] = identity
    save_protected('credentials.bin', credentials)
    settings.update(paused=False)
    if settings.get('bot_model') not in deepseek_adapter.MODELS:
        settings['selected_model'] = 'gpt-6-luna'
    save_protected('settings.bin', settings)
    return {'ok': True}


@app.post('/admin/disconnect')
async def disconnect(request: Request):
    admin(request)
    account = credentials['accounts'].get(credentials.get('active'))
    revoked = False
    if account and account.get('refresh_token'):
        meta = await oidc_metadata()
        endpoint = meta.get('revocation_endpoint', '')
        if endpoint.startswith(ISSUER + '/'):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(endpoint, data={'token': account['refresh_token'],
                        'token_type_hint': 'refresh_token', 'client_id': account['client_id']})
                    revoked = response.status_code == 200
            except httpx.HTTPError:
                pass
    if account:
        for key in ('access_token', 'refresh_token', 'id_token'):
            account.pop(key, None)
        save_protected('credentials.bin', credentials)
    return {'ok': True, 'remote_revoked': revoked,
        'message': '已断开。' if revoked else '本地凭证已清除；远程撤销未确认，可在 ChatGPT 设置中断开此应用。'}


@app.get('/v1/models')
async def api_models(request: Request):
    api_auth(request)
    return {'object': 'list', 'data': [{'id': m['slug'], 'name': m.get('display_name', m['slug']),
            'object': 'model', 'owned_by': 'chatgpt-plan'} for m in await models()]}


@app.post('/v1/audio/transcriptions')
async def api_transcriptions(request: Request):
    api_auth(request)
    from local_speech import MAX_BYTES, transcribe
    if int(request.headers.get('content-length', '0')) > MAX_BYTES + 1024 * 1024:
        raise HTTPException(413, '语音文件超过 20 MB。')
    async with request.form(max_files=1, max_fields=8, max_part_size=MAX_BYTES) as form:
        upload = form.get('file')
        if upload is None or not hasattr(upload, 'read'):
            raise HTTPException(400, 'Missing audio file.')
        data = await upload.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, '语音文件超过 20 MB。')
        try:
            result = await asyncio.to_thread(transcribe, data)
        except Exception as exc:
            raise HTTPException(400, '本地语音识别失败：' + str(exc)[:200]) from exc
    return {'text': result}


@app.post('/v1/responses')
async def api_responses(request: Request):
    api_auth(request)
    body = await request.json()
    # Buffered completion is intentional: don't deliver an incomplete reply as success.
    source='development_test' if request.headers.get('x-qq-usage-source')=='development_test' else 'bot_bridge'
    # AstrBot is authoritative: never override caller model or reasoning.
    settings.update(load_protected('settings.bin', settings))
    user=request.headers.get('x-qq-user','')
    identity=None
    if source!='development_test' and user:
        if not user.isascii() or not user.isdigit() or not 5 <= len(user) <= 20:
            raise HTTPException(400,'QQ 用户标识无效。')
        identity={'qq_user':user,'is_admin':request.headers.get('x-qq-admin')=='1'}
    result = await infer(body,source=source,identity=identity)
    if body.get('stream'):
        async def events():
            yield 'event: response.completed\ndata: ' + json.dumps({'type': 'response.completed', 'response': result}) + '\n\n'
        return StreamingResponse(events(), media_type='text/event-stream')
    return result


if __name__ == '__main__':
    if os.environ.get('COMPANION_MANAGED') == '1':
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
        (DATA / 'startup.json').write_text(json.dumps({'pid': os.getpid(), 'stage': '启动本机登录监听器', 'time': time.time()}, ensure_ascii=False), 'utf-8')
    print('QQ ChatGPT Plan Bridge: ' + ORIGIN, flush=True)
    # Disable access logs so OAuth callback query strings never enter logs.
    uvicorn.run(app, host='127.0.0.1', port=PORT, access_log=False, log_level='warning')
