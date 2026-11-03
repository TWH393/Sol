"""AstrBot embedding and managed services; framework-specific access stays here."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time
from contextvars import ContextVar
from pathlib import Path

import httpx

from .config import ROOT, RESOURCES
from .domain import Message
from .storage import uid

ACTIVE = None
SOURCE_EVENT = ContextVar("companion_source_event", default=None)


class AstrBotRuntime:
    def __init__(self, app):
        self.app = app
        self.core = None
        self.context = None
        self.task = None
        self.events = {}
        self.compat = []
        self.original_llm_generate = None
        self.stopping = False
        self.applied_account = None
        self.error = ""
        self.root = app.store.root / "astrbot"

    async def start(self):
        global ACTIVE
        if self.task and not self.task.done():
            return
        ACTIVE = self.app
        self.stopping = False
        self.root.mkdir(exist_ok=True)
        os.environ["ASTRBOT_ROOT"] = str(self.root)
        vendor = ROOT / "vendor/astrbot"
        if vendor.is_dir():
            sys.path.insert(0, str(vendor))
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        plugin = self.root / "data/plugins/companion_bridge"
        plugin.mkdir(parents=True, exist_ok=True)
        for package in (self.root / "data", self.root / "data/plugins"):
            (package / "__init__.py").touch(exist_ok=True)
        for name in ("main.py", "metadata.yaml", "__init__.py"):
            shutil.copy2(ROOT / "companion/bridge_plugin" / name, plugin / name)
        try:
            from astrbot.core import LogBroker, db_helper, astrbot_config
            from astrbot.core import core_lifecycle as lifecycle
            AstrBotCoreLifecycle = lifecycle.AstrBotCoreLifecycle
            account = self.app.config.get("account")
            astrbot_config["admins_id"] = account["admins"]
            astrbot_config["provider_settings"]["enable"] = False
            astrbot_config["wake_prefix"] = []
            astrbot_config["disable_builtin_commands"] = True
            astrbot_config["platform_settings"]["rate_limit"]["count"] = 0
            astrbot_config["platform_settings"]["enable_id_white_list"] = False
            astrbot_config["platform"] = [{"id": "companion-"+account["id"], "type": "aiocqhttp", "enable": True, "ws_reverse_host": "127.0.0.1", "ws_reverse_port": account["onebot_port"], "ws_reverse_token": self.app.config.vault.ensure("onebot")}] if account["enabled"] else []
            astrbot_config["dashboard"]["enable"] = False
            astrbot_config.save_config()
            self.core = AstrBotCoreLifecycle(LogBroker(), db_helper)
            native_manager = lifecycle.PluginManager
            class ManagedPlugins(native_manager):
                async def _ensure_plugin_requirements(self, plugin_dir_path, plugin_label):
                    if (Path(plugin_dir_path) / "requirements.txt").exists():
                        raise RuntimeError("插件依赖由同伴统一管理，核心不会自行安装或覆盖已有组件；请到插件页准备依赖，已有版本变更需要管理员确认")
            lifecycle.PluginManager = ManagedPlugins
            try:
                await self.core.initialize()
            finally:
                lifecycle.PluginManager = native_manager
            self.context = self.core.star_context
            self.app.models.context = self.context
            self.install_model_gateway()
            await self.sync_models()
            await self.apply_extension_switch()
            self.sync_external_tools()
            self.task = asyncio.create_task(self.core.start(), name="astrbot-core")
            def stopped(handle):
                if self.stopping or self.app.stopping or handle.cancelled():
                    return
                error = handle.exception()
                self.error = "内部核心意外停止"+(": "+str(error) if error else "")
                self.app.store.event("service", self.error, level="error")
                self.app.background_task(self.app.disconnected(self.error), "core-interruption")
            self.task.add_done_callback(stopped)
            self.error = ""
            self.applied_account = copy.deepcopy(account)
            self.app.store.event("service", "AstrBot 核心已启动")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.app.store.event("service", "AstrBot 核心启动失败", {"error": self.error}, "error")

    def install_model_gateway(self):
        """Meter the supported plugin SDK path without patching framework classes."""
        from astrbot.core.provider.entities import LLMResponse, TokenUsage
        self.original_llm_generate = self.context.llm_generate
        async def generate(*, chat_provider_id=None, prompt=None, image_urls=None, audio_urls=None, tools=None, system_prompt=None, contexts=None, **kwargs):
            source = SOURCE_EVENT.get()
            if source is None:
                raise RuntimeError("插件模型请求缺少可核对的消息来源，需要单独适配后台触发逻辑")
            message = copy.deepcopy(source)
            message.autonomous = not (message.private or message.mentioned or message.text.strip().startswith("/"))
            selected = self.app.models.selected()
            messages = [{"role":"system","content":system_prompt}] if system_prompt else []
            for entry in contexts or []:
                messages.append(entry if isinstance(entry, dict) else entry.model_dump(exclude_none=True))
            if prompt or image_urls:
                parts = [{"type":"text","text":prompt or ""}]
                parts += [{"type":"image_url","image_url":{"url":url}} for url in image_urls or []]
                messages.append({"role":"user","content":parts if len(parts)>1 else prompt or ""})
            if audio_urls:
                raise ValueError("插件原生音频请求尚未纳入统一用量路径，需要音频适配")
            schemas = tools.get_func_desc_openai_style() if tools else []
            task_id = self.app.tasks.create(message, "插件模型请求："+message.text[:60], "plugin", selected=selected)
            self.app.tasks.handles[task_id] = asyncio.current_task()
            self.app.tasks.progress(task_id, "插件调用统一主模型")
            try:
                async with asyncio.timeout(self.app.config.get("tasks", "timeout")):
                    result = await self.app.models.generate(messages, schemas, "@public" if message.autonomous else message.sender, message.bot, task_id, "plugin", selected)
                usage = self.app.store.one("SELECT input,output,cached FROM usage WHERE id=?", (result["_usage_id"],))
                token_usage = TokenUsage(input_other=usage["input"]-(usage["cached"] or 0), input_cached=usage["cached"] or 0, output=usage["output"]) if usage and usage["input"] is not None and usage["output"] is not None else None
                calls = result.get("tool_calls", [])
                response = LLMResponse(role="assistant", completion_text=result.get("content") or "", tools_call_name=[c["function"]["name"] for c in calls], tools_call_args=[json.loads(c["function"]["arguments"]) for c in calls], tools_call_ids=[c["id"] for c in calls], reasoning_content=result.get("reasoning_content"), usage=token_usage)
                self.app.tasks.finish(task_id, {"model_response_received": True, "plugin_delivery_not_verified": True})
                return response
            except BaseException as exc:
                self.app.tasks.finish(task_id, error=str(exc) or type(exc).__name__)
                raise
            finally:
                self.app.tasks.handles.pop(task_id, None)
        self.context.llm_generate = generate

    async def sync_models(self):
        if self.context is None:
            return
        for provider in self.app.config.get("models", "providers"):
            if not provider.get("enabled", True):
                continue
            kind = provider.get("kind", "openai")
            if kind == "gemini":
                continue
            entry = {"id": provider["id"], "type": "openai_responses" if kind in {"plan", "responses"} else "openai_chat_completion", "model": provider["model"], "enable": True, "api_base": provider["base_url"], "key": [self.app.config.vault.get("provider:"+provider["id"])], "timeout": 180}
            try:
                for item in list(self.compat):
                    old_provider, attribute, previous, replacement = item
                    if old_provider.provider_config.get("id") == provider["id"]:
                        if getattr(old_provider, attribute, None) is replacement:
                            setattr(old_provider, attribute, previous)
                        self.compat.remove(item)
                existing = await self.context.provider_manager.get_provider_by_id(provider["id"])
                if existing:
                    await self.context.provider_manager.reload(entry)
                else:
                    await self.context.provider_manager.load_provider(entry)
                instance = await self.context.provider_manager.get_provider_by_id(provider["id"])
                if instance:
                    instance.companion_snapshot = copy.deepcopy(provider)
                    if hasattr(instance, "client") and hasattr(instance.client, "with_options"):
                        instance.client = instance.client.with_options(max_retries=0)
                if instance and provider["model"].startswith("deepseek"):
                    from .compat.deepseek import install
                    self.compat.extend((instance, attribute, previous, replacement) for attribute, previous, replacement in install(instance))
            except Exception as exc:
                self.app.store.event("model", "模型适配器加载失败，使用统一 HTTP 接口", {"id": provider["id"], "error": type(exc).__name__}, "warning")

    async def reload_plugin(self, ident):
        if self.context is None:
            raise ValueError("AstrBot 核心没有启动")
        metadata = next((p for p in self.context.get_all_stars() if p.root_dir_name == ident), None)
        result = await self.context._star_manager.reload(metadata.name) if metadata else await self.context._star_manager.load(specified_dir_name=ident)
        if isinstance(result, tuple) and result and result[0] is False:
            raise RuntimeError(str(result[1]) if len(result)>1 else "插件加载失败")
        self.sync_external_tools()

    async def apply_extension_switch(self):
        if not self.context:
            return
        enabled = self.app.config.get("features", "plugins")
        wanted = {r["id"] for r in self.app.store.rows("SELECT id FROM extensions WHERE enabled=1 AND kind='plugin'")}
        for plugin in list(self.context.get_all_stars()):
            if plugin.root_dir_name == "companion_bridge" or not plugin.module_path.startswith("data.plugins."):
                continue
            if not enabled and plugin.activated:
                await self.context._star_manager.turn_off_plugin(plugin.name)
            elif enabled and plugin.root_dir_name in wanted and not plugin.activated:
                await self.context._star_manager.turn_on_plugin(plugin.name)
        self.sync_external_tools()

    def sync_external_tools(self):
        if self.context is None:
            return
        from .agent import CATEGORIES
        from .domain import ToolResult
        import inspect
        for name in [n for n,t in self.app.registry.items.items() if t.category.startswith("plugin:")]:
            self.app.registry.items.pop(name, None)
        for tool in self.context.provider_manager.llm_tools.func_list:
            module = tool.handler_module_path or getattr(getattr(tool.handler, "func", tool.handler), "__module__", "")
            if "plugins." not in module or "companion_bridge" in module or not tool.active or not tool.handler:
                continue
            origin = module.split("plugins.", 1)[1].split(".", 1)[0]
            category = "plugin:"+origin
            if category not in CATEGORIES:
                CATEGORIES.append(category)
            name = "ext_"+origin[:18]+"_"+tool.name[:35]
            handler = tool.handler
            async def invoke(message, task_id, _handler=handler, **kwargs):
                event = self.events.get(message.id)
                if event is None:
                    raise RuntimeError("原始会话事件已过期，不能伪造插件上下文")
                signature = inspect.signature(_handler)
                if "event" in signature.parameters:
                    kwargs["event"] = event
                result = _handler(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if inspect.isasyncgen(result):
                    parts = []
                    async for part in result:
                        parts.append(part.get_plain_text() if hasattr(part, "get_plain_text") else str(part))
                    result = "\n".join(parts)
                if event._has_send_oper:
                    return ToolResult("", terminal=True)
                return ToolResult(str(result or "插件执行完成"))
            self.app.registry.add(name, category, tool.description or tool.name, tool.parameters.get("properties", {}), tool.parameters.get("required", []), invoke)

    async def interrupt_plugin_handlers(self):
        event_bus = getattr(self.core, "event_bus", None)
        pending = list(getattr(event_bus, "_pending_tasks", set()))
        for handle in pending:
            handle.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def stop(self):
        self.stopping = True
        if self.context and self.original_llm_generate:
            self.context.llm_generate = self.original_llm_generate
            self.original_llm_generate = None
        for provider, attribute, previous, replacement in self.compat:
            if getattr(provider, attribute, None) is replacement:
                setattr(provider, attribute, previous)
        self.compat.clear()
        if self.core:
            try:
                async with asyncio.timeout(30):
                    await self.core.stop()
            except Exception as exc:
                self.app.store.event("service", "部分核心组件停止未确认，继续停止本软件其他服务", {"error": str(exc)}, "warning")
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
        self.context = None
        self.app.models.context = None

    async def ingest(self, event):
        from astrbot.api.message_components import At, Image, File, Reply, Plain, Forward, Node, Nodes
        scope = "private:"+event.get_sender_id() if event.is_private_chat() else "group:"+event.get_group_id()
        bot = self.app.config.get("account", "id")
        ident = f"{bot}:{event.message_obj.message_id}"
        components = event.get_messages()
        quote = next((c for c in components if isinstance(c, Reply)), None)
        quote_id = f"{bot}:{quote.id}" if quote else ""
        attachments = []
        # Native media is already archived by the shared Files service. Keep
        # OneBot transport textual so its generic adapter does not re-download
        # private local files or ask unsupported get_*_file_url actions.
        raw_native = getattr(event.message_obj, "raw_message", None)
        native_quote = raw_native.get("sol_native_reply") if isinstance(raw_native, dict) and self.app.config.get("account", "driver") == "native" else None
        if native_quote and re.fullmatch(r"[1-9]\d*", str(native_quote.get("id") or "")):
            quote_id = f"{bot}:{native_quote['id']}"
            if quote is None:
                # Native relay deliberately omits the OneBot reply segment so
                # framework conversion cannot wait on a redundant get_msg call.
                # Restore the actual component before third-party plugins run.
                saved_quote = self.app.store.one("SELECT text,sender,name,created FROM messages WHERE id=? AND bot=? AND scope=?", (quote_id, bot, scope))
                if saved_quote:
                    source_text, source_sender, source_name = saved_quote["text"], saved_quote["sender"], saved_quote["name"]
                    source_time = int(saved_quote["created"])
                else:
                    source_text = "引用原消息未取得" if native_quote.get("missing") else str(native_quote.get("text") or "")
                    source_sender, source_name = str(native_quote.get("sender") or ""), str(native_quote.get("name") or "未知")
                    source_time = 0
                quote = Reply(id=str(native_quote["id"]), chain=[Plain(text=source_text)] if source_text else [],
                    sender_id=source_sender or 0, sender_nickname=source_name, time=source_time,
                    message_str=source_text, text=source_text)
                components.insert(0, quote)
        if self.app.config.get("account", "driver") == "native" and isinstance(raw_native, dict):
            for item in raw_native.get("sol_native_attachments", [])[:17]:
                if not isinstance(item, dict):
                    continue
                attachment = {k: item[k] for k in ("file_id", "name", "kind", "native_id", "error") if k in item}
                if attachment.get("kind") not in {"image", "file"}:
                    continue
                if attachment.get("file_id"):
                    saved = self.app.store.one("SELECT bot,scope,owner FROM files WHERE id=? AND status='ready'", (attachment["file_id"],))
                    if not saved or saved["bot"] != bot or saved["scope"] != scope or saved["owner"] != event.get_sender_id():
                        attachment.pop("file_id", None)
                        attachment["error"] = "附件记录不属于当前消息来源，已拒绝读取"
                attachments.append(attachment)
        excerpts = []
        if native_quote and not quote:
            saved_quote = self.app.store.one("SELECT text,sender,name,payload FROM messages WHERE id=? AND bot=? AND scope=?", (quote_id, bot, scope)) if quote_id else None
            if saved_quote:
                excerpts.append(f"引用 {saved_quote['name']} ({saved_quote['sender']})：{saved_quote['text']}")
                attachments.extend({**a, "quoted": True} for a in json.loads(saved_quote["payload"]).get("attachments", []))
            else:
                excerpts.append("引用来源："+json.dumps(native_quote, ensure_ascii=False)+"；未取得的原文或附件不能猜测")
        visited = set()
        async def raw_chain(chain, source, depth=0):
            if depth > 4:
                excerpts.append("[嵌套转发过深，未展开剩余部分]")
                return
            if not isinstance(chain, list):
                return
            for segment in chain[:80]:
                data = segment.get("data") or {}
                typ = segment.get("type")
                if typ == "text":
                    excerpts.append(source+"："+str(data.get("text", "")))
                elif typ in {"image", "file", "video", "record"}:
                    attachments.append({"kind": "image" if typ == "image" else "file", "name": data.get("name") or data.get("file_name") or {"image":"图片.png","video":"视频.mp4","record":"语音.silk"}.get(typ,"文件"), "url": data.get("url"), "path": data.get("file"), "qq_file": data.get("file_id") or data.get("file"), "source": source, "quoted": True})
                elif typ == "forward":
                    await forward(str(data.get("id") or data.get("resid") or ""), depth+1)
                elif typ == "node":
                    await raw_chain(data.get("content", []), str(data.get("nickname") or data.get("user_id") or "转发者"), depth+1)
        async def forward(forward_id, depth=0):
            if not forward_id or forward_id in visited or depth > 4:
                return
            visited.add(forward_id)
            try:
                async with asyncio.timeout(12):
                    result = await event.bot.call_action(action="get_forward_msg", message_id=forward_id)
                for node in (result.get("messages") or result.get("message") or [])[:40]:
                    sender = node.get("sender") or {}
                    source = f"转发 {sender.get('nickname', '未知')} ({sender.get('user_id', '?')}) / {node.get('time', '?')}"
                    await raw_chain(node.get("content") or node.get("message") or [], source, depth)
            except Exception as exc:
                excerpts.append("[转发记录读取失败："+type(exc).__name__+"；不能声称已看过]")
                self.app.store.event("attachment", "转发记录读取失败", {"message": ident, "forward": forward_id, "error": str(exc)}, "warning")
        async def capture(chain, quoted=False):
            for item in chain:
                if isinstance(item, (Image, File)):
                    attachments.append({"kind": "image" if isinstance(item, Image) else "file", "name": getattr(item, "name", None) or ("图片.png" if isinstance(item, Image) else "文件"), "url": getattr(item, "url", None), "path": getattr(item, "file_", None) or getattr(item, "file", None), "qq_file": getattr(item, "file", None), "quoted": quoted})
                elif isinstance(item, Reply):
                    saved = self.app.store.one("SELECT payload,text,sender,name,scope FROM messages WHERE id=? AND bot=?", (f"{bot}:{item.id}", bot))
                    if saved and saved["scope"] == scope:
                        for attachment in json.loads(saved["payload"]).get("attachments", []):
                            attachments.append({**attachment, "quoted": True})
                        excerpts.append(f"引用 {saved['name']} ({saved['sender']})：{saved['text']}")
                    else:
                        excerpts.append(f"引用 {item.sender_nickname} ({item.sender_id})：{item.message_str or item.text or ''}")
                        await capture(item.chain or [], True)
                elif isinstance(item, Forward):
                    await forward(item.id)
                elif isinstance(item, Node):
                    excerpts.append(f"转发 {item.name} ({item.uin}) / {item.time}")
                    await capture(item.content, True)
                elif isinstance(item, Nodes):
                    await capture(item.nodes, quoted)
                elif quoted and isinstance(item, Plain):
                    excerpts.append(item.text)
        await capture(components)
        # The adapter may preserve a raw forward segment that has no component class.
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            for segment in raw.get("message", []) if isinstance(raw.get("message"), list) else []:
                if segment.get("type") == "forward":
                    await forward(str(segment.get("data", {}).get("id", "")))
        unique = {}
        for attachment in attachments:
            key = attachment.get("file_id") or attachment.get("native_id") or attachment.get("url") or str(attachment.get("path"))
            unique.setdefault(key, attachment)
        text = event.get_message_str()
        if excerpts:
            text += "\n[以下为引用/转发资料，保留原发言人，不能当成当前用户自述]\n"+"\n".join(excerpts)
        message = Message(ident, bot, scope, event.get_sender_id(), event.get_sender_name(), text, event.is_private_chat(), quote_id, any(isinstance(c, At) and str(c.qq) == event.get_self_id() for c in components) or bool(quote and str(getattr(quote, "sender_id", "")) == event.get_self_id()), attachments=list(unique.values()), origin=event.unified_msg_origin)
        message.targets = [str(c.qq) for c in components if isinstance(c, At) and str(c.qq) != event.get_self_id()]
        if native_quote:
            message.quote_source = copy.deepcopy(native_quote)
        elif quote:
            message.quote_source = {"id": quote_id, "sender": str(getattr(quote, "sender_id", "")), "name": getattr(quote, "sender_nickname", "未知"), "text": getattr(quote, "message_str", "") or getattr(quote, "text", "") or "", "missing": False}
        if native_quote and str(native_quote.get("sender")) == event.get_self_id():
            message.mentioned = True
        self.events[ident] = event
        if len(self.events) > 512:
            self.events.pop(next(iter(self.events)))
        return message

    async def refresh_attachment(self, message, attachment):
        event = self.events.get(message.id)
        if not event:
            raise ValueError("原始QQ事件已过期，无法刷新附件地址")
        file = attachment.get("qq_file")
        if attachment["kind"] == "image" and file:
            async with asyncio.timeout(12):
                result = await event.bot.call_action(action="get_image", file=file)
            return result.get("url")
        raise ValueError("QQ没有提供可刷新的附件标识")

    async def send(self, message, text=None, file=None):
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.api.message_components import Plain, Image, File, Reply, At
        if self.context is None or not self.app.online:
            raise ConnectionError("QQ 当前未连接")
        if self.app.config.get("account", "driver") == "native":
            # Shared Files/Delivery remain authoritative; native transport can use
            # the file directly instead of allocating an additional base64 copy.
            return await self.app.services.native_qq.send_message(message, file, text)
        chain = []
        for target in message.notify:
            chain.append(At(qq=target))
        if message.quote_reply:
            source_id = (message.reply_to or message.id).split(":", 1)[-1]
            if source_id.isdigit():
                chain.append(Reply(id=source_id))
        if file:
            row, path = self.app.files.resolve(file, message)
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                chain.append(Image.fromFileSystem(str(path)))
            else:
                chain.append(File(file=str(path), name=row["name"]))
        if text:
            chain.append(Plain(text))
        origin = message.origin
        if not origin:
            from astrbot.core.platform.message_type import MessageType
            group = message.scope.startswith("group:")
            typ = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
            origin = f"companion-{message.bot}:{typ.value}:{message.scope.split(':', 1)[1]}"
        result = await self.context.send_message(origin, MessageChain(chain))
        if result is False:
            raise ConnectionError("QQ 接入拒绝了消息发送")
        return result


class Services:
    def __init__(self, app):
        self.app = app
        self.processes = {}
        self.qq_started = False
        self.bridge_lock = asyncio.Lock()
        self.qq_lock = asyncio.Lock()
        self.outbound_lock = asyncio.Lock()
        self.last_send = 0
        self.qq_root = app.store.root / "qq"
        self.qq_root.mkdir(exist_ok=True)
        from .qq_native import NativeQQ
        self.native_qq = NativeQQ(app)

    async def pace_send(self):
        import random
        async with self.outbound_lock:
            gap = random.uniform(*self.app.config.get("chat", "send_interval"))
            await asyncio.sleep(max(0, self.last_send+gap-time.monotonic()))
            self.last_send = time.monotonic()

    async def command(self, *args, cwd=None, env=None, timeout=120):
        process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
            if process.returncode:
                raise RuntimeError(stderr.decode("utf-8", errors="replace")[-1000:])
            return stdout.decode("utf-8", errors="replace")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def qq(self, action):
        async with self.qq_lock:
            if self.app.config.get("account", "driver") != "legacy_llbot":
                if action == "stop":
                    await self.native_qq.stop()
                    self.qq_started = False
                    await self.app.disconnected("停止Sol QQ接入")
                    return {"stopped": True}
                result = await self.native_qq.start()
                self.qq_started = True
                return result
            return await self._qq(action)

    async def _qq(self, action):
        import yaml
        if action not in {"start", "stop"}:
            raise ValueError("QQ服务操作无效")
        account = self.app.config.get("account")
        if action == "stop":
            docker = shutil.which("docker")
            if not docker or not (self.qq_root / "compose.yaml").is_file():
                raise RuntimeError("未找到本应用的Docker接入配置，无法确认停止")
            await self.command(docker, "compose", "--project-directory", str(self.qq_root), "stop", timeout=30)
            self.qq_started = False
            await self.app.disconnected("管理员停止 QQ 服务")
            self.app.store.event("service", "本应用 QQ 服务已停止")
            return {"stopped": True}
        if not account["qq"].isdigit():
            raise ValueError("请先填写机器人 QQ 号，软件不会猜测或登录其他账号")
        project = "companion-"+account["id"]
        compose = {"name": project, "services": {
            "pmhq": {"image": account["pmhq_image"], "environment": {"AUTO_LOGIN_QQ": account["qq"], "PMHQ_AUTH_TOKEN": "${PMHQ_AUTH_TOKEN}"}, "volumes": ["qq_session:/root/.config/QQ"], "shm_size": "512mb", "restart": "unless-stopped", "ports": ["127.0.0.1:13005:13000"]},
            "llbot": {"image": account["llbot_image"], "environment": {"WEBUI_PORT": "3080", "PROTOCOL_MODE": "pmhq", "PMHQ_HOST": "pmhq"}, "ports": [f"127.0.0.1:{account['webui_port']}:3080"], "extra_hosts": ["host.docker.internal:host-gateway"], "volumes": ["./llbot:/app/llbot/data", "./exchange:/exchange"], "depends_on": ["pmhq"], "restart": "unless-stopped"}}, "volumes": {"qq_session": {}}}
        (self.qq_root / "compose.yaml").write_text(yaml.safe_dump(compose, allow_unicode=True), "utf-8")
        (self.qq_root / "llbot").mkdir(exist_ok=True)
        (self.qq_root / "exchange").mkdir(exist_ok=True)
        token = self.app.config.vault.get("llbot_auth")
        if action == "start" and not token:
            raise ValueError("请先在Sol的“客户端授权”中保存LLBot授权Token，再启动登录")
        if token:
            (self.qq_root / "llbot/auth_token.txt").write_text(token, "utf-8")
        # Sol owns this component's local management password; never expose it to the UI.
        webui_token = self.qq_root / "llbot/webui_token.txt"
        if not webui_token.exists():
            webui_token.write_text(self.app.config.vault.ensure("llbot_webui"), "utf-8")
        llbot = {"webui": {"enable": True, "host": "0.0.0.0", "port": 3080}, "milky": {"enable": False}, "satori": {"enable": False}, "ob11": {"enable": True, "connect": [{"type": "ws-reverse", "enable": True, "url": f"ws://host.docker.internal:{self.app.config.get('app','port')}/onebot", "token": self.app.config.vault.ensure("onebot"), "heartInterval": 10000, "reportSelfMessage": False, "reportOfflineMessage": False, "messageFormat": "array", "debug": False}]}, "enableLocalFile2Url": False, "log": True, "autoDeleteFile": True, "autoDeleteFileSecond": 172800, "msgCacheExpire": 3600}
        (self.qq_root / "llbot" / ("config_"+account["qq"]+".json")).write_text(json.dumps(llbot, ensure_ascii=False, indent=2), "utf-8")
        env = {**os.environ, "PMHQ_AUTH_TOKEN": token or self.app.config.vault.get("pmhq")}
        docker = shutil.which("docker")
        if not docker:
            raise RuntimeError("Docker 未安装，请先完成统一安装")
        self.qq_started = True  # A partially started compose project still needs cleanup on exit.
        try:
            await self.command(docker, "compose", "--project-directory", str(self.qq_root), "up", "-d", "--pull", "never", env=env)
        except Exception as exc:
            raise RuntimeError("QQ组件启动失败，请确认Docker Desktop已运行、WSL正常且接入组件已安装。详细原因："+str(exc)) from exc
        # Readiness is checked only as part of an explicit start, not by background health loops.
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            for _ in range(15):
                try:
                    ready = await client.get(f"http://127.0.0.1:{account['webui_port']}/")
                    if ready.is_success:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            else:
                raise RuntimeError("QQ容器已启动，但登录页面尚未就绪；请稍候刷新二维码，或查看Docker/QQ组件日志。未重复启动组件。")
        self.app.store.event("service", "QQ 服务已启动" if action == "start" else "QQ 服务已停止")
        return {"webui": f"http://127.0.0.1:{account['webui_port']}", "reverse_ws": f"ws://host.docker.internal:{self.app.config.get('app','port')}/onebot", "token_configured": True}

    async def qq_api(self, path):
        """Only the pinned LLBot login API, over Sol's own loopback port."""
        if not self.qq_started:
            raise ValueError("QQ服务尚未启动，请先点击“启动并扫码登录”")
        token_file = self.qq_root / "llbot/webui_token.txt"
        if not token_file.is_file():
            raise ValueError("本应用的QQ登录组件尚未准备好，请停止后重新启动QQ服务")
        token = hashlib.sha256(token_file.read_text("utf-8").strip().encode()).hexdigest()
        port = self.app.config.get("account", "webui_port")
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
                response = await client.get(f"http://127.0.0.1:{port}/api/{path}", headers={"X-Webui-Token": token})
        except httpx.HTTPError as exc:
            raise RuntimeError("QQ登录组件暂未响应；刚启动时请稍候再点刷新，持续失败请查看活动记录") from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("QQ登录组件返回了无效数据，请检查LLBot组件版本") from exc
        if not response.is_success or result.get("success") is not True:
            raise RuntimeError("QQ登录接口失败："+str(result.get("message") or f"HTTP {response.status_code}"))
        return result.get("data") or {}

    async def qq_status(self):
        if self.app.config.get("account", "driver") != "legacy_llbot":
            return await self.native_qq.status()
        account = self.app.config.get("account")
        status = {"expected": account["qq"], "authorized": bool(self.app.config.vault.get("llbot_auth")),
                  "started": self.qq_started, "connected": self.app.online, "online": False, "actual": "", "error": ""}
        if not self.qq_started:
            return status
        try:
            info = await self.qq_api("login-info")
            status.update(online=bool(info.get("online")), actual=str(info.get("uin") or ""), nickname=info.get("nick", ""))
            if status["online"] and status["actual"] != status["expected"]:
                status["error"] = "登录账号与指定机器人不符，Sol已拒绝接入。请停止QQ服务并使用指定账号重新扫码。"
                status["connected"] = False
        except (ValueError, RuntimeError) as exc:
            status["error"] = str(exc)
        return status

    async def qq_qrcode(self):
        if self.app.config.get("account", "driver") != "legacy_llbot":
            return await self.native_qq.qrcode()
        status = await self.qq_status()
        if status["online"]:
            if status["error"]:
                raise ValueError(status["error"])
            return {"online": True, "expected": status["expected"]}
        data = await self.qq_api("login-qrcode")
        image = str(data.get("pngBase64QrcodeData") or "")
        if image.startswith("data:image/png;base64,"):
            image = image.split(",", 1)[1]
        if not image or len(image) > 3_000_000:
            raise RuntimeError("QQ二维码尚未生成，请稍候点击“刷新二维码”；若持续失败请检查客户端授权")
        import base64
        try:
            decoded = base64.b64decode(image, validate=True)
            if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("not PNG")
        except ValueError as exc:
            raise RuntimeError("QQ组件返回的二维码图片无效，请刷新后重试") from exc
        return {"online": False, "expected": status["expected"], "image": image,
                "expires_in": max(1, min(600, int(data.get("expireTime") or 120)))}

    async def start_bridge(self):
        async with self.bridge_lock:
            await self._start_bridge()

    async def _start_bridge(self):
        if "plan" in self.processes and self.processes["plan"].returncode is None:
            return
        import socket
        with socket.socket() as port:
            try:
                port.bind(("127.0.0.1", 24762))
            except OSError as exc:
                raise RuntimeError("新软件的套餐端口24762被其他进程占用，未接管其他服务") from exc
        root = ROOT / "vendor/plan_bridge"
        if not (root / "bridge.py").is_file():
            raise FileNotFoundError("套餐桥组件尚未准备")
        env = {**os.environ, "QQ_PLAN_PORT": "24762", "QQ_PLAN_DATA_DIR": str(self.app.store.root / "plan"), "COMPANION_MANAGED": "1", "PYTHONUNBUFFERED": "1"}
        env.pop("SOL_DESKTOP_TOKEN", None)
        # Never inherit the desktop engine's stdin lifetime pipe: it has a blocking reader.
        # The bridge is managed through its own process handle and does not own the lease.
        with (self.app.store.root / "plan-bridge.log").open("ab") as log:
            process = await asyncio.create_subprocess_exec(sys.executable, "-u", str(root / "launch.py"), cwd=root, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, creationflags=0x08000000 if os.name == "nt" else 0)
        self.processes["plan"] = process

    async def plan_credentials(self):
        from .config import Vault
        await self.start_bridge()
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            settings_file = self.app.store.root / "plan/settings.bin"
            if settings_file.is_file():
                try:
                    settings = json.loads(Vault._protect(settings_file.read_bytes(), True))
                    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                        health = await client.get("http://127.0.0.1:24762/health")
                        if health.json().get("pid") != self.processes["plan"].pid:
                            raise RuntimeError("套餐桥进程身份与本软件不符")
                    for provider in self.app.config.get("models", "providers"):
                        if provider.get("kind") == "plan":
                            self.app.config.vault.set("provider:"+provider["id"], settings["bridge_key"])
                    return settings["bridge_key"]
                except (OSError, ValueError, httpx.HTTPError):
                    pass
            process = self.processes.get("plan")
            if process and process.returncode is not None:
                raise RuntimeError("套餐桥启动失败，请查看活动记录与plan-bridge.log")
            await asyncio.sleep(.2)
        stage = self.app.store.root / "plan/startup.json"
        detail = json.loads(stage.read_text("utf-8")).get("stage", "") if stage.is_file() else "Python子进程尚未进入启动脚本"
        raise TimeoutError("GPT登录桥未就绪："+detail+"。请查看plan-bridge.log；没有发起模型调用。")

    async def start_local(self):
        runtime = ROOT / "runtime/ollama/ollama.exe"
        if not runtime.is_file() or not self.app.config.get("models", "local_enabled"):
            return
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            try:
                response = await client.get(self.app.config.get("models", "local_url")+"/api/version")
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
        from urllib.parse import urlparse
        url = urlparse(self.app.config.get("models", "local_url"))
        env = {**os.environ, "OLLAMA_HOST": url.netloc, "OLLAMA_MODELS": str(RESOURCES / "ollama"), "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1"}
        args = [str(runtime), "serve"]
        marker = None
        if os.name == "nt":
            for key in ("SOL_DESKTOP_TOKEN", "PYTHONHOME", "PYTHONPATH"):
                env.pop(key, None)
            nonce = uid()
            marker = self.app.store.root / "local-service.json"
            args = [sys.executable, "-I", "-S", "-X", "utf8", "-u",
                    str(ROOT / "companion/workers/local_service.py"), str(marker), nonce]
        with (self.app.store.root / "local-model.log").open("ab") as log:
            process = await asyncio.create_subprocess_exec(
                *args, env=env, stdout=log, stderr=log,
                stdin=asyncio.subprocess.PIPE if marker else asyncio.subprocess.DEVNULL,
                creationflags=0x08000000 if os.name == "nt" else 0)
        self.processes["local"] = process
        if marker:
            try:
                for _ in range(50):
                    try:
                        state = json.loads(marker.read_text("utf-8"))
                    except (OSError, ValueError):
                        state = {}
                    if state.get("nonce") == nonce and state.get("pid") == process.pid:
                        if state.get("state") == "started" and state.get("protected"):
                            return
                        raise RuntimeError("本地模型服务未启动："+str(state.get("error") or state))
                    if process.returncode is not None:
                        raise RuntimeError(f"本地模型守护进程已退出（{process.returncode}），请查看local-model.log")
                    await asyncio.sleep(.1)
                raise TimeoutError("本地模型进程组保护未就绪，请查看local-model.log；未发起模型调用")
            except BaseException:
                await self._stop_process("local", process)
                raise

    async def _stop_process(self, name, process):
        if process.returncode is not None:
            return
        if name == "local" and os.name == "nt" and process.stdin:
            # EOF lets the supervisor release its Windows job, including runners.
            process.stdin.close()
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            # Killing the supervisor closes the job even if stdin handling stalled.
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def stop(self):
        try:
            await self.native_qq.stop()
        except Exception as exc:
            self.app.store.event("service", "QQ 服务停止未确认", {"error": str(exc)}, "warning")
        if self.qq_started:
            try:
                await self.qq("stop")
            except Exception as exc:
                self.app.store.event("service", "QQ 服务停止未确认", {"error": str(exc)}, "warning")
        for name, process in self.processes.items():
            try:
                await self._stop_process(name, process)
            except Exception as exc:
                self.app.store.event("service", "内部服务停止未确认", {"service": name, "error": str(exc)}, "warning")


