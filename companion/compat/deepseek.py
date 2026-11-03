"""Recover DeepSeek text-encoded tool calls before AstrBot displays them.

Only the tools supplied on this request can be recovered. No extra model call.
"""
import json
import html
import re
import uuid
import xml.etree.ElementTree as ET

from jsonschema import validate
from openai.types.chat import ChatCompletion

TAG = re.compile(r'<\s*(/?)\s*[|｜]+\s*DSML\s*[|｜]+\s*([a-z_]+)([^<>]*)>', re.I)
HAS_MARKER = re.compile(r'<\s*/?\s*[|｜]+\s*DSML', re.I)
INVOKE = re.compile(r'<invoke\b[^>]*>.*?</invoke\s*>', re.S)
FAILURE = '工具调用格式异常，这次调用未执行。请重新发送这条请求。'


def remove_wire_text(text):
    """Remove control syntax from old assistant history, never execute it."""
    if not isinstance(text, str) or not HAS_MARKER.search(text):
        return text
    # Keep the natural-language prefix only; unfinished directives are unsafe
    # context as well, and must not train subsequent turns to imitate them.
    return text[:HAS_MARKER.search(text).start()].rstrip()


def clean_history(payload):
    for message in payload.get('messages', []):
        if isinstance(message, dict) and message.get('role') == 'assistant':
            text = message.get('content')
            if isinstance(text, str) and HAS_MARKER.search(text):
                message['content'] = remove_wire_text(text) or (' ' if message.get('tool_calls') else '此前一次工具调用格式异常，未执行。')


def recover_completion(completion, tools):
    if not completion.choices:
        return completion
    message = completion.choices[0].message
    text = message.content
    if not isinstance(text, str) or not HAS_MARKER.search(text):
        return completion
    data = completion.model_dump()
    row = data['choices'][0]
    output = row['message']
    if message.tool_calls:
        # An API may return native calls and their textual rendering together.
        # Keep the native calls and never execute the rendering a second time.
        output['content'] = remove_wire_text(text) or None
        return ChatCompletion.model_validate(data)
    try:
        if '```' in text:
            raise ValueError('Quoted control syntax is not a callable response')
        if tools is None:
            raise ValueError('No tools were supplied')
        schemas = {item['function']['name']: item['function'].get('parameters', {})
                   for item in tools.get_func_desc_openai_style() if item.get('type') == 'function'}
        normalized = TAG.sub(lambda m: '<' + m[1] + m[2].lower() + m[3] + '>', text)
        first = TAG.search(text)
        # Allow a missing calls opener, but require complete invoke/parameter
        # pairs. A trailing calls closer sometimes accompanies that variant.
        control = normalized[first.start():]
        spans = list(INVOKE.finditer(control))
        if not spans or len(spans) > 32:
            raise ValueError('Missing or excessive invocations')
        remainder = INVOKE.sub('', control)
        remainder = re.sub(r'</?(?:calls|tool_calls)\s*>', '', remainder).strip()
        if remainder:
            raise ValueError('Malformed or mixed control body')
        calls = []
        for span in spans:
            xml = re.sub(r'(<parameter\b[^>]*>)(.*?)(</parameter\s*>)',
                         lambda m: m[1] + html.escape(m[2], quote=False) + m[3], span.group(), flags=re.S)
            node = ET.fromstring(xml)
            if set(node.attrib) != {'name'} or node.attrib['name'] not in schemas:
                raise ValueError('Tool not supplied on this request')
            if (node.text or '').strip():
                raise ValueError('Unexpected invoke body')
            args = {}
            for parameter in node:
                if parameter.tag != 'parameter' or set(parameter.attrib) != {'name', 'string'} or len(parameter):
                    raise ValueError('Malformed parameter')
                name = parameter.attrib['name']
                if name in args or (parameter.tail or '').strip():
                    raise ValueError('Duplicate parameter or extra body')
                value = parameter.text or ''
                flag = parameter.attrib['string']
                if flag not in ('true', 'false'):
                    raise ValueError('Invalid parameter type')
                args[name] = value if flag == 'true' else json.loads(value)
            validate(args, schemas[node.attrib['name']])
            calls.append({'id': 'call_dsml_' + uuid.uuid4().hex[:20], 'type': 'function',
                          'function': {'name': node.attrib['name'], 'arguments': json.dumps(args, ensure_ascii=False)}})
        output['content'] = text[:first.start()].strip() or None
        output['tool_calls'] = calls
        row['finish_reason'] = 'tool_calls'
    except Exception:
        # Do not issue a paid retry, guess arguments, or expose raw directives.
        output['content'] = FAILURE
        output['tool_calls'] = None
        row['finish_reason'] = 'stop'
    return ChatCompletion.model_validate(data)


def install(provider):
    originals = []
    parse = getattr(provider, '_parse_openai_completion', None)
    if parse is not None:
        async def parsed(completion, tools):
            return await parse(recover_completion(completion, tools), tools)
        provider._parse_openai_completion = parsed
        originals.append(('_parse_openai_completion', parse, parsed))
    stream = getattr(provider, '_query_stream', None)
    if stream is not None:
        async def streamed(*args, **kwargs):
            # With tools enabled, even '<' and 'DSML' can arrive in different
            # chunks. Hold partial output until the parsed final response.
            tools = args[1] if len(args) > 1 else kwargs.get('tools')
            has_tools = tools is not None and bool(tools.get_func_desc_openai_style())
            async for response in stream(*args, **kwargs):
                if not has_tools or not response.is_chunk:
                    yield response
        provider._query_stream = streamed
        originals.append(('_query_stream', stream, streamed))
    return originals
