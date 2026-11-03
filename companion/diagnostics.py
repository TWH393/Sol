"""Passive request evidence and grouped faults. No probes, notifications or model calls."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import json
import re
import time
import traceback
from contextlib import contextmanager

from .storage import uid

REQUEST = contextvars.ContextVar("sol_request", default=None)
TASK = contextvars.ContextVar("sol_task", default=None)


def scrub(value, key=""):
    if re.search(r"(?i)^(?:api_?key|access_?token|refresh_?token|authorization|cookie|password|secret|key|token|reasoning_content)$", key):
        return "[已隐藏]"
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?i)(?:bearer\s+)[\w.\-]+|\bsk-[\w-]+|\beyJ[\w-]+\.[\w-]+\.[\w-]+", "[已隐藏]", value)
    value = re.sub(r"data:[^;\s]+;base64,[A-Za-z0-9+/=]+", "[媒体内容已省略]", value)
    value = re.sub(r"(?i)([?&](?:key|token|signature|sig|access_token|api_key)=)[^&\s]+", r"\1[已隐藏]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|authorization|cookie)\s*[:=]\s*)[^\s,;]+", r"\1[已隐藏]", value)
    return value


def request_scope(function):
    @functools.wraps(function)
    async def wrapped(self, message, *args, **kwargs):
        source = message[0] if isinstance(message, list) else message
        token = REQUEST.set(source.request_id or None)
        task_token = TASK.set(kwargs.get("task_id"))
        try:
            return await function(self, message, *args, **kwargs)
        finally:
            TASK.reset(task_token)
            REQUEST.reset(token)
    return wrapped


class Diagnostics:
    def __init__(self, store):
        self.store = store

    def begin(self, message, parent=None):
        ident = uid("req_")
        self.store.insert("requests", {"id": ident, "bot": message.bot, "scope": message.scope,
            "owner": message.sender, "message_id": message.id, "parent_id": parent,
            "status": "running", "stage": "已接收", "created": time.time()})
        self.store.execute("INSERT OR IGNORE INTO request_messages VALUES(?,?)", (ident, message.id))
        message.request_id = ident
        self.instant("receive", "收到消息", {"message_id": message.id}, ident)
        return ident

    def merge(self, message, batch):
        primary = batch[0].request_id
        message.request_id = primary
        for item in batch:
            self.store.execute("INSERT OR IGNORE INTO request_messages VALUES(?,?)", (primary, item.id))
            if item.request_id and item.request_id != primary:
                self.store.execute("UPDATE requests SET parent_id=?,status='merged',stage='已合并',finished=? WHERE id=?", (primary, time.time(), item.request_id))

    def attach_task(self, task_id):
        if REQUEST.get():
            request_id = REQUEST.get()
            existing = self.store.one("SELECT task_id FROM requests WHERE id=?", (request_id,))
            if existing and existing["task_id"] and existing["task_id"] != task_id:
                # A reminder/child job must not steal its parent request's trace.
                return
            TASK.set(task_id)
            self.store.execute("UPDATE requests SET task_id=? WHERE id=? OR parent_id=? AND status='merged'", (task_id, request_id, request_id))
            self.store.execute("UPDATE request_spans SET task_id=? WHERE task_id IS NULL AND request_id IN (SELECT id FROM requests WHERE task_id=?)", (task_id, task_id))
            self.store.execute("UPDATE fault_occurrences SET task_id=? WHERE task_id IS NULL AND request_id IN (SELECT id FROM requests WHERE task_id=?)", (task_id, task_id))
            self.store.execute("UPDATE deliveries SET task_id=? WHERE task_id IS NULL AND request_id IN (SELECT id FROM requests WHERE task_id=?)", (task_id, task_id))
            for span in self.store.rows("SELECT detail FROM request_spans WHERE request_id=? AND stage IN ('model','model_queue')", (request_id,)):
                usage_id = json.loads(span["detail"]).get("usage_id")
                if usage_id:
                    self.store.execute("UPDATE usage SET task_id=? WHERE id=? AND task_id IS NULL", (task_id, usage_id))
        else:
            TASK.set(task_id)

    def start(self, stage, name, detail=None, request_id=None):
        request_id = request_id or REQUEST.get()
        if not request_id:
            return None
        ident = uid("span_")
        self.store.insert("request_spans", {"id": ident, "request_id": request_id, "task_id": TASK.get(),
            "stage": stage, "name": name, "status": "running", "started": time.time(),
            "detail": json.dumps(scrub(detail or {}), ensure_ascii=False, default=str)})
        self.store.execute("UPDATE requests SET stage=? WHERE id=? AND status='running'", (name, request_id))
        return ident

    def end(self, ident, status="success", detail=None, error=None):
        if not ident:
            return None
        row = self.store.one("SELECT * FROM request_spans WHERE id=?", (ident,))
        if not row or row["finished"]:
            return None
        fault = None
        reason = scrub(str(error)) if error else ""
        if error and not isinstance(error, asyncio.CancelledError):
            fault = self.fault(row["stage"], row["name"], error, row["request_id"], row["task_id"] or TASK.get(), ident)
        merged = {**json.loads(row["detail"]), **scrub(detail or {})}
        self.store.execute("UPDATE request_spans SET status=?,finished=?,detail=?,error=?,fault_id=? WHERE id=?", (status, time.time(), json.dumps(merged, ensure_ascii=False, default=str), reason[:2000], fault, ident))
        return fault

    def instant(self, stage, name, detail=None, request_id=None, status="success"):
        ident = self.start(stage, name, detail, request_id)
        self.end(ident, status)

    @contextmanager
    def span(self, stage, name, detail=None, request_id=None):
        ident = self.start(stage, name, detail, request_id)
        try:
            yield ident
        except BaseException as exc:
            self.end(ident, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", error=exc)
            raise
        else:
            self.end(ident)

    def finish(self, request_id, status="completed", reason=""):
        if request_id:
            self.store.execute("UPDATE request_spans SET status=?,finished=?,error=CASE WHEN error='' THEN ? ELSE error END WHERE request_id=? AND status='running'", ("cancelled" if status in {"failed", "partial", "cancelled", "interrupted"} else "skipped", time.time(), scrub(reason)[:1500], request_id))
            self.store.execute("UPDATE requests SET status=?,finished=?,reason=?,stage=? WHERE id=? AND status!='merged'", (status, time.time(), scrub(reason)[:1500], {"completed": "处理完成", "partial": "部分完成", "failed": "处理失败", "skipped": "未参与回复", "cancelled": "已中断", "waiting_approval": "等待确认"}.get(status, status), request_id))

    def fault(self, stage, title, error, request_id=None, task_id=None, span_id=None, message_id=None):
        request_id = request_id or REQUEST.get()
        task_id = task_id or TASK.get()
        reason = scrub(str(error))[:2000]
        # Deduplicate the same failure reported by a low-level span and its caller.
        if request_id and stage in {"execution", "prepare"}:
            reasons, cause = [reason], getattr(error, "__cause__", None)
            for _ in range(5):
                if cause is None:
                    break
                reasons.append(scrub(str(cause))[:2000])
                cause = getattr(cause, "__cause__", None)
            marks = ",".join("?" for _ in reasons)
            recent = self.store.one("SELECT o.fault_id FROM fault_occurrences o JOIN faults f ON f.id=o.fault_id WHERE o.request_id=? AND f.reason IN ("+marks+") AND o.created>? ORDER BY o.created DESC LIMIT 1", (request_id, *reasons, time.time()-2))
            if recent:
                return recent["fault_id"]
        canonical = re.sub(r"(?:req_|t_|f_|span_|u_)[a-f0-9]{16}|\b\d{8,}\b|0x[0-9a-fA-F]+", "<id>", reason)
        fingerprint = hashlib.sha256((stage+":"+title+":"+type(error).__name__+":"+canonical).encode()).hexdigest()
        suggestion = "查看关联步骤和已保留成果；只补救失败部分"
        if re.search(r"登录|401|403|授权|密钥|配置", reason):
            suggestion = "先处理对应账号或配置，再继续；不要反复重试"
        elif stage == "delivery":
            suggestion = "文件已生成时仅重发；送达未确认时先核对QQ，避免重复发送"
        now = time.time()
        with self.store.lock, self.store.db:
            row = self.store.one("SELECT id FROM faults WHERE fingerprint=?", (fingerprint,))
            ident = row["id"] if row else uid("fault_")
            self.store.db.execute("INSERT INTO faults(id,fingerprint,title,stage,reason,status,count,first_seen,last_seen,suggestion) VALUES(?,?,?,?,?,'open',1,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET status='open',count=count+1,last_seen=excluded.last_seen,recovered=NULL,reason=excluded.reason", (ident, fingerprint, title, stage, reason, now, now, suggestion))
            trace = "".join(traceback.format_exception(type(error), error, error.__traceback__)) if isinstance(error, BaseException) else str(error)
            self.store.db.execute("INSERT INTO fault_occurrences(id,fault_id,request_id,task_id,span_id,message_id,status,detail,created) VALUES(?,?,?,?,?,?,'open',?,?)", (uid("fo_"), ident, request_id, task_id, span_id, message_id, json.dumps({"technical": scrub(trace)[-16000:]}, ensure_ascii=False), now))
        return ident

    def recover_faults(self, task_id, name=None):
        clauses, args = ["o.task_id=?", "o.status='open'"], [task_id]
        if name:
            clauses.append("s.name=?")
            args.append(name)
        for row in self.store.rows("SELECT o.id,o.fault_id FROM fault_occurrences o LEFT JOIN request_spans s ON s.id=o.span_id WHERE "+" AND ".join(clauses), args):
            self.store.execute("UPDATE fault_occurrences SET status='recovered',recovered=? WHERE id=?", (time.time(), row["id"]))
            if not self.store.one("SELECT id FROM fault_occurrences WHERE fault_id=? AND status='open'", (row["fault_id"],)):
                self.store.execute("UPDATE faults SET status='recovered',recovered=? WHERE id=?", (time.time(), row["fault_id"]))

    def recover_span(self, span_id):
        if not span_id:
            return
        for row in self.store.rows("SELECT id,fault_id FROM fault_occurrences WHERE span_id=? AND status='open'", (span_id,)):
            self.store.execute("UPDATE fault_occurrences SET status='recovered',recovered=? WHERE id=?", (time.time(), row["id"]))
            if not self.store.one("SELECT id FROM fault_occurrences WHERE fault_id=? AND status='open'", (row["fault_id"],)):
                self.store.execute("UPDATE faults SET status='recovered',recovered=? WHERE id=?", (time.time(), row["fault_id"]))

    def recover_delivery(self, file_id, bot, scope):
        if not file_id:
            return
        rows = self.store.rows("""SELECT DISTINCT s.id FROM request_spans s
            JOIN requests r ON r.id=s.request_id JOIN fault_occurrences o ON o.span_id=s.id
            WHERE r.bot=? AND r.scope=? AND s.stage IN ('delivery','send_queue')
            AND json_extract(s.detail,'$.file_id')=? AND o.status='open'""", (bot, scope, file_id))
        for row in rows:
            self.recover_span(row["id"])

    def recover_stage(self, stage):
        now = time.time()
        self.store.execute("UPDATE fault_occurrences SET status='recovered',recovered=? WHERE status='open' AND fault_id IN (SELECT id FROM faults WHERE stage=? AND status='open')", (now, stage))
        self.store.execute("UPDATE faults SET status='recovered',recovered=? WHERE stage=? AND status='open'", (now, stage))

    def recover_request_stage(self, request_id, stage):
        if not request_id:
            return
        for row in self.store.rows("SELECT o.id,o.fault_id FROM fault_occurrences o JOIN faults f ON f.id=o.fault_id WHERE o.request_id=? AND f.stage=? AND o.status='open'", (request_id, stage)):
            self.store.execute("UPDATE fault_occurrences SET status='recovered',recovered=? WHERE id=?", (time.time(), row["id"]))
            if not self.store.one("SELECT id FROM fault_occurrences WHERE fault_id=? AND status='open'", (row["fault_id"],)):
                self.store.execute("UPDATE faults SET status='recovered',recovered=? WHERE id=?", (time.time(), row["fault_id"]))

    def observe_event(self, kind, title, detail, level):
        if kind == "connection" and level == "info" and "已连接" in title:
            self.recover_stage("connection")
            return
        if level not in {"error", "warning"} or REQUEST.get():
            return
        if not re.search(r"失败|异常|未完成|未启动|断联|断开|错误|超时", title):
            return
        if not isinstance(detail, dict):
            detail = {"error": str(detail)}
        self.fault(kind, title, detail.get("error") or detail.get("reason") or title, task_id=detail.get("task_id"), message_id=detail.get("message"))

    def memories(self, rows, selected=None):
        if not REQUEST.get():
            return
        for row in rows:
            snapshot = {k: row.get(k) for k in ("id", "bot", "owner", "subject", "scope", "kind", "visibility", "text", "evidence", "valid_until", "supersedes", "updated")}
            self.store.execute("INSERT INTO request_memories(request_id,memory_id,snapshot,selected,state) VALUES(?,?,?,?,?) ON CONFLICT(request_id,memory_id) DO UPDATE SET selected=MAX(selected,excluded.selected),state=CASE WHEN request_memories.state='candidate' THEN excluded.state ELSE request_memories.state END", (REQUEST.get(), row["id"], json.dumps(scrub(snapshot), ensure_ascii=False), int(selected is not None and row["id"] in selected), "selected" if selected is not None and row["id"] in selected else "candidate"))

    def memory_submission(self, messages, call_id, state):
        request_id = REQUEST.get()
        if not request_id:
            return
        # Explicit memory IDs in messages are evidence; no claim of model adoption.
        texts = []
        for message in messages:
            value = message.get("content")
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, list):
                texts.extend(part["text"] for part in value if isinstance(part, dict) and isinstance(part.get("text"), str))
        content = "\n".join(texts)
        for row in self.store.rows("SELECT memory_id,snapshot,calls FROM request_memories WHERE request_id=?", (request_id,)):
            text = json.loads(row["snapshot"]).get("text") or ""
            encoded = json.dumps(text, ensure_ascii=False)[1:-1]
            if row["memory_id"] not in content or not text or text not in content and encoded not in content:
                continue
            calls = json.loads(row["calls"])
            calls = [c for c in calls if c["id"] != call_id]+[{"id": call_id, "state": state}]
            overall = "supplied" if any(c["state"] == "supplied" for c in calls) else state
            self.store.execute("UPDATE request_memories SET selected=1,state=?,calls=? WHERE request_id=? AND memory_id=?", (overall, json.dumps(calls), request_id, row["memory_id"]))

    def recover(self):
        now = time.time()
        self.store.execute("UPDATE requests SET status='cancelled',stage='上次运行中断',finished=? WHERE status='running'", (now,))
        self.store.execute("UPDATE request_spans SET status='cancelled',finished=?,error='上次运行中断' WHERE status='running'", (now,))
        self.store.execute("UPDATE deliveries SET status='cancelled',error='上次排队中断，尚未提交',finished=? WHERE status='queued'", (now,))
        self.store.execute("UPDATE deliveries SET status='unknown',error='上次发送中断，送达未确认',finished=? WHERE status='sending'", (now,))

    def details(self, request_id):
        row = self.store.one("SELECT * FROM requests WHERE id=?", (request_id,))
        if not row:
            raise ValueError("没有这轮请求的处理记录")
        if row["status"] == "merged" and row.get("parent_id"):
            return self.details(row["parent_id"])
        if row["status"] == "running":
            active = self.store.one("SELECT name FROM request_spans WHERE request_id=? AND status='running' ORDER BY CASE stage WHEN 'tool' THEN 0 WHEN 'model' THEN 1 WHEN 'model_queue' THEN 2 WHEN 'model_prepare' THEN 3 WHEN 'memory' THEN 4 WHEN 'prepare' THEN 5 WHEN 'local' THEN 6 ELSE 7 END,started DESC LIMIT 1", (request_id,))
            if active:
                row["stage"] = active["name"]
        ids = [request_id]+[r["id"] for r in self.store.rows("SELECT id FROM requests WHERE parent_id=? AND status='merged'", (request_id,))]
        marks = ",".join("?" for _ in ids)
        return {"request": row,
            "spans": self.store.rows(f"SELECT * FROM request_spans WHERE request_id IN ({marks}) ORDER BY started", ids),
            "memories": self.store.rows("SELECT * FROM request_memories WHERE request_id=?", (request_id,)),
            "deliveries": self.store.rows("SELECT * FROM deliveries WHERE request_id=? ORDER BY created", (request_id,)),
            "attempts": self.store.rows("SELECT * FROM requests WHERE task_id=? ORDER BY created", (row["task_id"],)) if row["task_id"] else []}
