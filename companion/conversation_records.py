"""Read-only conversation projection. Does not change memory extraction or storage."""
from __future__ import annotations

import json


def conversations(store, bot):
    rows = store.rows("""WITH ranked AS (
        SELECT bot,scope,text,id,created,role,name,
          COUNT(*) OVER(PARTITION BY bot,scope) AS count,
          ROW_NUMBER() OVER(PARTITION BY bot,scope ORDER BY created DESC,id DESC) AS position
        FROM messages WHERE bot=?)
        SELECT bot,scope,count,created AS latest,id AS latest_id,text AS preview,role FROM ranked
        WHERE position=1 ORDER BY latest DESC,latest_id DESC""", (bot,))
    for row in rows:
        private = row["scope"].startswith("private:")
        contact = store.one("SELECT name FROM messages WHERE bot=? AND scope=? AND role='user' ORDER BY created DESC LIMIT 1", (bot, row["scope"])) if private else None
        row["name"] = contact["name"] if contact and contact["name"] else ("私聊" if private else "群聊")+" · "+row["scope"].split(":", 1)[-1]
        row["private"] = private
        row["preview"] = row["preview"][:180]
    by_scope = {r["scope"]: r for r in rows}
    for event in store.rows("SELECT id,created,detail FROM events WHERE kind='delivery' AND title='QQ消息发送失败' ORDER BY created DESC LIMIT 500"):
        detail = json.loads(event["detail"])
        row = by_scope.get(detail.get("scope"))
        if detail.get("bot") == bot and row and event["created"] > row["latest"]:
            row.update(latest=event["created"], latest_id="failed:"+event["id"], preview="发送失败："+detail.get("text", "[附件]")[:150])
    rows.sort(key=lambda r: (r["latest"], r["latest_id"]), reverse=True)
    return rows


def messages(store, bot, scope, before, before_id, query, anchor=""):
    conditions = ["bot=?", "scope=?"]
    args = [bot, scope]
    if anchor:
        target = store.one("SELECT id,created FROM messages WHERE bot=? AND scope=? AND id=?", (bot, scope, anchor))
        if not target:
            return []
        conditions.append("(created<? OR(created=? AND id<=?))")
        args += [target["created"], target["created"], target["id"]]
    if before_id:
        conditions.append("(created<? OR(created=? AND id<?))")
        args += [before, before, before_id]
    else:
        conditions.append("created<?")
        args.append(before)
    if query:
        needle = "%"+query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")+"%"
        conditions.append("(text LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')")
        args += [needle, needle]
    rows = store.rows("SELECT * FROM messages WHERE "+" AND ".join(conditions)+" ORDER BY created DESC,id DESC LIMIT 100", tuple(args))
    failed = store.rows("""SELECT id,created,detail FROM events WHERE kind='delivery' AND title='QQ消息发送失败'
        AND json_extract(detail,'$.bot')=? AND json_extract(detail,'$.scope')=?
        AND (created<? OR(created=? AND 'failed:'||id<?)) ORDER BY created DESC,id DESC LIMIT 100""",
        (bot, scope, before, before, before_id or "\uffff"))
    for event in failed:
        detail = json.loads(event["detail"])
        if query and query.lower() not in (detail.get("text", "")+detail.get("name", "")).lower():
            continue
        rows.append({"id": "failed:"+event["id"], "bot": bot, "scope": scope, "name": detail.get("name", "Sol"),
                     "sender": detail.get("sender", ""), "role": "assistant", "text": detail.get("text", ""),
                     "quote_id": detail.get("quote_id", ""), "created": event["created"],
                     "payload": json.dumps({"delivery_status": "failed", "error": detail.get("error", "发送未成功"), "attachments": []})})
    rows.sort(key=lambda r: (r["created"], r["id"]), reverse=True)
    rows = rows[:100]
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            payload = {}
        row["status"] = payload.get("delivery_status", "sent" if row["role"] == "assistant" else "received")
        row["error"] = payload.get("error", "")
        row["sources"] = payload.get("parts", [])
        if row.get("quote_id"):
            quote = store.one("SELECT name,text FROM messages WHERE bot=? AND scope=? AND id IN (?,?) LIMIT 1", (bot, scope, row["quote_id"], bot+":"+row["quote_id"]))
            if not quote:
                source = payload.get("quote_source") or next((part.get("quote") for part in payload.get("parts", []) if part.get("quote_id") == row["quote_id"] and part.get("quote")), {})
                if source and not source.get("missing") and str(source.get("id")) in {row["quote_id"], row["quote_id"].removeprefix(bot+":")}:
                    quote = {"name": source.get("name") or "未知", "text": source.get("text") or ""}
            row["quote"] = {"name": quote["name"], "text": quote["text"][:300]} if quote else None
        files = []
        for item in payload.get("attachments", []):
            record = store.one("SELECT id,name,kind,status,size,hash,metadata FROM files WHERE id=? AND bot=? AND scope=?", (item.get("file_id", ""), bot, scope))
            read = json.loads(record.pop("metadata")).get("last_model_input") if record else None
            image = item.get("kind") == "image" or bool(record and record["name"].lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
            files.append({**(record or {"name": item.get("name", "附件"), "status": "unavailable"}), "image": image, "error": item.get("error", ""), "input": read})
        row["attachments"] = files
        request = store.one("SELECT r.id,r.status,r.stage,r.created,r.finished FROM requests r JOIN request_messages m ON m.request_id=r.id WHERE m.message_id=? AND r.status!='merged' ORDER BY r.created DESC LIMIT 1", (row["id"],))
        if request and request["status"] == "running":
            active = store.one("SELECT name FROM request_spans WHERE request_id=? AND status='running' ORDER BY CASE stage WHEN 'tool' THEN 0 WHEN 'model' THEN 1 WHEN 'model_queue' THEN 2 WHEN 'memory' THEN 3 WHEN 'prepare' THEN 4 ELSE 5 END,started DESC LIMIT 1", (request["id"],))
            if active:
                request["stage"] = active["name"]
        row["processing"] = request
    return rows
