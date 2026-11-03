"""Immutable artifact versions, quoted-version branches and editable provenance."""
from __future__ import annotations

import json
import re
import time

from .diagnostics import scrub
from .storage import uid


class Artifacts:
    def __init__(self, app):
        self.app, self.store = app, app.store

    def version(self, file_id):
        seen = set()
        while file_id and file_id not in seen:
            seen.add(file_id)
            row = self.store.one("SELECT v.*,a.owner,a.bot,a.scope,a.title,a.latest_file FROM artifact_versions v JOIN artifacts a ON a.id=v.artifact_id WHERE v.file_id=?", (file_id,))
            if row:
                return row
            record = self.store.one("SELECT metadata FROM files WHERE id=?", (file_id,))
            file_id = json.loads(record["metadata"]).get("reused_from") if record else None
        return None

    def permitted(self, row, message):
        if row["bot"] != message.bot or row["scope"] != message.scope:
            return False
        return row["owner"] == message.sender or self.app.tasks.allowed(row["task_id"], message.sender, "edit")

    def adopt(self, file_id, message):
        """Lazily import pre-upgrade outputs, preserving their real source files."""
        if not file_id:
            return None
        file, _ = self.app.files.resolve(file_id, message)
        if file["kind"] != "output" or not file["task_id"]:
            return None
        task = self.app.tasks.get(file["task_id"])
        if file["owner"] != message.sender and not self.app.tasks.allowed(task["id"], message.sender, "edit"):
            return None
        from .domain import ToolResult
        metadata = json.loads(file["metadata"])
        recipe = self.store.one("SELECT name,input FROM task_steps WHERE task_id=? AND status='success' ORDER BY started DESC LIMIT 1", (task["id"],))
        args = json.loads(recipe["input"]) if recipe else {"file_ids": metadata.get("input_files", [])}
        self.capture(message, task["id"], recipe["name"] if recipe else "existing_output", args,
                     ToolResult(files=[file_id], data={"change_summary": "已有成果纳入版本；原始制作信息与可用源文件保留"}))
        return self.version(file_id)

    def select(self, message):
        if not re.search(r"改|修改|调整|换|优化|美化|补充|删掉|删除|加上|再做|继续做|重新排版|恢复.*版", message.text):
            return None
        if re.search(r"先不(?:改|做)|先别|只(?:是)?讨论|不修改|如果|怎么(?:修改|改)|能不能|可不可以", message.text) and not re.search(r"帮我|请(?:把|改|修改)|直接(?:改|做)", message.text):
            return None
        quoted = []
        quote_ids = list(dict.fromkeys([message.quote_id, *[p.get("quote_id") for p in message.parts]]))
        for quote_id in filter(None, quote_ids):
            row = self.store.one("SELECT payload FROM messages WHERE bot=? AND scope=? AND id=?", (message.bot, message.scope, quote_id))
            if row:
                quoted += [a.get("file_id") for a in json.loads(row["payload"]).get("attachments", [])]
        explicit = [a.get("file_id") for a in message.attachments]
        candidates = []
        for ident in quoted+explicit:
            if not ident:
                continue
            try:
                row = self.version(ident) or self.adopt(ident, message)
            except (PermissionError, FileNotFoundError):
                continue
            if row and row["file_id"] not in {r["file_id"] for r in candidates} and self.permitted(row, message):
                candidates.append(row)
        if message.quote_id or quoted:
            # A quoted non-artifact never silently selects an unrelated latest file.
            return self._choose(candidates, message.text)
        if explicit and not candidates:
            return None
        if not candidates:
            recent = self.store.rows("SELECT payload FROM messages WHERE bot=? AND scope=? AND role='assistant' ORDER BY created DESC LIMIT 6", (message.bot, message.scope))
            recent_files = {a.get("file_id") for r in recent for a in json.loads(r["payload"]).get("attachments", [])}
            if not recent_files and not re.search(r"PPT|PDF|Word|Excel|文档|文件|表格|图片|封面|排版|第.+页|第.+版", message.text, re.I):
                return None
            if re.search(r"回答|回复|解释|说法", message.text) and not re.search(r"PPT|PDF|文档|文件|图片|表格", message.text, re.I):
                return None
            rows = self.store.rows("SELECT v.*,a.owner,a.bot,a.scope,a.title,a.latest_file FROM artifacts a JOIN artifact_versions v ON v.file_id=a.latest_file WHERE a.bot=? AND a.scope=? AND a.owner=? ORDER BY a.updated DESC LIMIT 12", (message.bot, message.scope, message.sender))
            candidates = [r for r in rows if self.permitted(r, message)]
            if not candidates:
                legacy = self.store.rows("SELECT id FROM files WHERE bot=? AND scope=? AND owner=? AND kind='output' AND status='ready' ORDER BY created DESC LIMIT 12", (message.bot, message.scope, message.sender))
                for item in legacy:
                    candidate = self.adopt(item["id"], message)
                    if candidate:
                        candidates.append(candidate)
            named = self._choose(candidates, message.text, named_only=True)
            if named:
                return named
            # The latest delivered artifact is the default; equally recent multi-
            # output bundles require the model/user to select, not an arbitrary file.
            if candidates:
                latest_task = candidates[0]["task_id"]
                return self._choose([r for r in candidates if r["task_id"] == latest_task], message.text)
        return self._choose(candidates, message.text)

    def _choose(self, rows, text, named_only=False):
        named = []
        for row in rows:
            file = self.store.one("SELECT name FROM files WHERE id=?", (row["file_id"],))
            name = file["name"] if file else row["title"]
            stem = name.rsplit(".", 1)[0]
            if name and (name in text or len(stem) >= 2 and stem in text):
                named.append(row)
        if len(named) == 1:
            return named[0]
        requested = re.search(r"\b(PDF|PPTX?|DOCX?|Word|XLSX?|Excel)\b", text, re.I)
        if requested:
            kind = requested[1].lower()
            kind = {"ppt": "pptx", "word": "docx", "doc": "docx", "excel": "xlsx", "xls": "xlsx"}.get(kind, kind)
            matching = [r for r in rows if self.store.one("SELECT name FROM files WHERE id=?", (r["file_id"],))["name"].lower().endswith("."+kind)]
            if len(matching) == 1:
                return matching[0]
        return rows[0] if len(rows) == 1 and not named_only else None

    def context(self, base, message):
        if not base:
            return None
        self.app.files.resolve(base["file_id"], message)
        sources = []
        for ident in json.loads(base["sources"]):
            try:
                row, _ = self.app.files.resolve(ident, message)
                sources.append({"file_id": ident, "name": row["name"], "kind": row["kind"]})
            except (FileNotFoundError, PermissionError):
                pass
        return {"base_file": base["file_id"], "artifact_id": base["artifact_id"], "version": base["number"],
            "branch_from_old": base["latest_file"] != base["file_id"], "title": base["title"], "sources": sources,
            "instruction": "基于此版本及本轮要求继续修改，可协调整份结构/排版/配图，但保留明确事实和数据。原文件与可编辑源/制作代码可按文件ID读取；需要完整制作配方可调用inspect_artifact。不要仅凭历史聊天重建。旧版分支不混入较新版本的修改。完成后直接交付新版本并附简短修改说明，不等待预览确认。"}

    async def inspect(self, message, task_id, file_id):
        from .domain import ToolResult
        row = self.version(file_id) or self.adopt(file_id, message)
        if not row or not self.permitted(row, message):
            raise PermissionError("成果不存在或缺少创建者对本次修改的授权")
        self.app.files.resolve(row["file_id"], message)
        return ToolResult(data={**self.context(row, message), "recipe": json.loads(row["recipe"]), "previous_changes": row["summary"]})

    async def base(self, message, task_id, file_id):
        from .domain import ToolResult
        row = self.version(file_id) or self.adopt(file_id, message)
        if not row or not self.permitted(row, message):
            raise PermissionError("只有创建者或已获本次授权的参与者可以修改此成果")
        context = self.context(row, message)
        task = self.app.tasks.get(task_id)
        payload = json.loads(task["payload"])
        payload["revision_base"] = row["file_id"]
        self.store.execute("UPDATE tasks SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), task_id))
        return ToolResult(data=context)

    def capture(self, message, task_id, name, args, result):
        if not result.files:
            return
        task = self.app.tasks.get(task_id)
        payload = json.loads(task["payload"])
        base = self.version(payload.get("revision_base"))
        if base and not self.permitted(base, message):
            raise PermissionError("成果修改授权已失效")
        for ident in result.files:
            file, _ = self.app.files.resolve(ident, message)
            existing = self.version(ident)
            if existing and existing["owner"] == file["owner"]:
                continue
            if file["kind"] != "output":
                continue
            def family(filename):
                ext = filename.rsplit(".", 1)[-1].lower()
                return "image" if ext in {"png", "jpg", "jpeg", "webp", "gif", "bmp"} else ext
            parent = base if base and family(file["name"]) == family(self.store.one("SELECT name FROM files WHERE id=?", (base["file_id"],))["name"]) else None
            if base and not parent:
                # A PPTX and its PDF export keep separate version numbers. When
                # branching an old bundle, each format follows its old sibling.
                siblings = self.store.rows("SELECT v.*,a.owner,a.bot,a.scope,a.title,a.latest_file,f.name FROM artifact_versions v JOIN artifacts a ON a.id=v.artifact_id JOIN files f ON f.id=v.file_id WHERE v.task_id=? AND a.bot=? AND a.scope=?", (base["task_id"], message.bot, message.scope))
                matching = [s for s in siblings if family(s["name"]) == family(file["name"]) and self.permitted(s, message)]
                named = [s for s in matching if s["name"] == file["name"]]
                parent = named[0] if len(named) == 1 else matching[0] if len(matching) == 1 else None
            metadata = json.loads(file["metadata"])
            source_ids = set(args.get("file_ids") or []) | set(metadata.get("input_files") or [])
            source_ids.update(i for i in (args.get("file_id"), args.get("mask_file"), metadata.get("code_file")) if i)
            if base:
                source_ids.update(json.loads(base["sources"]))
                source_ids.add(base["file_id"])
            sources = []
            for source_id in source_ids-{ident}:
                try:
                    self.app.files.resolve(source_id, message)
                    sources.append(source_id)
                except (FileNotFoundError, PermissionError):
                    continue
            now = time.time()
            with self.store.lock, self.store.db:
                artifact_id = parent["artifact_id"] if parent else uid("artifact_")
                if not parent:
                    self.store.db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)", (artifact_id, file["bot"], file["scope"], file["owner"], file["name"], ident, now, now))
                number = (self.store.one("SELECT MAX(number) AS n FROM artifact_versions WHERE artifact_id=?", (artifact_id,))["n"] or 0)+1
                summary = result.data.get("change_summary") or (("修改要求：" if parent else "首次制作要求：")+message.text)
                self.store.db.execute("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?,?)", (ident, artifact_id, number, parent["file_id"] if parent else None, task_id, summary, json.dumps(scrub({"tool": name, "arguments": args, "metadata": metadata}), ensure_ascii=False), json.dumps(sorted(sources)), now))
                self.store.db.execute("UPDATE artifacts SET latest_file=?,updated=? WHERE id=?", (ident, now, artifact_id))
            self.app.files.reference(ident, "artifact", artifact_id)
            for source_id in sources:
                self.app.files.reference(source_id, "artifact_version", ident)
            result.data.setdefault("artifact_versions", []).append({"file_id": ident, "artifact_id": artifact_id, "version": number, "parent_file": parent["file_id"] if parent else None})

    def details(self, file_id):
        row = self.version(file_id)
        if not row:
            file = self.store.one("SELECT id,name,kind,task_id,metadata FROM files WHERE id=?", (file_id,))
            if not file or file["kind"] != "output":
                raise ValueError("该文件还没有成果版本记录")
            meta = json.loads(file["metadata"])
            source_ids = set(meta.get("input_files") or [])
            if meta.get("code_file"):
                source_ids.add(meta["code_file"])
            sources = [source for ident in source_ids if (source := self.store.one("SELECT id,name,kind,status FROM files WHERE id=?", (ident,)))]
            return {"legacy": True, "file": {k: file[k] for k in ("id", "name", "task_id")}, "sources": sources, "recipe": meta, "versions": []}
        versions = self.store.rows("SELECT v.*,f.name,f.kind,f.status AS file_status,f.size FROM artifact_versions v JOIN files f ON f.id=v.file_id WHERE v.artifact_id=? ORDER BY v.number DESC", (row["artifact_id"],))
        return {"artifact": self.store.one("SELECT * FROM artifacts WHERE id=?", (row["artifact_id"],)), "selected_file": row["file_id"], "versions": versions}

    def register(self, registry):
        from .tools import STRING
        registry.add("inspect_artifact", "documents", "读取已有成果的版本、源文件与制作配方，便于继续修改。引用旧版应基于旧版分支，不混入新版本。", {"file_id": STRING}, ["file_id"], self.inspect)
        registry.add("select_artifact_base", "documents", "有多个成果时明确选择本轮修改的基础版本。只能修改本人或已获本次授权的成果；完成直接交付新版，旧版保留。", {"file_id": STRING}, ["file_id"], self.base)
