"""Explicit, same-task repair with persisted successful tool receipts."""
from __future__ import annotations

import asyncio
import ast
from dataclasses import asdict
import hashlib
import json
import time

from .diagnostics import REQUEST, TASK, scrub
from .domain import Message, ToolResult


class Recovery:
    def __init__(self, app):
        self.app, self.store = app, app.store

    @staticmethod
    def signature(args):
        return hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def save(self, task_id, name, args, result):
        self.store.execute("INSERT INTO tool_receipts VALUES(?,?,?,?,?,?) ON CONFLICT(task_id,name,signature) DO UPDATE SET result=excluded.result,created=excluded.created", (task_id, name, self.signature(args), json.dumps(scrub(args), ensure_ascii=False), json.dumps(scrub(asdict(result)), ensure_ascii=False, default=str), time.time()))

    def reuse(self, task_id, name, args, message):
        row = self.store.one("SELECT result FROM tool_receipts WHERE task_id=? AND name=? AND signature=?", (task_id, name, self.signature(args)))
        if not row:
            return None
        result = ToolResult(**json.loads(row["result"]))
        for ident in result.files:
            self.app.files.resolve(ident, message)
        self.app.diagnostics.instant("reuse", "复用已完成步骤", {"tool": name})
        return result

    def context(self, task_id):
        # Pre-upgrade steps stored dataclass reprs inside JSON. Read those as
        # literals only; never eval a tool result or execute old code to recover it.
        for step in self.store.rows("SELECT name,input,result FROM task_steps WHERE task_id=? AND status='success' ORDER BY started", (task_id,)):
            args = json.loads(step["input"])
            if self.store.one("SELECT name FROM tool_receipts WHERE task_id=? AND name=? AND signature=?", (task_id, step["name"], self.signature(args))):
                continue
            raw = step["result"] or ""
            if len(raw) > 2_000_000:
                continue
            try:
                value = json.loads(raw)
                node = ast.parse(value, mode="eval").body if isinstance(value, str) else None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ToolResult" and not node.args:
                    values = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg in ToolResult.__dataclass_fields__}
                    result = ToolResult(**values)
                    if isinstance(result.files, list) and isinstance(result.data, dict):
                        self.save(task_id, step["name"], args, result)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                continue
        rows = self.store.rows("SELECT name,args,result,created FROM tool_receipts WHERE task_id=? ORDER BY created", (task_id,))
        completed = [{"tool": r["name"], "files": json.loads(r["result"]).get("files", []),
                 "text": json.loads(r["result"]).get("text", ""), "completed": r["created"]} for r in rows]
        failed = self.store.rows("SELECT name,input,result,started FROM task_steps WHERE task_id=? AND status IN ('failed','cancelled','interrupted') ORDER BY started DESC LIMIT 3", (task_id,))
        return scrub({"completed_steps": completed, "failed_steps": failed,
            "saved_files": self.store.rows("SELECT id,name,kind,status FROM files WHERE task_id=?", (task_id,)),
            "execution_state": "执行环境可能已释放；从保留文件、制作代码与数据恢复必要输入，再处理失败部分，不能依赖旧Python变量。不要重跑已完成的生成、提醒或发送操作。"})

    def options(self, task_id):
        task = self.app.tasks.get(task_id)
        pending = self.pending_deliveries(task_id)
        active = bool(task_id in self.app.tasks.handles and not self.app.tasks.handles[task_id].done())
        resumable = task["status"] in {"failed", "partial", "interrupted"} and not active and task["kind"] != "reminder"
        payload = json.loads(task["payload"])
        return {"can_continue": resumable and bool(payload.get("message")) and not pending,
                "can_resend": resumable and bool(pending), "deliveries": pending,
                "completed_steps": self.store.one("SELECT COUNT(*) AS n FROM tool_receipts WHERE task_id=?", (task_id,))["n"],
                "note": "在原任务中补救，记录保留；继续处理可能调用模型并产生用量。发送未确认需先核对QQ。"}

    def pending_deliveries(self, task_id):
        failed = self.store.rows("""SELECT d.* FROM deliveries d WHERE task_id=? AND file_id IS NOT NULL
            AND status IN ('failed','unknown','cancelled')
            AND NOT EXISTS(SELECT 1 FROM deliveries n WHERE n.task_id=d.task_id AND n.file_id=d.file_id AND (n.created>d.created OR n.created=d.created AND n.id>d.id))
            ORDER BY created""", (task_id,))
        task = self.app.tasks.get(task_id)
        intended = json.loads(task["payload"]).get("delivery_files", [])
        known = {d["file_id"] for d in failed}
        for file_id in intended:
            if file_id in known:
                continue
            latest = self.store.one("SELECT status FROM deliveries WHERE task_id=? AND file_id=? ORDER BY created DESC,id DESC LIMIT 1", (task_id, file_id))
            if latest is None:
                failed.append({"file_id": file_id, "status": "not_submitted", "error": "前一文件发送失败，本文件尚未提交"})
        return failed

    async def continue_task(self, task_id, mode="continue", confirm_unknown=False):
        task = self.app.tasks.get(task_id)
        options = self.options(task_id)
        if not self.app.online or self.app.stopping or not self.app.tasks.accepting:
            raise ValueError("QQ尚未连接，请先恢复连接")
        if mode not in {"continue", "resend"} or not options["can_continue" if mode == "continue" else "can_resend"]:
            raise ValueError("当前任务不能执行此补救；已有文件发送失败时请使用重发")
        payload = json.loads(task["payload"])
        source = payload.get("message")
        if not source:
            raise ValueError("此旧任务没有可恢复的原始请求，请在原会话重新说明")
        message = Message(**{k: v for k, v in source.items() if k in Message.__dataclass_fields__})
        if (message.bot, message.scope, message.sender) != (task["bot"], task["scope"], task["owner"]):
            raise PermissionError("任务来源与创建者不匹配，不能继续")
        if not self.app.chat.allowed(message):
            raise PermissionError("当前使用范围不允许此会话")
        if self.app.config.get("account", "driver") == "native":
            # Preserve the existing fresh-target rule; no old peer is resurrected.
            await self.app.services.native_qq.rpc("peer", {"scope": message.scope})
        if mode == "resend" and any(d["status"] == "unknown" for d in options["deliveries"]) and not confirm_unknown:
            raise ValueError("存在送达未确认的文件，请先核对QQ后明确确认重发")
        limit = self.app.config.get("tasks")
        seconds = limit["type_limits"].get(task["kind"], {}).get("timeout", limit["timeout"])
        payload["recovering"] = True
        changed = self.store.execute("UPDATE tasks SET status='queued',stage='等待原任务补救',error=NULL,deadline=?,payload=?,attempts=attempts+1,updated=? WHERE id=? AND status IN ('failed','partial','interrupted')", (time.time()+seconds, json.dumps(payload, ensure_ascii=False), time.time(), task_id))
        if not changed:
            raise ValueError("任务已在处理或状态已变化，请刷新后查看")
        self.app.tasks.cancelled.discard(task_id)
        original = message.request_id
        self.app.diagnostics.begin(message, parent=original or None)
        token = REQUEST.set(message.request_id)
        task_token = TASK.set(task_id)
        try:
            self.app.diagnostics.attach_task(task_id)
            self.app.diagnostics.instant("recovery", "管理员继续原任务", {"mode": mode, "completed_steps": options["completed_steps"]})
            if mode == "continue":
                handle = self.app.background_task(self.app.chat.run(message, task["kind"], task_id), "recover:"+task_id)
            else:
                handle = self.app.background_task(self.resend(message, task_id, options["deliveries"]), "resend:"+task_id)
            self.app.tasks.handles[task_id] = handle
        finally:
            TASK.reset(task_token)
            REQUEST.reset(token)
        return {"task_id": task_id, "request_id": message.request_id, "status": "queued"}

    async def resend(self, message, task_id, deliveries):
        try:
            self.app.tasks.progress(task_id, "仅重发已有文件")
            for item in deliveries:
                self.app.tasks.check(task_id)
                self.app.files.resolve(item["file_id"], message)
                await self.app.delivery.enqueue(message, file=item["file_id"])
                self.store.execute("DELETE FROM file_refs WHERE file_id=? AND target_kind='delivery_failed'", (item["file_id"],))
            payload = json.loads(self.app.tasks.get(task_id)["payload"])
            payload["delivery_pending"] = False
            self.store.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), task_id))
            self.app.tasks.finish(task_id, {"resent": [d["file_id"] for d in deliveries], "regenerated": False})
            self.app.diagnostics.recover_faults(task_id)
            self.app.diagnostics.finish(message.request_id)
        except asyncio.CancelledError:
            self.app.diagnostics.finish(message.request_id, "cancelled", "重发已中断")
            raise
        except Exception as exc:
            self.app.tasks.finish(task_id, error=str(exc))
            self.app.diagnostics.fault("delivery", "已有文件重发未完成", exc)
            self.app.diagnostics.finish(message.request_id, "failed", str(exc))
        finally:
            self.app.tasks.handles.pop(task_id, None)
