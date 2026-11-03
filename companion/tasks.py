"""Step-level priority scheduling, task-scoped collaboration and interruption."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager

from .domain import Interrupted, QuotaExceeded
from .storage import uid


class Resources:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.waiters = []
        self.active = {}
        self.sequence = 0

    @asynccontextmanager
    async def acquire(self, name, autonomous=False, limit=1):
        async with self.condition:
            self.sequence += 1
            ticket = (10 if autonomous else 0, self.sequence, name)
            self.waiters.append(ticket)
            try:
                await self.condition.wait_for(lambda: self.active.get(name, 0) < limit and ticket == min((t for t in self.waiters if t[2] == name), default=ticket))
            except BaseException:
                self.waiters.remove(ticket)
                self.condition.notify_all()
                raise
            self.waiters.remove(ticket)
            self.active[name] = self.active.get(name, 0)+1
        try:
            yield
        finally:
            async with self.condition:
                self.active[name] -= 1
                self.condition.notify_all()


class Tasks:
    LIVE = ("queued", "running", "waiting_approval", "scheduled")

    def __init__(self, store, config):
        self.store, self.config = store, config
        self.resources = Resources()
        self.handles = {}
        self.cancelled = set()
        self.accepting = True
        self.epoch = 0
        self.reminder_handles = {}
        self.resource_hook = None
        self.schedule_cancel = None
        self.diagnostics = None

    def recover(self):
        rows = self.interrupt_records("软件退出或上次运行中断")
        # -1 means discarded, never falsely marked as delivered. No old notice
        # is sent on startup; memories and task history remain available locally.
        self.store.execute("UPDATE reports SET delivered=-1 WHERE delivered=0")
        return rows

    def interrupt_records(self, reason):
        condition = "status IN ('queued','running','waiting_approval','scheduled') AND id NOT IN (SELECT id FROM future_tasks)"
        rows = self.store.rows("SELECT * FROM tasks WHERE "+condition)
        self.store.execute("UPDATE tasks SET status='interrupted',stage='已中断',error=?,updated=? WHERE "+condition, (reason, time.time()))
        self.store.execute("UPDATE task_steps SET status='interrupted',finished=? WHERE status='running'", (time.time(),))
        self.store.execute("DELETE FROM collaborators WHERE task_id IN (SELECT id FROM tasks WHERE status='interrupted')")
        self.store.execute("UPDATE approvals SET status='expired' WHERE status='pending' AND task_id IN (SELECT id FROM tasks WHERE status='interrupted')")
        if rows:
            text = reason+"，以下任务已中断，不会自动恢复：\n"+"\n".join(f"{r['id']} · {r['title']} · {r['stage']}" for r in rows)
            self.store.insert("reports", {"id": uid("r_"), "bot": "main", "text": text, "created": time.time()})
        return len(rows)

    def create(self, message, title, kind="task", payload=None, selected=None, fingerprint=None):
        if not self.accepting:
            raise Interrupted("QQ 未连接，不能创建新任务")
        pending = self.store.one("SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','running')")["n"]
        if pending >= self.config.get("tasks", "max_pending"):
            raise RuntimeError("任务队列已满，请稍后再试")
        ident, now = uid("t_"), time.time()
        policy = self.config.get("tasks")["type_limits"].get(kind, {})
        self.store.insert("tasks", {"id": ident, "bot": message.bot, "owner": "@robot" if message.autonomous else message.sender, "scope": message.scope,
            "title": title[:150], "kind": kind, "autonomous": int(message.autonomous), "status": "queued", "stage": "等待处理",
            "model": json.dumps(selected or {}), "payload": json.dumps(payload or {}, ensure_ascii=False),
            "created": now, "updated": now, "fingerprint": fingerprint, "deadline": now+policy.get("timeout", self.config.get("tasks", "timeout"))})
        if self.diagnostics:
            self.diagnostics.attach_task(ident)
        return ident

    def get(self, ident):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (ident,))
        if not row:
            raise ValueError("任务不存在")
        return row

    def allowed(self, ident, actor, operation="edit"):
        row = self.get(ident)
        if actor == row["owner"]:
            return True
        approval = self.store.one("SELECT permissions FROM collaborators WHERE task_id=? AND user=?", (ident, actor))
        return bool(approval and operation in json.loads(approval["permissions"]))

    def grant(self, ident, owner, user, permissions):
        row = self.get(ident)
        if row["owner"] != owner or row["status"] not in self.LIVE:
            raise PermissionError("只有当前任务创建者可以授权")
        if set(permissions)-{"edit", "cancel", "files"}:
            raise ValueError("协作权限无效")
        self.store.execute("INSERT INTO collaborators VALUES(?,?,?,?) ON CONFLICT(task_id,user) DO UPDATE SET permissions=excluded.permissions,approved_by=excluded.approved_by", (ident, user, json.dumps(permissions), owner))

    def cancel(self, ident, actor=None):
        if actor and not self.allowed(ident, actor, "cancel"):
            raise PermissionError("需要任务创建者授权才能取消")
        if self.schedule_cancel:
            self.schedule_cancel(ident)
        self.cancelled.add(ident)
        if ident in self.handles:
            self.handles[ident].cancel()
        if ident in self.reminder_handles:
            self.reminder_handles[ident].cancel()
        self.store.execute("UPDATE tasks SET status='cancelled',stage='已取消',updated=? WHERE id=? AND status IN ('queued','running','waiting_approval','scheduled')", (time.time(), ident))
        self.store.execute("DELETE FROM collaborators WHERE task_id=?", (ident,))
        self.store.execute("UPDATE approvals SET status='expired' WHERE task_id=? AND status='pending'", (ident,))

    def check(self, ident):
        row = self.get(ident)
        if ident in self.cancelled or row["status"] in {"interrupted", "cancelled"}:
            raise Interrupted("任务已中断或取消")
        if row["deadline"] and time.time() > row["deadline"]:
            raise TimeoutError("任务达到总执行时限")
        policy = self.config.get("tasks")
        limit = policy["type_limits"].get(row["kind"], {}).get("tokens", policy["autonomous_token_limit" if row["autonomous"] else "token_limit"])
        used = self.store.one("SELECT SUM(input+output) AS n FROM usage WHERE task_id=?", (ident,))["n"] or 0
        if limit and used >= limit:
            raise QuotaExceeded("本次任务达到累计用量上限，已有产物已保留")
        return row

    def progress(self, ident, stage, status="running"):
        self.check(ident)
        self.store.execute("UPDATE tasks SET stage=?,status=?,updated=? WHERE id=?", (stage, status, time.time(), ident))

    async def step(self, ident, name, args, call, resource="cpu"):
        row = self.check(ident)
        self.progress(ident, "等待资源："+name, "queued")
        limit = 1 if resource in {"gpu", "video"} else self.config.get("tasks", "parallel")
        remaining = max(.01, row["deadline"]-time.time()) if row["deadline"] else self.config.get("tasks", "timeout")
        waiting = self.diagnostics.start("resource", "等待资源："+name, {"resource": resource}) if self.diagnostics else None
        try:
            async with asyncio.timeout(remaining), self.resources.acquire("user:"+row["bot"]+":"+row["owner"], bool(row["autonomous"]), self.config.get("tasks", "per_user")), self.resources.acquire(resource, bool(row["autonomous"]), limit):
                if self.diagnostics:
                    self.diagnostics.end(waiting)
                self.check(ident)
                self.progress(ident, name)
                step_id = uid("s_")
                self.store.insert("task_steps", {"id": step_id, "task_id": ident, "name": name, "input": json.dumps(args, ensure_ascii=False), "status": "running", "started": time.time()})
                observed = self.diagnostics.start("tool", name, {"step_id": step_id, "resource": resource, "arguments": args}) if self.diagnostics else None
                try:
                    if resource == "gpu" and self.resource_hook:
                        await self.resource_hook(True)
                    result = await call()
                    self.check(ident)
                    self.store.execute("UPDATE task_steps SET status='success',result=?,finished=? WHERE id=?", (json.dumps(result, ensure_ascii=False, default=str), time.time(), step_id))
                    if self.diagnostics:
                        self.diagnostics.end(observed, detail={"completed": True})
                    return result
                except BaseException as exc:
                    self.store.execute("UPDATE task_steps SET status=?,result=?,finished=? WHERE id=?", ("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", json.dumps({"error": str(exc)[:1000]}), time.time(), step_id))
                    if self.diagnostics:
                        self.diagnostics.end(observed, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error=exc)
                    exc.sol_span_id = observed
                    raise
                finally:
                    if resource == "gpu" and self.resource_hook:
                        await self.resource_hook(False)
        except BaseException as exc:
            if self.diagnostics:
                self.diagnostics.end(waiting, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error=exc)
            raise

    async def interrupt_all(self, reason):
        self.accepting = False
        self.epoch += 1
        count = self.interrupt_records(reason)
        for task in list(self.handles.values())+list(self.reminder_handles.values()):
            task.cancel()
        await asyncio.gather(*list(self.handles.values()), *list(self.reminder_handles.values()), return_exceptions=True)
        self.handles.clear()
        self.reminder_handles.clear()
        return count

    def finish(self, ident, result=None, error=None):
        row = self.get(ident)
        if row["status"] in {"interrupted", "cancelled"}:
            return
        preserved = self.store.one("SELECT COUNT(*) AS n FROM files WHERE task_id=? AND kind='output' AND status='ready'", (ident,))["n"]
        completed = self.store.one("SELECT COUNT(*) AS n FROM tool_receipts WHERE task_id=?", (ident,))["n"]
        state = "partial" if error and (preserved or completed) else "failed" if error else "completed"
        self.store.execute("UPDATE tasks SET status=?,stage=?,result=?,error=?,updated=? WHERE id=?", (state, "部分步骤完成，等待补救" if state == "partial" else "处理失败" if error else "已完成", json.dumps(result or {}, ensure_ascii=False), error, time.time(), ident))
        if not error and self.diagnostics and json.loads(row["payload"]).get("recovering"):
            self.diagnostics.recover_faults(ident)
        self.store.execute("DELETE FROM collaborators WHERE task_id=?", (ident,))
        self.store.execute("UPDATE approvals SET status='expired' WHERE task_id=? AND status='pending'", (ident,))
        self.store.event("task", row["title"]+("失败" if error else "已完成"), {"id": ident, "error": error}, "error" if error else "info")
