"""Sol's isolated official-QQ adapter prototype. No LLBot process or external license."""
from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import aiohttp
import httpx
import psutil

from .config import ROOT
from .qq_client import prepare_client


class NativeQQ:
    def __init__(self, app):
        self.app = app
        self.root = app.store.root / "qq"
        self.root.mkdir(exist_ok=True)
        self.process = None
        self.bridge = None
        self.stage = "自研QQ接入原型尚未启动"
        self.cache = None
        self.cached_at = 0
        self.media_cache = {}
        self.outgoing_staged = set()
        self.lifecycle_lock = asyncio.Lock()

    async def rpc(self, route, payload=None):
        timeout = 60 if route == "media" else 30 if route == "qrcode" else 135 if route == "send" else 5
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            response = await client.request("GET" if payload is None else "POST", "http://127.0.0.1:24766/"+route,
                                            headers={"X-Sol-QQ": self.app.config.vault.ensure("native_qq_rpc")}, json=payload)
            data = response.json()
            if not response.is_success:
                raise RuntimeError(data.get("error") or "本地QQ接口未响应")
            if route == "status":
                if Path(data.get("profile", "")).resolve() != (self.root / "sol-profile").resolve():
                    raise RuntimeError("QQ资料目录未隔离，Sol拒绝接入")
                owner = Path(psutil.Process(data["pid"]).exe()).resolve()
                if not owner.is_relative_to((self.root / "sol-client").resolve()):
                    raise RuntimeError("QQ进程不属于Sol独立副本，拒绝接入")
                if data.get("expected") != self.app.config.get("account", "qq"):
                    raise RuntimeError("指定QQ配置已变化，请先停止QQ服务再重新启动；不会接管旧账号")
            return data

    async def status(self):
        base = {"driver": "native", "expected": self.app.config.get("account", "qq"), "started": False,
                "online": False, "connected": False, "actual": "", "error": "", "phase": "stopped", "implementation": "prototype"}
        if self.process is None:
            return {**base, "stage": self.stage}
        if self.process.returncode is not None:
            return {**base, "phase": "error", "error": f"独立QQ进程已退出（{self.process.returncode}），请查看QQ组件日志"}
        base["started"] = True
        try:
            if self.cache is None or time.monotonic()-self.cached_at > 1:
                self.cache = await self.rpc("status")
                self.cached_at = time.monotonic()
            data = self.cache
            error = data.get("error", "")
            if data.get("actual") and data["actual"] != base["expected"]:
                error = "实际QQ与指定机器人不符，已拒绝接收和发送"
            return {**base, "actual": data.get("actual", ""), "online": bool(data.get("online")) and not error,
                    "connected": bool(data.get("online")) and self.app.online and not error,
                    "error": error, "stage": data.get("stage", self.stage), "phase": data.get("phase", "initializing"),
                    "received": data.get("received", 0), "sent": data.get("sent", 0)}
        except (httpx.HTTPError, ValueError, RuntimeError, psutil.Error) as exc:
            return {**base, "error": "本地QQ接入模块尚未就绪："+str(exc)[:200]}

    async def start(self):
        async with self.lifecycle_lock:
            return await self._start()

    async def _start(self):
        qq = self.app.config.get("account", "qq")
        if not qq or not qq.isascii() or not qq.isdigit():
            raise ValueError("请先保存指定机器人QQ号")
        client = self.root / "sol-client"
        if not (client / "sol-client.json").is_file():
            self.stage = "首次准备独立QQ组件，仅复制本机程序文件，可能需要一些时间"
            try:
                await asyncio.to_thread(prepare_client, self.root)
            except OSError as exc:
                raise RuntimeError("独立QQ组件准备失败："+str(exc)) from exc
        if self.process is None or self.process.returncode is not None:
            if any(c.laddr and c.laddr.port == 24766 and c.status == "LISTEN" for c in psutil.net_connections("tcp")):
                raise RuntimeError("Sol QQ接入端口已有进程监听，未接管其他实例")
            launcher = ROOT / "runtime/qq_native/sol-qq-launcher.exe"
            if not launcher.is_file():
                raise FileNotFoundError("Sol本地QQ加载组件尚未构建")
            self.stage = "正在初始化独立QQ客户端"
            self.cache = None
            self.cached_at = 0
            env = {**os.environ, "SOL_QQ_RPC_KEY": self.app.config.vault.ensure("native_qq_rpc"), "SOL_QQ_LEASE": "1",
                   "SOL_QQ_AUTO_LOGIN": "1" if self.app.config.get("account", "auto_login") else "0"}
            env.pop("SOL_DESKTOP_TOKEN", None)
            with (self.root / "native-client.log").open("ab") as log:
                self.process = await asyncio.create_subprocess_exec(str(launcher), str(client / "QQ.exe"), str(self.root / "sol-profile"), qq,
                    stdin=asyncio.subprocess.PIPE, stdout=log, stderr=log, env=env, creationflags=0x08000000 if os.name == "nt" else 0)
        deadline = time.monotonic()+60
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                raise RuntimeError(f"Sol QQ加载器已退出（{self.process.returncode}），请查看QQ组件日志")
            state = await self.status()
            if self.cache is not None:
                if state["phase"] == "error":
                    raise RuntimeError(state["error"])
                self.stage = state.get("stage", "独立QQ已启动，等待扫码")
                if self.bridge is None or self.bridge.done():
                    self.bridge = self.app.background_task(self.relay(), "native-qq-relay")
                return state
            await asyncio.sleep(1)
        raise RuntimeError("QQ加载器已启动，接入模块仍未就绪；请查看QQ组件日志，不会自动重复启动")

    async def qrcode(self):
        if self.process is None or self.process.returncode is not None:
            raise ValueError("请先点击启动并扫码登录")
        data = await self.rpc("qrcode", {})
        if data.get("expected") != self.app.config.get("account", "qq"):
            raise RuntimeError("二维码所属接入实例与指定QQ不符")
        return data

    async def action(self, message):
        action, params = message.get("action"), message.get("params") or {}
        state = await self.status()
        if action == "get_login_info":
            if not state["online"]:
                raise RuntimeError("指定QQ尚未登录")
            return {"user_id": int(state["actual"]), "nickname": "Sol"}
        if action == "get_status":
            return {"online": state["online"], "good": state["online"]}
        if action == "get_version_info":
            return {"app_name": "SolQQ", "app_version": "prototype", "protocol_version": "v11"}
        if action == "get_msg":
            return await self.rpc("message", {"id": str(params.get("message_id") or "")})
        if action in {"send_private_msg", "send_group_msg", "send_msg"}:
            segments = params.get("message", "")
            group = action == "send_group_msg" or "group_id" in params
            return await self.send_segments(group, str(params.get("group_id" if group else "user_id", "")), segments)
        if action in {"upload_group_file", "upload_private_file"}:
            group = action == "upload_group_file"
            return await self.send_segments(group, str(params.get("group_id" if group else "user_id", "")),
                [{"type": "file", "data": {"file": params.get("file"), "name": params.get("name")}}])
        raise ValueError("自研QQ原型尚未支持此接口："+str(action))

    async def send_message(self, message, ident=None, text=None):
        permit = None
        if message.schedule_id:
            arrangement = self.app.schedules.get(message.schedule_id)
            run_id = message.id.removeprefix(message.bot+":")
            run = self.app.store.one("SELECT id FROM future_runs WHERE id=? AND schedule_id=? AND status='sending'", (run_id, arrangement["id"]))
            if (not run or arrangement["status"] != "active" or arrangement["bot"] != message.bot
                    or arrangement["scope"] != message.scope or arrangement["qq"] != self.app.config.get("account", "qq")):
                raise PermissionError("未来提醒的目标授权已失效，未提交发送")
            permit = json.loads(arrangement["peer"])
        segments = [{"type": "text", "data": {"text": " ".join("@"+str(q) for q in message.notify)+" "}}] if message.notify else []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        if ident:
            row, path = self.app.files.resolve(ident, message)
            kind = "image" if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"} else "file"
            segments.append({"type": kind, "data": {"file": str(path), "name": row["name"]}})
        anchor = (message.reply_to or message.id).split(":", 1)[-1] if message.quote_reply else ""
        return await self.send_segments(not message.private, message.scope.split(":", 1)[1], segments, reply_to=anchor if anchor.isdigit() else "", persistent_peer=permit)

    def outbound_media(self, kind, data):
        from urllib.parse import urlsplit
        from urllib.request import url2pathname
        source = str(data.get("file") or "")
        maximum = self.app.config.get("files", "max_mb")*1024*1024
        staging = (self.root / "outgoing").resolve()
        if source.startswith("base64://"):
            if kind != "image" or len(source)-9 > ((maximum+2)//3)*4:
                raise ValueError("QQ图片内容超过当前文件大小限制或类型不支持")
            content = base64.b64decode(source[9:], validate=True)
            if not content or len(content) > maximum:
                raise ValueError("QQ图片为空或超过当前文件大小限制")
            staging.mkdir(exist_ok=True)
            source = str(staging / (uuid.uuid4().hex+".image"))
            self.outgoing_staged.add(Path(source))
            Path(source).write_bytes(content)
        elif source.startswith("file:"):
            url = urlsplit(source)
            if url.netloc not in {"", "localhost"}:
                raise ValueError("QQ附件不接受网络共享路径")
            source = url2pathname(url.path)
        if not source or "://" in source or not Path(source).is_absolute():
            raise ValueError("QQ附件需要文件库中的本地文件；请先下载或归档")
        path = Path(source).resolve(strict=True)
        if not any(path.is_relative_to(root) for root in (self.app.files.root.resolve(), staging)):
            raise PermissionError("QQ附件不在Sol文件库或本次发送暂存目录，已拒绝读取")
        size = path.stat().st_size
        if not path.is_file() or not 0 < size <= maximum:
            raise ValueError("QQ附件为空或超过当前文件大小限制")
        digest = hashlib.md5()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b""):
                digest.update(chunk)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(data.get("name") or path.name))[:180].strip(" .") or "附件"
        element = {"type": kind, "path": str(path), "name": name, "size": size, "md5": digest.hexdigest()}
        if kind == "image":
            from PIL import Image
            with Image.open(path) as image:
                types = {"JPEG": (1000, ".jpg"), "PNG": (1001, ".png"), "WEBP": (1002, ".webp"), "BMP": (1005, ".bmp"), "GIF": (2000, ".gif")}
                if image.format not in types or not 0 < image.width*image.height <= 40000000:
                    raise ValueError("QQ图片格式不支持或超过4000万像素；可作为文件发送")
                element.update(width=image.width, height=image.height, pic_type=types[image.format][0])
                if path.suffix == ".image":
                    element["name"] = "图片"+types[image.format][1]
                image.verify()
        return element

    async def send_segments(self, group, target, segments, *, reply_to="", persistent_peer=None):
        if not target.isascii() or not target.isdigit():
            raise ValueError("QQ发送目标无效")
        segments = segments if isinstance(segments, list) else [{"type": "text", "data": {"text": str(segments)}}]
        if not 1 <= len(segments) <= 32:
            raise ValueError("QQ单条消息需要1至32个内容段")
        elements = []
        for segment in segments:
            kind, data = segment.get("type"), segment.get("data") or {}
            if kind in {"text", "at"}:
                text = str(data.get("text") or "") if kind == "text" else "@"+str(data.get("qq") or "")+" "
                if text:
                    elements.append({"type": "text", "text": text})
            elif kind in {"image", "file"}:
                elements.append(await asyncio.to_thread(self.outbound_media, kind, data))
            elif kind == "reply":
                reply_to = str(data.get("id") or "")
                if not reply_to.isdigit():
                    raise ValueError("QQ引用消息编号无效")
            else:
                raise ValueError("Sol QQ尚不支持此消息段："+str(kind))
        result = await self.rpc("send", {"group": bool(group), "target": target, "elements": elements,
            "persistent_peer": persistent_peer,
            "reply_to": reply_to, "send_interval": self.app.config.get("chat", "send_interval"),
            "max_bytes": self.app.config.get("files", "max_mb")*1024*1024})
        # Keep staging on uncertain/failed sends; only confirmed sends may release it.
        for element in elements:
            path = Path(element.get("path", ""))
            if path in self.outgoing_staged:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                else:
                    self.outgoing_staged.discard(path)
        return result

    async def archive_media(self, item):
        """Save received originals through the shared file authority, once per event."""
        if item["key"] in self.media_cache:
            return self.media_cache[item["key"]]
        from .domain import Message
        bot = self.app.config.get("account", "id")
        scope = ("group:"+str(item["group"])) if item["group"] else "private:"+str(item["sender"])
        owner = Message(f"{bot}:{item['id']}", bot, scope, str(item["sender"]), item["name"], item["text"], private=not item["group"])
        if not self.app.chat.allowed(owner):
            return []
        maximum = self.app.config.get("files", "max_mb")*1024*1024
        attachments = []
        for media in item.get("media", [])[:17]:
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(media.get("name") or "附件"))[:220].strip(" .") or "附件"
            attachment = {"name": name, "kind": "image" if media.get("kind") == "image" else "file", "native_id": media.get("token") or "overflow", "native": True}
            try:
                if media.get("error"):
                    raise ValueError(media["error"])
                result = await self.rpc("media", {"token": media["token"], "max_bytes": maximum})
                path = Path(result["path"]).resolve(strict=True)
                if not path.is_relative_to((self.root / "sol-profile").resolve()):
                    raise ValueError("QQ附件不在Sol独立资料目录，已拒绝读取")
                def read_original():
                    with path.open("rb") as source:
                        data = source.read(maximum+1)
                    if not data or len(data) > maximum:
                        raise ValueError("QQ附件为空或超过当前大小限制")
                    suffix = None
                    if attachment["kind"] == "image":
                        import io
                        from PIL import Image
                        with Image.open(io.BytesIO(data)) as image:
                            suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp", "TIFF": ".tiff"}.get(image.format)
                            image.verify()
                        if not suffix:
                            raise ValueError("QQ图片格式暂不支持预览")
                    return data, suffix
                data, suffix = await asyncio.to_thread(read_original)
                if suffix and Path(name).suffix.lower() not in ({".jpg", ".jpeg"} if suffix == ".jpg" else {suffix}):
                    name = Path(name).stem+suffix
                    attachment["name"] = name
                attachment["file_id"] = self.app.files.add(data, name, owner, metadata={"source": "qq-native", "message_id": owner.id, "kind": attachment["kind"]})
                if result.get("temporary") and path.parent == (self.root / "sol-profile/sdk/incoming").resolve():
                    # Only the completed staging file created for this request;
                    # never delete QQ's cache or the shared file-library copy.
                    with suppress(OSError):
                        await asyncio.to_thread(path.unlink, missing_ok=True)
            except Exception as exc:
                attachment["error"] = str(exc)[:240]
                self.app.store.event("attachment", "QQ附件保存失败", {"message": owner.id, "name": name, "error": attachment["error"]}, "warning")
            attachments.append(attachment)
        self.media_cache[item["key"]] = attachments
        if len(self.media_cache) > 256:
            self.media_cache.pop(next(iter(self.media_cache)))
        return attachments

    async def relay(self):
        while self.process and self.process.returncode is None:
            state = await self.status()
            if not state["online"]:
                await asyncio.sleep(2)
                continue
            qq = state["expected"]
            headers = {"Authorization": "Bearer "+self.app.config.vault.ensure("onebot"), "X-Self-ID": qq, "X-Client-Role": "Universal"}
            try:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    async with session.ws_connect(f"http://127.0.0.1:{self.app.config.get('app','port')}/onebot", headers=headers) as ws:
                        lock = asyncio.Lock()
                        media_slots, pending, stream_tails = asyncio.Semaphore(2), {}, {}
                        async def emit(data):
                            async with lock:
                                await ws.send_json(data)
                        async def dispatch(item, attachments=None):
                            markers = "\n".join(("[图片：" if a["kind"] == "image" else "[文件：")+a["name"]+"]" for a in attachments or [])
                            text = "\n".join(t for t in (item["text"], markers) if t)
                            segments = list(item.get("segments") or [])
                            if isinstance(item.get("quote"), dict):
                                # Native provenance already accompanies this event. A generic
                                # OneBot reply segment makes AstrBot call get_msg again before
                                # capture, which can stall an otherwise complete quote for 180s.
                                # Runtime restores the plugin Reply component from this same
                                # source and the shared same-scope archive, without that RPC.
                                segments = [segment for segment in segments if segment.get("type") != "reply"]
                            if markers:
                                segments.append({"type": "text", "data": {"text": "\n"+markers}})
                            event = {"time": item["created"], "self_id": int(qq), "post_type": "message", "message_id": item["id"], "user_id": int(item["sender"]),
                                "message_type": "group" if item["group"] else "private", "sub_type": "normal" if item["group"] else "friend", "message": segments or [{"type": "text", "data": {"text": text}}],
                                "raw_message": text, "font": 0, "sender": {"user_id": int(item["sender"]), "nickname": item["name"]}, "sol_native_attachments": attachments or [], "sol_native_reply": item.get("quote")}
                            if item["group"]:
                                event["group_id"] = int(item["group"])
                            await emit(event)
                            await self.rpc("events/ack", {"keys": [item["key"]]})
                        async def ordered_event(item, stream, previous):
                            try:
                                if previous:
                                    await previous
                                if item.get("media"):
                                    async with media_slots:
                                        await dispatch(item, await self.archive_media(item))
                                else:
                                    await dispatch(item)
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                self.app.store.event("attachment", "QQ附件消息交付中断", {"message": item["id"], "error": str(exc)[:240]}, "warning")
                                await ws.close()
                            finally:
                                pending.pop(item["key"], None)
                                if stream_tails.get(stream) is asyncio.current_task():
                                    stream_tails.pop(stream, None)
                        async def events():
                            last_heartbeat = 0
                            while True:
                                current = await self.status()
                                if not current["online"]:
                                    await ws.close()
                                    return
                                if time.monotonic()-last_heartbeat > 10:
                                    await emit({"time": int(time.time()), "self_id": int(qq), "post_type": "meta_event", "meta_event_type": "heartbeat", "status": {"online": True, "good": True}, "interval": 10000})
                                    last_heartbeat = time.monotonic()
                                blocked_streams = set()
                                for item in await self.rpc("events"):
                                    stream = (item["group"] or "private", item["sender"])
                                    if stream in blocked_streams:
                                        continue
                                    previous = stream_tails.get(stream)
                                    if item.get("media") or previous:
                                        if item["key"] not in pending and len(pending) < 32:
                                            task = asyncio.create_task(ordered_event(item, stream, previous))
                                            pending[item["key"]] = stream_tails[stream] = task
                                        elif item["key"] not in pending:
                                            blocked_streams.add(stream)
                                    else:
                                        await dispatch(item)
                                await asyncio.sleep(1)
                        async def run_events():
                            try:
                                await events()
                            finally:
                                await ws.close()
                        pump = asyncio.create_task(run_events())
                        actions = set()
                        async def respond(request):
                            try:
                                data = await self.action(request)
                                await emit({"status": "ok", "retcode": 0, "data": data, "echo": request.get("echo")})
                            except Exception as exc:
                                await emit({"status": "failed", "retcode": 1404, "message": str(exc), "echo": request.get("echo")})
                        try:
                            async for received in ws:
                                if received.type != aiohttp.WSMsgType.TEXT:
                                    continue
                                request = json.loads(received.data)
                                # Uploads must not block login/status heartbeats.
                                if request.get("action") in {"get_status", "get_login_info", "get_version_info"}:
                                    await respond(request)
                                elif len(actions) >= 8:
                                    await emit({"status": "failed", "retcode": 1404, "message": "QQ发送操作过多，请稍后手动重发", "echo": request.get("echo")})
                                else:
                                    task = asyncio.create_task(respond(request))
                                    actions.add(task)
                                    task.add_done_callback(actions.discard)
                        finally:
                            for task in list(actions):
                                task.cancel()
                            await asyncio.gather(*list(actions), return_exceptions=True)
                            pump.cancel()
                            with suppress(asyncio.CancelledError):
                                await pump
                            for task in list(pending.values()):
                                task.cancel()
                            await asyncio.gather(*list(pending.values()), return_exceptions=True)
            except (aiohttp.ClientError, httpx.HTTPError, RuntimeError) as exc:
                self.app.store.event("service", "Sol QQ接入连接中断", {"error": str(exc)}, "warning")
            await asyncio.sleep(2)

    async def stop(self):
        async with self.lifecycle_lock:
            await self._stop()

    async def _stop(self):
        if self.bridge:
            self.bridge.cancel()
            with suppress(asyncio.CancelledError):
                await self.bridge
            self.bridge = None
        if self.process and self.process.returncode is None:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.terminate()
                await self.process.wait()
        self.process = None
        self.cache = None
        self.media_cache.clear()
        for path in list(self.outgoing_staged):
            with suppress(OSError):
                path.unlink(missing_ok=True)
        self.outgoing_staged.clear()
        self.stage = "Sol QQ接入已停止"