class Delivery:
    def __init__(self, app):
        self.app = app
        self.queue = asyncio.Queue()
        self.file_queue = asyncio.Queue()
        self.worker = None
        self.file_worker = None
        self.inflight = set()
        self.replied = set()
        self.reply_claims = set()
        self.observations = {}

    async def text(self, message, text):
        import re
        if not text.strip():
            return
        if text.lstrip().startswith("<think>") and "</think>" in text:
            text = re.sub(r"^\s*<think>.*?</think>\s*", "", text, count=1, flags=re.S)
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        text = re.sub(r"(?m)^#{1,6}\s+", "", text)
        if not re.search(r"链接|网址|来源|出处|url|source", message.text, re.I):
            text = re.sub(r"\[([^\]]+)\]\(https?://[^\s)]+\)", lambda m: "" if re.fullmatch(r"(?:[\w-]+\.)+[a-z]{2,}", m[1], re.I) else m[1], text)
            text = re.sub(r"https?://[^\s)]+", "", text)
            text = re.sub(r"\(\s*\)|（\s*）", "", text)
        if re.search(r"<\s*/?\s*[|｜]*\s*DSML|<tool_call>|<think>", text, re.I):
            raise ValueError("检测到未解析的模型控制标记，已阻止发送")
        # Keep code blocks and lists together; split short natural sentences into separate bubbles.
        if self.app.config.get("chat", "natural") and len(text) < 500 and "```" not in text and not re.search(r"(?m)^\s*(?:[-*]|\d+[.、])\s", text):
            parts = [p.strip() for p in re.split(r"\n\s*\n|(?<=[。！？!?])\s*", text) if p.strip()]
            parts = [p.rstrip("。") for p in parts]
        else:
            parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        max_bubbles = self.app.config.get("chat", "max_bubbles")
        if max_bubbles > 0 and len(parts) > max_bubbles:
            parts = parts[:max_bubbles-1]+["\n\n".join(parts[max_bubbles-1:])]
        for part in parts:
            await self.enqueue(message, text=part)

    async def enqueue(self, message, text=None, file=None):
        from .diagnostics import REQUEST, TASK
        request_id = message.request_id or REQUEST.get()
        request = self.app.store.one("SELECT task_id FROM requests WHERE id=?", (request_id,)) if request_id else None
        file_task = self.app.store.one("SELECT task_id FROM files WHERE id=?", (file,)) if file else None
        task_id = message.schedule_id or (request or {}).get("task_id") or TASK.get() or (file_task or {}).get("task_id")
        delivery_id = uid("delivery_")
        self.app.store.insert("deliveries", {"id": delivery_id, "request_id": request_id, "task_id": task_id,
            "bot": message.bot, "scope": message.scope, "source_message": message.id,
            "file_id": file, "text": text, "status": "queued", "created": time.time()})
        span = self.app.diagnostics.start("send_queue", "附件发送排队" if file else "文字发送排队", {"delivery_id": delivery_id, "file_id": file}, request_id)
        future = asyncio.get_running_loop().create_future()
        self.observations[future] = {"id": delivery_id, "request_id": request_id, "task_id": task_id, "queue_span": span}
        queue = self.file_queue if file else self.queue
        await queue.put((self.app.tasks.epoch, copy.deepcopy(message), text, file, future))
        try:
            await future
        except BaseException as exc:
            self.app.store.execute("UPDATE deliveries SET status='cancelled',error=?,finished=? WHERE id=? AND status='queued'", (str(exc) or "排队已中断，尚未提交", time.time(), delivery_id))
            self.app.diagnostics.end(span, "cancelled", error=exc if isinstance(exc, asyncio.CancelledError) else None)
            raise
        finally:
            self.observations.pop(future, None)

    async def interrupt(self):
        for queue in (self.queue, self.file_queue):
            while not queue.empty():
                _, _, _, _, future = queue.get_nowait()
                if not future.done():
                    future.set_exception(ConnectionError("连接状态变化，旧消息已丢弃，不补发"))
                queue.task_done()
        handles = list(self.inflight)
        for handle in handles:
            handle.cancel()
        await asyncio.gather(*handles, return_exceptions=True)
        self.replied.clear()
        self.reply_claims.clear()

    async def run(self, files=False):
        queue = self.file_queue if files else self.queue
        while True:
            item = await queue.get()
            handle = asyncio.create_task(self.send_item(*item))
            future = item[-1]
            def cancelled(done, current=handle):
                if done.cancelled():
                    current.cancel()
            future.add_done_callback(cancelled)
            self.inflight.add(handle)
            try:
                await handle
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                future.remove_done_callback(cancelled)
                self.inflight.discard(handle)
                queue.task_done()

    async def send_item(self, epoch, message, text, file, future):
        from .diagnostics import REQUEST, TASK
        observation = self.observations.get(future, {})
        request_token = REQUEST.set(observation.get("request_id"))
        task_token = TASK.set(observation.get("task_id"))
        observed = None
        anchor = None
        claimed = False
        try:
            if future.cancelled():
                return
            if epoch != self.app.tasks.epoch or not self.app.online:
                raise ConnectionError("连接状态变化，旧消息不再补发")
            self.app.diagnostics.end(observation.get("queue_span"))
            observed = self.app.diagnostics.start("delivery", "发送附件" if file else "发送文字", {"delivery_id": observation.get("id"), "file_id": file})
            self.app.store.execute("UPDATE deliveries SET status='sending' WHERE id=?", (observation.get("id"),))
            anchor = (epoch, message.bot, message.scope, message.reply_to or message.id)
            message.quote_reply = not message.schedule_id and anchor not in self.replied and anchor not in self.reply_claims
            if message.quote_reply:
                self.reply_claims.add(anchor)
                claimed = True
            result = await self.app.astrbot.send(message, text, file)
            if epoch != self.app.tasks.epoch or not self.app.online:
                raise ConnectionError("发送期间连接变化，送达状态未确认，不自动重发")
            if message.quote_reply:
                self.replied.add(anchor)
                if len(self.replied) > 2048:
                    self.replied = {anchor}
            if text or file:
                attachments = []
                if file:
                    record = self.app.store.one("SELECT id,name,kind FROM files WHERE id=?", (file,))
                    if record:
                        attachments.append({"file_id": record["id"], "name": record["name"], "kind": record["kind"]})
                raw_id = str(result.get("message_id") or "") if isinstance(result, dict) else ""
                ident = message.bot+":"+raw_id if raw_id.isdigit() else uid("out_")
                if self.app.store.one("SELECT id FROM messages WHERE id=?", (ident,)):
                    ident = uid("out_")
                quoted = result.get("quoted") if isinstance(result, dict) else None
                self.app.store.insert("messages", {"id": ident, "bot": message.bot, "scope": message.scope, "sender": self.app.config.get("account", "qq"), "name": self.app.config.get("app", "name"), "role": "assistant", "text": text or "", "quote_id": (message.reply_to or message.id) if message.quote_reply and quoted is not False else "", "topic": message.topic, "private": int(message.private), "payload": json.dumps({"attachments": attachments, "delivery_status": "sent", "qq_message_id": raw_id, "quoted": quoted, "reply_to": message.reply_to or message.id}, ensure_ascii=False), "created": time.time()})
                if observation.get("request_id"):
                    self.app.store.execute("INSERT OR IGNORE INTO request_messages VALUES(?,?)", (observation["request_id"], ident))
            self.app.store.execute("UPDATE deliveries SET status='sent',finished=?,qq_message_id=? WHERE id=?", (time.time(), str((result or {}).get("message_id", "")) if isinstance(result, dict) else "", observation.get("id")))
            self.app.diagnostics.end(observed, detail={"acknowledged": True})
            self.app.diagnostics.recover_delivery(file, message.bot, message.scope)
            if not future.done():
                future.set_result(True)
        except asyncio.CancelledError:
            self.app.store.execute("UPDATE deliveries SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'unknown' END,error='发送中断，请核对是否送达',finished=? WHERE id=?", (time.time(), observation.get("id")))
            self.app.diagnostics.end(observed or observation.get("queue_span"), "cancelled")
            if not future.done():
                future.set_exception(ConnectionError("发送已中断，送达状态未确认，不自动重发"))
            raise
        except Exception as exc:
            import re
            uncertain = bool(re.search(r"未确认|超时|timeout|timed out|中断|连接变化", str(exc), re.I)) or isinstance(exc, (httpx.HTTPError, ConnectionError))
            self.app.store.execute("UPDATE deliveries SET status=?,error=?,finished=? WHERE id=?", ("unknown" if uncertain else "failed", str(exc)[:1500], time.time(), observation.get("id")))
            self.app.diagnostics.end(observed or observation.get("queue_span"), "unknown" if uncertain else "failed", error=exc)
            self.app.store.event("delivery", "QQ消息发送失败", {"bot": message.bot, "scope": message.scope,
                "name": self.app.config.get("app", "name"), "sender": self.app.config.get("account", "qq"),
                "text": text or "[附件]", "quote_id": message.reply_to or message.id, "error": str(exc)[:300]}, "error")
            if not future.done():
                future.set_exception(exc)
        finally:
            if claimed:
                self.reply_claims.discard(anchor)
            TASK.reset(task_token)
            REQUEST.reset(request_token)
