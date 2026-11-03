"""Payload shape only: never persist message text, images, keys, or URLs."""
def purpose_of(body):
    instruction=body.get('instructions','')
    if not isinstance(instruction,str):instruction=''
    if instruction.startswith('你是图片编辑规划器。'):return 'image_plan'
    if instruction.startswith('你是严格的图片编辑检查员。'):return 'image_review'
    if '[QQ_CONTEXT_SUMMARY]' in instruction:return 'context_summary'
    messages=body.get('input',[])
    if isinstance(messages,list) and messages and isinstance(messages[-1],dict):
        content=messages[-1].get('content','')
        texts=[content] if isinstance(content,str) else [p.get('text','') for p in content if isinstance(p,dict)] if isinstance(content,list) else []
        if any(t.startswith('Generate a summary of our previous conversation history.') and '[QQ_CONTEXT_SUMMARY]' in t for t in texts):return 'context_summary'
    return 'chat_or_tool'

CATEGORIES = ('system', 'history', 'current_message', 'tool_definitions', 'tool_results', 'tool_calls', 'other')


def _encoder():
    # Assets are provisioned once. Missing assets must not download during a chat.
    import os, hashlib
    from pathlib import Path
    cache=Path(os.environ.setdefault('TIKTOKEN_CACHE_DIR',str(Path.home()/'AppData/Local/QQChatGPTPlanBridge/tokenizer-cache')))
    asset=cache/hashlib.sha1(b'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken').hexdigest()
    if not asset.is_file(): return None
    try:
        import tiktoken
        return tiktoken.get_encoding('o200k_base')
    except Exception:
        return None


def request_metrics(body):
    import json, math
    encoder=_encoder()
    categories={name:{'estimated_tokens':0,'chars':0} for name in CATEGORIES}
    media={'images':0,'audio':0,'files':0,'opaque_items':0}
    def add(category, text):
        if not isinstance(text,str) or not text: return
        categories[category]['chars']+=len(text)
        # A rough fallback is explicitly labelled; it never changes quota counters.
        tokens=len(encoder.encode(text,disallowed_special=())) if encoder else math.ceil(len(text.encode('utf-8'))/4)
        categories[category]['estimated_tokens']+=tokens
    def walk(value, category):
        if isinstance(value,str): add(category,value)
        elif isinstance(value,list):
            for part in value: walk(part,category)
        elif isinstance(value,dict):
            kind=value.get('type','')
            if kind in {'input_image','image_url','image'}:
                media['images']+=1;return
            if kind in {'input_audio','audio','audio_url'}:
                media['audio']+=1;return
            if kind in {'input_file','file'}:
                media['files']+=1;return
            if kind in {'item_reference','reasoning'}:
                media['opaque_items']+=1
            for key,item in value.items():
                if key in {'content','text','output','arguments','summary'}: walk(item,category)
                elif key in {'encrypted_content','file_data'}: media['opaque_items']+=1
    add('system',body.get('instructions'))
    items=body.get('input',[])
    if isinstance(items,str): add('current_message',items);items=[]
    if not isinstance(items,list): items=[]
    last_user=max((i for i,m in enumerate(items) if isinstance(m,dict) and m.get('role')=='user'),default=-1)
    for i,item in enumerate(items):
        if not isinstance(item,dict): walk(item,'other');continue
        role=item.get('role');kind=item.get('type','')
        if role in {'system','developer'}: category='system'
        elif role in {'tool','function'} or kind in {'function_call_output','custom_tool_call_output'}: category='tool_results'
        elif kind in {'function_call','custom_tool_call'}: category='tool_calls'
        elif role=='user' and i==last_user: category='current_message'
        elif role in {'user','assistant'}: category='history'
        else: category='other'
        walk(item,category)
        if kind in {'function_call','custom_tool_call'}: add('tool_calls',item.get('name'))
        for call in item.get('tool_calls',[]) or []:
            add('tool_calls',json.dumps(call,ensure_ascii=False,separators=(',',':')))
    tools=body.get('tools') or []
    if tools: add('tool_definitions',json.dumps(tools,ensure_ascii=False,separators=(',',':')))
    for key in ('text','response_format'):
        if body.get(key): add('other',json.dumps(body[key],ensure_ascii=False,separators=(',',':')))
    if body.get('previous_response_id') or body.get('conversation'):media['opaque_items']+=1
    breakdown={'version':1,'method':'o200k_base_reference' if encoder else 'utf8_bytes_div4_fallback',
        'scope':'bridge_received_request','categories':categories,'media':media,
        'estimated_text_tokens':sum(c['estimated_tokens'] for c in categories.values())}
    return {'purpose':purpose_of(body),'input_chars':sum(c['chars'] for c in categories.values()),
        'image_count':media['images'],'message_count':len(body['input']) if isinstance(body.get('input'),list) else 1,
        'tool_count':len(tools),'input_breakdown':breakdown}
