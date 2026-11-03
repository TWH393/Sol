"""Durable reminders. Only confirmed future arrangements survive engine shutdown."""
from __future__ import annotations

import asyncio
import json
import math
import time

from .domain import Message
from .storage import uid


class Schedules:
    def __init__(self, app):
        self.app, self.store = app, app.store
        self.wake = asyncio.Event()
        self.worker = None
        self.current = None
        self.current_id = None
        self.online_since = 0
        self.resumed = {}

    @staticmethod
    def validate(text, due, interval, remaining):
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise ValueError("提醒内容需要1至5000个字符")
        if type(due) not in (int, float) or not math.isfinite(due) or due <= time.time():
            raise ValueError("提醒时间必须是未来的有效时间")
        if type(interval) not in (int, float) or not math.isfinite(interval) or (interval != 0 and interval < 60):
            raise ValueError("周期须为0（一次性）或至少60秒")
        if type(remaining) is not int or remaining < 0 or remaining > 1000 or (not interval and remaining != 1):
            raise ValueError("次数需要为1至1000；周期安排可以用0表示持续重复")

    async def create(self, message, seconds, text, target="", interval=0, occurrences=1):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("提醒时间必须在未来")
        due = time.time()+seconds
        self.validate(text, due, interval, occurrences)
        target = target or message.sender
        if message.autonomous:
            raise PermissionError("提醒需要用户明确委托，不能自主创建")
        if target != message.sender and (message.private or target not in message.targets):
            raise PermissionError("提醒其他人需要在本轮群聊中明确 @该对象")
        peer = {}
        if self.app.config.get("account", "driver") == "native":
            peer = await self.app.services.native_qq.rpc("peer", {"scope": message.scope})
        # Store only the destination authority, never the model context/attachments.
        source = {"id": message.id, "bot": message.bot, "scope": message.scope,
                  "sender": message.sender, "name": message.name, "text": text,
                  "private": message.private, "origin": message.origin, "quote_reply": False}
        now = time.time()
        with self.store.lock, self.store.db:
            ident = self.app.tasks.create(message, "提醒："+text[:80], "reminder")
            self.store.db.execute("INSERT INTO future_tasks(id,bot,qq,owner,scope,target,text,next_due,interval,remaining,status,source,peer,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ident, message.bot, self.app.config.get("account", "qq"), message.sender, message.scope,
                 target, text.strip(), due, interval, occurrences, "active", json.dumps(source, ensure_ascii=False), json.dumps(peer), now, now))
            self.store.db.execute("UPDATE tasks SET status='scheduled',stage='等待提醒时间',deadline=NULL,updated=? WHERE id=?", (now, ident))
        self.wake.set()
        return ident

    def recover(self):
        # A crash between submission and acknowledgement cannot be retried safely.
        for run in self.store.rows("SELECT * FROM future_runs WHERE status='sending'"):
            self.settle(run, "unknown", "上次发送中断，是否送达未确认；不会自动重复发送")
        self.store.execute("UPDATE tasks SET status='scheduled',deadline=NULL WHERE id IN (SELECT id FROM future_tasks WHERE status IN ('active','paused','needs_review'))")

    def cancel(self, ident):
        self.store.execute("UPDATE future_tasks SET status='cancelled',updated=? WHERE id=? AND status IN ('active','paused','needs_review')", (time.time(), ident))
        if self.current_id == ident and self.current and not self.current.done():
            self.current.cancel()
        self.wake.set()

    def resume(self):
        self.online_since = time.time()
        if self.worker is None or self.worker.done():
            self.worker = self.app.background_task(self.run(), "future-arrangements")
        self.wake.set()

    async def suspend(self):
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None

    def get(self, ident):
        row = self.store.one("SELECT * FROM future_tasks WHERE id=? AND bot=?", (ident, self.app.config.get("account", "id")))
        if not row:
            raise ValueError("未来安排不存在")
        return row

    def listing(self):
        return self.store.rows("SELECT id,owner,scope,target,text,next_due,interval,remaining,status,error,created,updated FROM future_tasks WHERE bot=? ORDER BY CASE WHEN status IN ('active','paused','needs_review') THEN 0 ELSE 1 END,next_due,created DESC", (self.app.config.get("account", "id"),))

    async def change(self, ident, action, changes=None):
        row = self.get(ident)
        if action not in {"pause", "resume", "edit", "cancel"}:
            raise ValueError("安排操作无效")
        if row["status"] not in {"active", "paused"} and action != "cancel":
            raise ValueError("此安排已结束或发送状态待核对；请核对记录后重新设置")
        if action == "edit":
            data = changes or {}
            self.validate(data.get("text", row["text"]), data.get("next_due", row["next_due"]), data.get("interval", row["interval"]), data.get("remaining", row["remaining"]))
        if self.current_id == ident and self.current and not self.current.done():
            raise ValueError("此提醒正在提交发送，请等本次发送结束后再操作")
        if action == "cancel":
            self.app.tasks.cancel(ident)
        elif action == "edit":
            self.store.execute("UPDATE future_tasks SET text=?,next_due=?,interval=?,remaining=?,error='',updated=? WHERE id=?",
                (data.get("text", row["text"]).strip(), data.get("next_due", row["next_due"]), data.get("interval", row["interval"]), data.get("remaining", row["remaining"]), time.time(), ident))
        else:
            self.store.execute("UPDATE future_tasks SET status=?,updated=? WHERE id=?", ("active" if action == "resume" else "paused", time.time(), ident))
            # Paused elapsed occurrences use the same catch-up policies as offline time.
            if action == "resume":
                self.resumed[ident] = time.time()
        self.wake.set()
        return self.get(ident)

    def claim(self, row, count):
        ident, now = uid("fr_"), time.time()
        with self.store.lock, self.store.db:
            current = self.store.one("SELECT status,next_due FROM future_tasks WHERE id=?", (row["id"],))
            if not current or current["status"] != "active" or current["next_due"] != row["next_due"]:
                return None
            self.store.db.execute("INSERT INTO future_runs(id,schedule_id,due,count,status,started) VALUES(?,?,?,?,?,?)", (ident, row["id"], row["next_due"], count, "sending", now))
        return {"id": ident, "schedule_id": row["id"], "due": row["next_due"], "count": count}

    def settle(self, run, status, error=""):
        now = time.time()
        with self.store.lock, self.store.db:
            row = self.store.one("SELECT * FROM future_tasks WHERE id=?", (run["schedule_id"],))
            if not row:
                return
            changed = self.store.db.execute("UPDATE future_runs SET status=?,error=?,finished=? WHERE id=? AND status='sending'", (status, error, now, run["id"])).rowcount
            if not changed:
                return
            remaining = max(0, row["remaining"]-run["count"]) if row["remaining"] else 0
            exhausted = not row["interval"] or bool(row["remaining"] and not remaining)
            state = ("needs_review" if status == "unknown" else "completed" if status == "sent" else "skipped") if exhausted else "active"
            if row["status"] == "cancelled":
                state = "cancelled"
            due = None if exhausted else run["due"]+row["interval"]*run["count"]
            # If sending took longer than a cycle, treat those slots as missed next loop.
            self.store.db.execute("UPDATE future_tasks SET next_due=?,remaining=?,status=?,error=?,updated=? WHERE id=?", (due, remaining, state, error, now, row["id"]))
            task_state = "scheduled" if state in {"active", "paused", "needs_review"} else "cancelled" if state == "cancelled" else "completed"
            stage = {"active": "等待下一次提醒", "needs_review": "发送状态待核对", "skipped": "错过的提醒已跳过", "cancelled": "已取消", "completed": "提醒已送达"}.get(state, state)
            self.store.db.execute("UPDATE tasks SET status=?,stage=?,error=?,updated=? WHERE id=?", (task_state, stage, error or None, now, row["id"]))

    async def dispatch(self, row):
        now = time.time()
        count = int((now-row["next_due"])//row["interval"])+1 if row["interval"] else 1
        if row["remaining"]:
            count = min(count, row["remaining"])
        missed = row["next_due"] < max(self.online_since, self.resumed.get(row["id"], 0)) or (row["interval"] and count > 1)
        policy = self.app.config.get("reminders", "catch_up_recurring" if row["interval"] else "catch_up_once")
        # Do not submit to a different robot after account settings change.
        if row["qq"] != self.app.config.get("account", "qq"):
            self.store.execute("UPDATE future_tasks SET status='paused',error='机器人账号已改变，安排已暂停',updated=? WHERE id=?", (now, row["id"]))
            return
        message = Message(**json.loads(row["source"]))
        if not self.app.config.get("features", "tasks") or not self.app.chat.allowed(message):
            self.store.execute("UPDATE future_tasks SET status='paused',error='当前使用范围或任务开关已禁止此提醒，安排已暂停',updated=? WHERE id=?", (now, row["id"]))
            return
        run = self.claim(row, count)
        if not run:
            return
        if missed and not policy:
            self.settle(run, "skipped", "已按后台策略跳过错过的提醒")
            return
        try:
            source = json.loads(row["source"])
            source.update(id=row["bot"]+":"+run["id"], text=row["text"], created=now,
                          quote_reply=False, schedule_id=row["id"], notify=[] if source["private"] else [row["target"]])
            message = Message(**source)
            # One occurrence is one QQ bubble, including merged offline catch-up.
            prefix = "补发提醒：" if missed else "提醒："
            await self.app.delivery.enqueue(message, text=prefix+row["text"])
        except asyncio.CancelledError:
            self.settle(run, "unknown", "发送期间连接或服务中断，是否送达未确认；不会自动重复发送")
            raise
        except Exception as exc:
            self.settle(run, "unknown", str(exc)[:700]+"；未自动重发，请核对执行记录")
            self.store.event("reminder", "提醒未确认送达", {"id": row["id"], "error": str(exc)[:700]}, "warning")
            if isinstance(exc, PermissionError):
                self.store.execute("UPDATE future_tasks SET status='paused' WHERE id=? AND status='active'", (row["id"],))
        else:
            self.settle(run, "sent")

    async def run(self):
        try:
            while self.app.online and not self.app.stopping:
                self.wake.clear()
                row = self.store.one("SELECT * FROM future_tasks WHERE bot=? AND status='active' ORDER BY next_due LIMIT 1", (self.app.config.get("account", "id"),))
                delay = max(0, row["next_due"]-time.time()) if row else 30
                if row and delay == 0:
                    self.current_id = row["id"]
                    self.current = asyncio.create_task(self.dispatch(row), name="reminder:"+row["id"])
                    try:
                        await self.current
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                    except Exception as exc:
                        self.store.execute("UPDATE future_tasks SET status='paused',error=?,updated=? WHERE id=?", (str(exc)[:700], time.time(), row["id"]))
                        self.store.event("reminder", "安排调度异常，已暂停", {"id": row["id"], "error": str(exc)}, "error")
                    finally:
                        self.current = None
                        self.current_id = None
                    continue
                try:
                    await asyncio.wait_for(self.wake.wait(), min(delay, 30))
                except TimeoutError:
                    pass
        finally:
            if self.current and not self.current.done():
                self.current.cancel()
                await asyncio.gather(self.current, return_exceptions=True)
