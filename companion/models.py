"""One model gateway, immutable request selection, actual usage and quota accounting."""
from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime

import httpx

from .domain import QuotaExceeded
from .storage import uid


class Models:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.context = None
        self.background_slots = asyncio.Semaphore(4)
        self.chat_slots = asyncio.Semaphore(2)
        self.account_locks = {}
        self.task_check = None
        self.resources = None
        self.plan_ready = None
        self.image_upload_locks = {}

    def selected(self):
        cfg = self.config.get("models")
        selected = next((p for p in cfg["providers"] if p["id"] == cfg["selected"] and p.get("enabled", True)), None)
        if not selected:
            raise ValueError("请先在模型页面添加并选择一个主模型")
        from .reasoning import effective
        return effective(selected)

    async def catalog(self, spec):
        from .model_catalog import fetch_catalog
        kind = spec.get("kind", "openai")
        if kind not in {"plan", "openai", "responses", "gemini"}:
            raise ValueError("请选择模型接口类型")
        if kind == "plan":
            if not self.plan_ready:
                raise ValueError("GPT套餐桥尚未接入")
            key = await self.plan_ready()
            base = "http://127.0.0.1:24762/v1"
        else:
            base, key = str(spec.get("base_url", "")).strip(), str(spec.get("key") or "").strip()
            if not key:
                key = self.config.provider_key(spec)
            if not key:
                raise ValueError("请先填写API Key，再获取模型列表；获取时不会自动保存密钥")
        return await fetch_catalog(kind, base, key)

    def usage(self, owner, bot="main"):
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        reset = self.store.checkpoint("quota_reset:"+bot+":"+owner) or 0
        return self.store.one("SELECT COALESCE(SUM(input+output),0) AS tokens, COUNT(*) AS calls, SUM(CASE WHEN source!='image' AND status NOT IN ('running','rejected') AND (input IS NULL OR output IS NULL) THEN 1 ELSE 0 END) AS unknown FROM usage WHERE bot=? AND owner=? AND created>=?", (bot, owner, max(midnight, reset)))

    def check_quota(self, owner, bot="main"):
        cfg = self.config.get("usage")
        if owner in self.config.get("account", "admins"):
            return
        limit = cfg["public_daily"] if owner == "@public" else cfg["overrides"].get(owner, cfg["user_daily"])
        if not limit:
            return
        used = self.usage(owner, bot)
        if used["tokens"] >= limit:
            raise QuotaExceeded("公共额度不足" if owner == "@public" else "今日个人额度不足")
        if used["unknown"] and cfg.get("unknown_blocks"):
            raise QuotaExceeded("有调用未返回用量，额度暂时无法确认；请管理员核对或重置")

    async def generate(self, messages, tools=None, owner="@public", bot="main", task_id=None, source="chat", selected=None, effort=None, native=None):
        provider = copy.deepcopy(selected or self.selected())
        observed_messages = messages if native is None else [{"role": item.get("role", "tool"), "content": item.get("content", "")} for item in native.get("input", []) if isinstance(item, dict) and (item.get("role") or item.get("type") == "message")]
        if provider.get("kind") == "plan" and self.plan_ready:
            diagnostics = getattr(self, "diagnostics", None)
            if diagnostics:
                with diagnostics.span("model_prepare", "准备模型接入"):
                    await self.plan_ready()
            else:
                await self.plan_ready()
        key = bot+":"+owner
        lock = self.account_locks.setdefault(key, asyncio.Lock())
        async with lock:
            self.check_quota(owner, bot)
            if task_id and self.task_check:
                self.task_check(task_id)
            start = time.monotonic()
            ident = uid("u_")
            self.store.insert("usage", {"id": ident, "bot": bot, "owner": owner, "task_id": task_id,
                "model": provider["id"], "source": source, "status": "running", "duration": 0,
                "breakdown": json.dumps(self.breakdown(observed_messages, (tools or [])+(native or {}).get("tools", [])), ensure_ascii=False), "created": time.time()})
        dispatched = False
        diagnostics = getattr(self, "diagnostics", None)
        queued = diagnostics.start("model_queue", "等待模型槽位", {"usage_id": ident, "model": provider["model"], "effort": effort}) if diagnostics else None
        observed = None
        try:
            slots = self.chat_slots if source == "chat" else self.background_slots
            if self.resources is not None:
                slots = self.resources.acquire("model-chat" if source == "chat" else "model-work", owner == "@public", 2 if source == "chat" else 4)
            async with slots:
                self.check_quota(owner, bot)
                if task_id and self.task_check:
                    self.task_check(task_id)
                if diagnostics:
                    diagnostics.end(queued)
                    observed = diagnostics.start("model", "模型调用", {"usage_id": ident, "model": provider["model"], "provider": provider["id"], "effort": effort})
                    diagnostics.memory_submission(observed_messages, ident, "submitting")
                dispatched = True
                result = await self._call(provider, messages, tools or [], effort, native)
                if diagnostics:
                    diagnostics.end(observed)
                    diagnostics.memory_submission(observed_messages, ident, "supplied")
                    from .diagnostics import REQUEST
                    diagnostics.recover_request_stage(REQUEST.get(), "local")
            usage = result.pop("usage", {}) or {}
            self.store.execute("UPDATE usage SET input=?,output=?,cached=?,reasoning=?,status='success',duration=? WHERE id=?",
                (usage.get("input"), usage.get("output"), usage.get("cached"), usage.get("reasoning"), time.monotonic()-start, ident))
            if result.get("native_metrics"):
                breakdown = self.breakdown(observed_messages, (tools or [])+(native or {}).get("tools", []))
                breakdown["provider_tools"] = result["native_metrics"]
                self.store.execute("UPDATE usage SET breakdown=? WHERE id=?", (json.dumps(breakdown, ensure_ascii=False), ident))
            result["_usage_id"] = ident
            return result
        except BaseException as exc:
            status = "rejected" if not dispatched or getattr(exc, "status_code", None) in {400,401,403,404,410} else "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            self.store.execute("UPDATE usage SET status=?,duration=? WHERE id=?", (status, time.monotonic()-start, ident))
            if diagnostics:
                diagnostics.end(observed or queued, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error=exc)
                if dispatched:
                    diagnostics.memory_submission(observed_messages, ident, "unconfirmed")
            raise

    @staticmethod
    def breakdown(messages, tools):
        totals = {"system": 0, "history": 0, "current": 0, "tool_results": 0, "tools": len(json.dumps(tools, ensure_ascii=False)), "tool_arguments": 0, "reasoning_replay": 0, "images": 0}
        for i, message in enumerate(messages):
            content = message.get("content", "")
            key = "system" if message["role"] == "system" else "tool_results" if message["role"] == "tool" else "current" if i == len(messages)-1 else "history"
            totals[key] += len(content) if isinstance(content, str) else sum(len(c.get("text", "")) for c in content if isinstance(c, dict)) if isinstance(content, list) else 0
            totals["tool_arguments"] += sum(len(c.get("function", {}).get("arguments", "")) for c in message.get("tool_calls", []))
            totals["reasoning_replay"] += len(message.get("reasoning_content") or "")
            if isinstance(content, list):
                totals["images"] += sum(c.get("type") in {"image_url", "input_image"} for c in content)
        return {"characters": totals, "method": "character_counts_not_tokens", "media_tokens": None}

    async def _call(self, provider, messages, tools, effort, native=None):
        from .domain import ProviderImageExpired
        try:
            return await self._call_once(provider, messages, tools, effort, native)
        except ProviderImageExpired as exc:
            if native is not None or not any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" and p.get("image_url", {}).get("url", "").startswith("data:") for p in m["content"]) for m in messages):
                raise
            from .provider_files import invalidate_images
            invalidate_images(self, provider, messages)
            self.store.event("reuse", "服务商明确拒绝过期图片引用，重新上传一次", {"provider": provider["id"]})
            try:
                return await self._call_once(provider, messages, tools, effort, native)
            except ProviderImageExpired:
                raise ProviderImageExpired("图片上传引用失效，重新上传一次后仍被拒绝；已停止，没有重跑整个任务") from None

    async def _call_once(self, provider, messages, tools, effort, native=None):
        from .reasoning import effective, validate
        provider = effective(provider)
        effort = validate(provider, effort or provider.get("effort", "default"))
        if native is not None:
            return await self._responses(provider, messages, tools, effort, native)
        has_images = any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]) for m in messages)
        if has_images:
            if self.config.get("native", "features").get("vision") is False:
                from .domain import CapabilityUnavailable
                raise CapabilityUnavailable("当前已关闭原生识图；请在模型页开启后再读取图片")
            from .provider_files import reuse_provider_images
            messages = await reuse_provider_images(self, provider, messages)
        from .native_capabilities import family
        import re
        model_version = re.match(r"gpt-(\d+)(?:\.(\d+))?", provider.get("model", ""))
        if family(provider) == "openai" and model_version and (int(model_version[1]), int(model_version[2] or 0)) >= (5, 4):
            return await self._responses(provider, messages, tools, effort)
        if self.context is not None and effort == "default" and not has_images and provider.get("kind") not in {"plan", "responses", "gemini"}:
            from astrbot.core.agent.tool import FunctionTool, ToolSet
            manager = self.context.provider_manager
            instance = await manager.get_provider_by_id(provider["id"])
            if instance and getattr(instance, "companion_snapshot", None) == provider:
                tool_set = ToolSet([FunctionTool(name=t["function"]["name"], description=t["function"]["description"], parameters=t["function"]["parameters"]) for t in tools])
                system = "\n".join(m["content"] for m in messages if m["role"] == "system")
                contexts = [m for m in messages if m["role"] != "system"]
                response = await instance.text_chat(prompt=None, contexts=contexts, system_prompt=system, func_tool=tool_set if tools else None, request_max_retries=1, **self.effort_payload(provider, effort))
                raw_usage = getattr(getattr(response, "raw_completion", None), "usage", None)
                usage = raw_usage.model_dump() if raw_usage is not None and hasattr(raw_usage, "model_dump") else {}
                return {"content": response.completion_text or "", "reasoning_content": response.reasoning_content, "tool_calls": [{"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for i, n, a in zip(response.tools_call_ids, response.tools_call_name, response.tools_call_args)],
                    "usage": {"input":usage.get("prompt_tokens"),"output":usage.get("completion_tokens"),"cached":(usage.get("prompt_tokens_details") or {}).get("cached_tokens",usage.get("prompt_cache_hit_tokens")),"reasoning":(usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}}
        if provider.get("kind") == "gemini":
            return await self._gemini(provider, messages, tools, effort)
        if provider.get("kind") in {"responses", "plan"}:
            return await self._responses(provider, messages, tools, effort)
        payload = {"model": provider["model"], "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        payload.update(self.effort_payload(provider, effort))
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as client:
            response = await client.post(provider["base_url"].rstrip("/")+"/chat/completions", headers={"Authorization": "Bearer "+self.config.vault.get("provider:"+provider["id"])}, json=payload)
            self.raise_error(response)
            data = response.json()
        if provider["model"].startswith("deepseek"):
            from openai.types.chat import ChatCompletion
            from .compat.deepseek import recover_completion
            class SuppliedTools:
                def get_func_desc_openai_style(self):
                    return tools
            data = recover_completion(ChatCompletion.model_validate(data), SuppliedTools()).model_dump()
        message = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        calls = message.get("tool_calls", [])
        if not calls and "DSML" in (message.get("content") or ""):
            raise ValueError("模型把工具指令当成了文本，已阻止发送；请检查提供商工具调用兼容性")
        return {"content": message.get("content") or "", "reasoning_content": message.get("reasoning_content"), "tool_calls": calls,
            "usage": {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens"), "cached": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", usage.get("prompt_cache_hit_tokens")), "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}}

    @staticmethod
    def effort_payload(provider, effort):
        if effort == "default":
            return {}
        if provider["model"].startswith("deepseek"):
            return {"thinking": {"type": "disabled" if effort == "none" else "enabled"}, **({"reasoning_effort": effort} if effort != "none" else {})}
        return {"reasoning_effort": effort}

    @staticmethod
    def raise_error(response):
        if response.is_success:
            return
        if response.status_code in {400, 404, 410}:
            import re
            from .domain import ProviderImageExpired
            try:
                detail = json.dumps(response.json().get("error", {})).lower()
            except (ValueError, AttributeError):
                detail = ""
            if re.search(r"file[_ -]?(?:not[_ -]?found|expired)|file.{0,160}(?:not[_ -]?found|expired|does not exist|no longer exists)|no such file.{0,80}file-", detail):
                error = ProviderImageExpired("服务商明确返回上传图片不存在或已过期")
                error.status_code = response.status_code
                raise error
        from .domain import CapabilityUnavailable
        messages = {400: "模型不支持本次接口参数，请查看当前型号与原生能力配置", 401: "模型授权失效或 API Key 无效", 402: "提供商余额不足", 403: "当前账号无权调用该模型或原生工具", 429: "提供商额度不足或请求限流", 404: "模型或接口地址不存在"}
        error_type = CapabilityUnavailable if response.status_code in {400, 401, 402, 403, 404} else RuntimeError
        detail = ""
        try:
            payload = response.json().get("error", {})
            if isinstance(payload, dict):
                detail = str(payload.get("message", ""))
                if payload.get("param"):
                    detail = "参数 "+str(payload["param"])+"："+detail
            elif isinstance(payload, str):
                detail = payload
        except (ValueError, AttributeError):
            pass
        import re
        detail = re.sub(r"(?i)sk-[A-Za-z0-9_-]+|Bearer\s+\S+|data:[^;\s]+;base64,[A-Za-z0-9+/=]+", "[已隐藏]", detail)
        prefix = "模型请求被接口拒绝（HTTP 400）" if response.status_code == 400 else messages.get(response.status_code, f"模型服务返回 HTTP {response.status_code}")
        error = error_type(prefix+("："+detail[:500] if detail else ""))
        error.status_code = response.status_code
        raise error

    async def _responses(self, provider, messages, tools, effort, native=None):
        items = []
        for message in messages:
            if message["role"] == "tool":
                items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
                continue
            if message.get("content"):
                content = message["content"]
                if isinstance(content, list):
                    content = [{"type": "input_image", "image_url": c["image_url"]["url"]} if c.get("type") == "image_url" else {"type": "input_image", "file_id": c["file_id"]} if c.get("type") == "file" else {"type": "input_text", "text": c.get("text", "")} for c in content]
                items.append({"role": message["role"], "content": content})
            for call in message.get("tool_calls", []):
                items.append({"type": "function_call", "call_id": call["id"], "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
        payload = {"model": provider["model"], "input": items, "stream": False}
        if tools:
            payload["tools"] = [{"type": "function", "strict": False, **t["function"]} for t in tools]
        if native is None and tools:
            from .native_capabilities import family
            import re
            model = re.match(r"gpt-(\d+)(?:\.(\d+))?", provider["model"])
            enabled = self.config.get("native")
            if len(tools) > 3 and family(provider) == "openai" and enabled["enabled"] and enabled["features"].get("tool_search", True) and model and (int(model[1]), int(model[2] or 0)) >= (5, 4):
                for tool in payload["tools"]:
                    if tool["name"] != "load_tools":
                        tool["defer_loading"] = True
                payload["tools"].append({"type": "tool_search"})
        if native is not None:
            payload["tools"] = native["tools"]
            payload["input"] = native.get("input", items)
            payload["store"] = False
            if native.get("include"):
                payload["include"] = native["include"]
            if native.get("tool_choice"):
                payload["tool_choice"] = native["tool_choice"]
        if effort != "default":
            payload["reasoning"] = {"effort": effort}
        async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=15), trust_env=provider.get("kind") != "plan") as client:
            response = await client.post(provider["base_url"].rstrip("/")+"/responses", json=payload, headers={"Authorization": "Bearer "+self.config.vault.get("provider:"+provider["id"])})
            self.raise_error(response)
            data = response.json()
        text, calls, annotations = [], [], []
        for item in data.get("output", []):
            if item.get("type") == "function_call":
                calls.append({"id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item["arguments"]}})
            else:
                text.extend(c["text"] for c in item.get("content", []) if c.get("type") == "output_text")
                for content in item.get("content", []):
                    annotations.extend(content.get("annotations", []))
        usage = data.get("usage") or {}
        result = {"content": "\n".join(text), "tool_calls": calls, "annotations": annotations,
                  "native_metrics": {"calls": [{"type": i["type"], "status": i.get("status")} for i in data.get("output", []) if i.get("type", "").endswith("_call") and i["type"] != "function_call"], "server_tool_use": usage.get("server_tool_use"), "tool_cost": "not_reported"},
                  "usage": {"input": usage.get("input_tokens"), "output": usage.get("output_tokens"), "cached": (usage.get("input_tokens_details") or {}).get("cached_tokens"), "reasoning": (usage.get("output_tokens_details") or {}).get("reasoning_tokens")}}
        if native is not None:
            result.update(native_output=data.get("output", []), native_response_id=data.get("id"), native_status=data.get("status"), native_error=data.get("error"))
        return result

    async def service_call(self, provider, owner, bot, task_id, source, call):
        """Non-token provider operations (e.g. Search API) still share quota gates."""
        from .domain import CapabilityUnavailable
        self.check_quota(owner, bot)
        if task_id and self.task_check:
            self.task_check(task_id)
        ident, start = uid("u_"), time.monotonic()
        self.store.insert("usage", {"id": ident, "bot": bot, "owner": owner, "task_id": task_id, "model": provider["id"],
            "source": source, "status": "running", "duration": 0, "input": 0, "output": 0,
            "breakdown": json.dumps({"non_token_api": True, "provider_requests": 1, "cost": "not_reported"}), "created": time.time()})
        try:
            result = await call()
            self.store.execute("UPDATE usage SET status='success',duration=? WHERE id=?", (time.monotonic()-start, ident))
            return result
        except BaseException as exc:
            status = "rejected" if isinstance(exc, CapabilityUnavailable) else "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            self.store.execute("UPDATE usage SET status=?,duration=? WHERE id=?", (status, time.monotonic()-start, ident))
            raise

    async def _gemini(self, provider, messages, tools, effort):
        contents = []
        for message in messages:
            if message["role"] == "system":
                continue
            if message["role"] == "tool":
                contents.append({"role": "user", "parts": [{"functionResponse": {"name": message.get("name", "tool"), "response": {"result": message["content"]}}}]})
                continue
            parts = []
            value = message.get("content", "")
            if isinstance(value, str) and value:
                parts.append({"text": value})
            elif isinstance(value, list):
                for block in value:
                    if block.get("type") == "text":
                        parts.append({"text": block["text"]})
                    elif block.get("type") == "image_url":
                        url = block["image_url"]["url"]
                        if url.startswith("data:"):
                            mime, raw = url[5:].split(";base64,", 1)
                            parts.append({"inlineData": {"mimeType": mime, "data": raw}})
            for call in message.get("tool_calls", []):
                part = {"functionCall": {"name": call["function"]["name"], "args": json.loads(call["function"]["arguments"])}}
                if call.get("thoughtSignature"):
                    part["thoughtSignature"] = call["thoughtSignature"]
                parts.append(part)
            if parts:
                contents.append({"role": "model" if message["role"] == "assistant" else "user", "parts": parts})
        payload = {"contents": contents, "systemInstruction": {"parts": [{"text": "\n".join(m["content"] for m in messages if m["role"] == "system")} ]}}
        if tools:
            payload["tools"] = [{"functionDeclarations": [{"name": t["function"]["name"], "description": t["function"]["description"], "parameters": t["function"]["parameters"]} for t in tools]}]
        from .reasoning import gemini_config
        thinking = gemini_config(provider, effort)
        if thinking:
            payload["generationConfig"] = {"thinkingConfig": thinking}
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(provider["base_url"].rstrip("/")+"/v1beta/models/"+provider["model"]+":generateContent", headers={"x-goog-api-key": self.config.vault.get("provider:"+provider["id"])}, json=payload)
            self.raise_error(response)
            data = response.json()
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        usage = data.get("usageMetadata") or {}
        return {"content": "\n".join(p["text"] for p in parts if p.get("text") and not p.get("thought")), "tool_calls": [{"id": uid("g_"), "type": "function", **({"thoughtSignature": p["thoughtSignature"]} if p.get("thoughtSignature") else {}), "function": {"name": p["functionCall"]["name"], "arguments": json.dumps(p["functionCall"].get("args", {}))}} for p in parts if "functionCall" in p],
            "usage": {"input": usage.get("promptTokenCount"), "output": usage.get("candidatesTokenCount", 0)+usage.get("thoughtsTokenCount", 0) if "candidatesTokenCount" in usage or "thoughtsTokenCount" in usage else None, "cached": usage.get("cachedContentTokenCount"), "reasoning": usage.get("thoughtsTokenCount")}}
