"""Local content index. Search always joins live file permissions, never caches access."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .memory import terms

TEXT = {".txt", ".md", ".csv", ".json", ".html", ".srt", ".vtt", ".py", ".yaml", ".yml"}
FORMATS = TEXT | {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm"}


def extract_chunks(path):
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        with path.open("rb") as stream:
            book = PdfReader(stream)
            if book.is_encrypted:
                raise ValueError("PDF已加密，未读取正文")
            for i, page in enumerate(book.pages):
                yield "第"+str(i+1)+"页", page.extract_text() or ""
    elif ext == ".docx":
        from docx import Document
        book = Document(path)
        for i, paragraph in enumerate(book.paragraphs):
            if paragraph.text.strip():
                yield "段落"+str(i+1), paragraph.text
        for i, table in enumerate(book.tables):
            for j, row in enumerate(table.rows):
                yield f"表格{i+1}·行{j+1}", " | ".join(c.text for c in row.cells)
    elif ext == ".pptx":
        from pptx import Presentation
        for i, slide in enumerate(Presentation(path).slides):
            yield "幻灯片"+str(i+1), "\n".join(s.text for s in slide.shapes if s.has_text_frame)
    elif ext in {".xlsx", ".xlsm"}:
        import openpyxl
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            for sheet in book.worksheets:
                batch = []
                start = 1
                for i, row in enumerate(sheet.values, 1):
                    batch.append(str(i)+": "+" | ".join(str(c) if c is not None else "" for c in row))
                    if len(batch) == 50:
                        yield f"工作表{sheet.title}·行{start}-{i}", "\n".join(batch)
                        batch, start = [], i+1
                if batch:
                    yield f"工作表{sheet.title}·行{start}-{i}", "\n".join(batch)
        finally:
            book.close()
    elif ext in TEXT:
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            block = []
            for i, line in enumerate(stream, 1):
                block.append(line.rstrip())
                if len(block) == 60:
                    yield f"行{i-59}-{i}", "\n".join(block)
                    block = []
            if block:
                yield f"行{i-len(block)+1}-{i}", "\n".join(block)


class DocumentIndex:
    def __init__(self, app):
        self.app = app
        self.pending = {}

    def index(self, row):
        path = Path(row["path"])
        if path.suffix.lower() not in FORMATS or not path.is_file():
            return
        digest = row["hash"]
        self.app.store.execute("DELETE FROM document_chunks WHERE hash=? AND location NOT LIKE '%·OCR'", (digest,))
        self.app.store.execute("DELETE FROM document_search WHERE hash=? AND location NOT LIKE '%·OCR'", (digest,))
        count, empty = 0, 0
        try:
            for location, text in extract_chunks(path):
                if self.app.stopping:
                    return
                if not text.strip():
                    empty += 1
                    continue
                # Chunking is a storage/search boundary, not an input-token budget.
                for offset in range(0, len(text), 10000):
                    label = location+(f"·片段{offset//10000+1}" if len(text) > 10000 else "")
                    self.add(digest, label, text[offset:offset+10000])
                    count += 1
            status, error = ("needs_ocr" if empty else "ready"), (f"{empty}处没有可提取文字，需要时读取原页或OCR" if empty else "")
        except Exception as exc:
            status, error = "failed", str(exc)[:500]
        self.app.store.execute("INSERT OR REPLACE INTO document_indexes VALUES(?,?,?,?)", (digest, status, error, time.time()))

    def add(self, digest, location, text):
        self.app.store.execute("INSERT OR REPLACE INTO document_chunks VALUES(?,?,?)", (digest, location, text))
        self.app.store.execute("DELETE FROM document_search WHERE hash=? AND location=?", (digest, location))
        self.app.store.execute("INSERT INTO document_search VALUES(?,?,?)", (digest, location, " ".join(sorted(terms(text)))))

    async def ensure(self, row):
        state = self.app.store.one("SELECT * FROM document_indexes WHERE hash=?", (row["hash"],))
        if state:
            return state
        handle = self.pending.get(row["hash"])
        if handle is None:
            if self.pending:
                return {"status": "pending", "error": "本地索引正在处理其他文档"}
            handle = asyncio.create_task(asyncio.to_thread(self.index, row))
            self.pending[row["hash"]] = handle
            def done(task):
                self.pending.pop(row["hash"], None)
                if not task.cancelled():
                    task.exception()
            handle.add_done_callback(done)
        try:
            await asyncio.wait_for(asyncio.shield(handle), 5)
        except TimeoutError:
            return {"status": "indexing", "error": "本地索引尚未完成，可以先按页读取原件"}
        return self.app.store.one("SELECT * FROM document_indexes WHERE hash=?", (row["hash"],)) or {"status": "pending"}

    async def maintain(self):
        while True:
            if not self.app.config.get("features", "documents"):
                await asyncio.sleep(2)
                continue
            if not self.pending:
                row = self.app.store.one("SELECT f.* FROM files f LEFT JOIN document_indexes i ON i.hash=f.hash WHERE f.status='ready' AND i.hash IS NULL AND f.kind IN ('original','material','note','output') AND lower(substr(f.name,instr(f.name,'.'))) != '' ORDER BY f.created DESC LIMIT 1")
                if row:
                    if Path(row["path"]).suffix.lower() in FORMATS:
                        await self.ensure(row)
                    else:
                        self.app.store.execute("INSERT OR IGNORE INTO document_indexes VALUES(?,?,?,?)", (row["hash"], "unsupported", "请使用对应媒体工具", time.time()))
            # Remove text when no live source remains. Favorites/notes follow Files retention.
            stale = self.app.store.rows("SELECT hash FROM document_indexes WHERE NOT EXISTS(SELECT 1 FROM files f WHERE f.hash=document_indexes.hash AND f.status='ready')")
            for row in stale:
                for table in ("document_search", "document_chunks", "document_indexes"):
                    self.app.store.execute("DELETE FROM "+table+" WHERE hash=?", (row["hash"],))
            await asyncio.sleep(2)

    async def search(self, message, query, file_ids=None):
        rows = self.app.store.rows("SELECT * FROM files WHERE bot=? AND status='ready' AND (owner=? OR public=1 OR (scope=? AND scope LIKE 'group:%')) ORDER BY created DESC", (message.bot, message.sender, message.scope))
        if file_ids:
            for ident in file_ids:
                self.app.files.resolve(ident, message)
            rows = [r for r in rows if r["id"] in file_ids]
        # Explicit files can be indexed immediately; background work indexes the rest without a model.
        states = []
        for row in rows[:8] if file_ids else []:
            states.append({"file_id": row["id"], **(await self.ensure(row) or {})})
        tokens = sorted(terms(query))
        match = " OR ".join('"'+t.replace('"', '""')+'"' for t in tokens)
        if not match:
            raise ValueError("请输入需要查找的内容")
        permitted = {}
        for row in rows:
            permitted.setdefault(row["hash"], row)
        # ACL join occurs inside SQL before ranking/limit, avoiding inaccessible-file starvation.
        restrict = " AND f.id IN ("+",".join("?" for _ in file_ids)+")" if file_ids else ""
        hits = self.app.store.rows("SELECT s.hash,s.location,c.text,bm25(document_search) AS score FROM document_search s JOIN document_chunks c ON c.hash=s.hash AND c.location=s.location WHERE document_search MATCH ? AND EXISTS(SELECT 1 FROM files f WHERE f.hash=s.hash AND f.bot=? AND f.status='ready' AND (f.owner=? OR f.public=1 OR (f.scope=? AND f.scope LIKE 'group:%'))"+restrict+") ORDER BY score LIMIT 120", [match, message.bot, message.sender, message.scope, *(file_ids or [])])
        results = []
        for hit in hits:
            row = permitted.get(hit["hash"])
            if not row:
                continue
            text = hit["text"]
            where = next((text.lower().find(t.lower()) for t in tokens if t.lower() in text.lower()), 0)
            results.append({"file_id": row["id"], "name": row["name"], "location": hit["location"], "excerpt": text[max(0, where-200):max(0, where-200)+1800], "source_hash": row["hash"]})
            if len(results) == 24:
                break
        return {"matches": results, "index_states": states, "untrusted_document_content": True, "note": "仅检索有权访问的原件；数字和原文引用请核对对应页面。无结果不等于文件没有内容，可能尚未索引或需OCR。"}
