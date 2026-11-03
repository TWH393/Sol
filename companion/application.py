"""Application composition root. Every public capability has one owner."""
from __future__ import annotations

import asyncio
import json
import time

from .backup import Backups
from .config import Config, DATA
from .storage import Store, uid
from .memory import Memory, LocalAssistant, Personality
from .models import Models
from .tasks import Tasks
from .files import Files
from .runtime import AstrBotRuntime, Services, Delivery
from .agent import Chat
from .extensions import Extensions
from .tools import Registry
from .tools.python import PythonSessions
from .tools.media import Media
from .tools.documents import Documents
from .tools.web import WebTools
from .tools.native import NativeTools
from .tools.control import Control
from .tools.browser import PublicBrowser
from .tools.mcp import McpTools
from .document_index import DocumentIndex
from .domain import Message
from .schedules import Schedules
from .diagnostics import Diagnostics
from .artifacts import Artifacts
from .recovery import Recovery


class Application:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        Backups.apply_restore(DATA)
        self.config = Config(DATA)
        self.store = Store(DATA)
        self.diagnostics = Diagnostics(self.store)
        self.store.event_observer = self.diagnostics.observe_event
        self.started = time.time()
        self.online = False
        self.startup_stage = "正在准备内部服务"
        self.startup_error = ""
        self.qq_startup_error = ""
        self.stopping = False
        self.connection_token = None
        self.background = set()
        self.local = LocalAssistant(self.config)
        self.local.diagnostics = self.diagnostics
        self.memory = Memory(self.store, self.config, self.local)
        self.memory.diagnostics = self.diagnostics
        self.personality = Personality(self.store, self.config.get("account", "id"))
        self.models = Models(self.config, self.store)
        self.models.diagnostics = self.diagnostics
        self.tasks = Tasks(self.store, self.config)
        self.tasks.diagnostics = self.diagnostics
        self.models.task_check = self.tasks.check
        self.models.resources = self.tasks.resources
        self.tasks.resource_hook = self.local.pause_for_gpu
        self.files = Files(self.store, self.config)
        self.artifacts = Artifacts(self)
        self.recovery = Recovery(self)
        self.astrbot = AstrBotRuntime(self)
        self.services = Services(self)
        self.models.plan_ready = self.services.plan_credentials
        self.delivery = Delivery(self)
        self.schedules = Schedules(self)
        self.tasks.schedule_cancel = self.schedules.cancel
        self.python = PythonSessions(self)
        self.media = Media(self)
        self.documents = Documents(self)
        self.native = NativeTools(self)
        self.webtools = WebTools(self)
        self.control = Control(self)
        self.extensions = Extensions(self)
        self.document_index = DocumentIndex(self)
        self.browser = PublicBrowser(self)
        self.mcp = McpTools(self)
        self.backups = Backups(self)
        self.registry = Registry()
        for module in (self.python, self.media, self.documents, self.webtools, self.control, self.native, self.browser, self.mcp, self.artifacts):
            module.register(self.registry)
        self.chat = Chat(self)

    def background_task(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        def record_failure(done):
            if done.cancelled():
                return
            error = done.exception()
            if error and not self.stopping:
                if name == "application-startup":
                    self.startup_error = str(error)
                self.store.event("background", "后台工作未完成："+name, {"error": str(error)}, "error")
        task.add_done_callback(record_failure)
        return task

    async def start(self):
        self.startup_stage = "正在启动本地辅助服务"
        self.tasks.recover()
        self.diagnostics.recover()
        self.schedules.recover()
        self.tasks.accepting = False
        try:
            await self.services.start_local()
        except Exception as exc:
            self.store.event("service", "本地辅助模型未启动", {"error": str(exc)}, "warning")
        self.delivery.worker = self.background_task(self.delivery.run(), "delivery")
        self.delivery.file_worker = self.background_task(self.delivery.run(files=True), "file-delivery")
        self.background_task(self.memory.maintain(), "memory-maintenance")
        self.background_task(self.maintenance(), "maintenance")
        self.background_task(self.document_index.maintain(), "document-index")
        self.startup_stage = "正在启动消息核心"
        await self.astrbot.start()
        self.startup_stage = "正在连接 QQ"
        if self.astrbot.context is not None:
            self.background_task(self.personality.reflect_on_start(self.local), "personality-startup")
        if self.config.get("account", "enabled"):
            if self.config.get("account", "driver") == "native":
                if self.config.get("account", "auto_login"):
                    # Keep the UI/engine startup responsive. The owned QQ
                    # service makes one attempt and reports a manual-login state.
                    self.background_task(self.start_qq_once(), "qq-startup-login")
                else:
                    self.store.event("service", "QQ自动登录已关闭，等待手动启动")
            else:
                try:
                    await self.services.qq("start")
                except Exception as exc:
                    self.store.event("service", "QQ 服务尚未就绪", {"error": str(exc)}, "warning")
        if any(p.get("kind") == "plan" for p in self.config.get("models", "providers")):
            try:
                await self.services.start_bridge()
            except Exception as exc:
                self.store.event("service", "套餐桥未启动", {"error": str(exc)}, "warning")
        self.store.event("application", "同伴已启动", {"version": "0.1.0"})

    async def start_qq_once(self):
        self.qq_startup_error = ""
        try:
            await self.services.qq("start")
        except Exception as exc:
            self.qq_startup_error = str(exc)
            self.store.event("service", "QQ启动连接未完成，请查看登录面板", {"error": str(exc)}, "warning")

    async def connected(self):
        if self.online or self.stopping or self.astrbot.task is None or self.astrbot.task.done():
            return
        self.online = True
        self.tasks.accepting = True
        self.schedules.resume()
        self.store.event("connection", "机器人 QQ 已连接")
        self.background_task(self.send_reports(), "administrator-reports")

    async def disconnected(self, reason):
        was_online = self.online
        self.online = False
        await self.schedules.suspend()
        await self.delivery.interrupt()
        await self.chat.interrupt()
        await self.tasks.interrupt_all(reason)
        await self.astrbot.interrupt_plugin_handlers()
        for ident in list(self.python.sessions):
            await self.python.release(ident)
        for ident in list(self.native.containers):
            await self.native.release(ident)
        for ident in list(self.browser.sessions):
            await self.browser.release(ident)
        if was_online:
            self.store.event("connection", "QQ 已断联，执行任务中断，未来安排保留", {"reason": reason}, "error")

    async def send_reports(self):
        admins = self.config.get("account", "admins")
        if not admins:
            return
        rows = self.store.rows("SELECT * FROM reports WHERE delivered=0 ORDER BY created")
        if not rows:
            return
        for row in rows:
            for admin in admins:
                key = "report:"+row["id"]+":"+admin
                if self.store.checkpoint(key):
                    continue
                message = Message(uid("report_"), row["bot"], "private:"+admin, admin, "管理员", "管理通知", True)
                try:
                    await self.delivery.text(message, row["text"])
                    self.store.checkpoint(key, True)
                except Exception:
                    return
            self.store.execute("UPDATE reports SET delivered=1 WHERE id=?", (row["id"],))

    async def maintenance(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self.python.cleanup()
                await self.native.cleanup()
                await asyncio.to_thread(self.files.cleanup)
            except Exception as exc:
                self.store.event("cleanup", "资源清理未完成，下次周期重试", {"error": str(exc)}, "warning")
            last = self.store.checkpoint("last_backup") or 0
            if time.time()-last > self.config.get("app", "backup_hours")*3600:
                try:
                    await self.backups.create("scheduled")
                except Exception as exc:
                    self.store.event("backup", "自动备份失败", {"error": str(exc)}, "error")
            # Quiet-group initiation is opt-in and remains group-only.
            if not self.online:
                continue
            scopes = self.store.rows("SELECT scope,MAX(created) AS last FROM messages WHERE private=0 GROUP BY scope")
            for row in scopes:
                override = self.config.get("access")["group_overrides"].get(row["scope"], {})
                if not override.get("idle_topics", self.config.get("social", "idle_topics")) or not self.chat.can_participate(row["scope"]):
                    continue
                last_idle = self.store.checkpoint("idle:"+row["scope"]) or 0
                if time.time()-row["last"] < 1800 or time.time()-last_idle < 7200:
                    continue
                recent = self.store.rows("SELECT text,sender FROM messages WHERE scope=? AND role='user' ORDER BY created DESC LIMIT 12", (row["scope"],))
                decision = await self.local.json("判断是否有适合在群里主动开启的话题。没有合适内容返回{reply:false}；有则返回{reply:true,text:一句话题方向}。不要编造个人经历，不使用私聊内容。", recent, background=True)
                if decision and decision.get("reply"):
                    if not self.chat.can_participate(row["scope"], mark=True):
                        continue
                    record = self.store.one("SELECT * FROM messages WHERE scope=? ORDER BY created DESC LIMIT 1", (row["scope"],))
                    message = Message(uid("idle_"), record["bot"], row["scope"], "@robot", "同伴", decision["text"], topic=uid("topic_"), origin=json.loads(record["payload"]).get("origin", ""), autonomous=True)
                    self.store.checkpoint("idle:"+row["scope"], time.time())
                    self.background_task(self.chat.run(message), "idle-topic")

    async def stop(self):
        if self.stopping:
            return
        self.stopping = True
        await self.disconnected("软件停止")
        for task in list(self.background):
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        await self.astrbot.stop()
        await self.services.stop()
        self.store.checkpoint("clean_shutdown", time.time())
        self.store.close()
