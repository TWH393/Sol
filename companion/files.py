"""Content-addressed artifacts with logical ownership and retention references."""
from __future__ import annotations

import hashlib
import base64
import io
import json
import mimetypes
import time
from pathlib import Path

from .storage import uid


class Files:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.root = store.root / "files"
        self.root.mkdir(exist_ok=True)

    def add(self, data, name, message, task_id=None, kind="original", public=False, metadata=None):
        if not isinstance(data, bytes) or len(data) > self.config.get("files", "max_mb")*1024*1024:
            raise ValueError("文件超过当前大小限制")
        name = Path(name.replace("\\", "/")).name
        if not name or name in {".", ".."}:
            raise ValueError("文件名无效")
        digest = hashlib.sha256(data).hexdigest()
        existing = self.store.one("SELECT path FROM files WHERE hash=? AND status='ready' LIMIT 1", (digest,))
        target = Path(existing["path"]) if existing and Path(existing["path"]).is_file() else self.root / digest[:2] / (digest+Path(name).suffix.lower())
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            temp = target.with_suffix(target.suffix+".part")
            temp.write_bytes(data)
            temp.replace(target)
        ident, now = uid("f_"), time.time()
        days = self.config.get("files", "temporary_days" if kind == "temporary" else "normal_days")
        task = self.store.one("SELECT owner FROM tasks WHERE id=?", (task_id,)) if task_id and kind != "original" else None
        self.store.insert("files", {"id": ident, "bot": message.bot, "owner": task["owner"] if task else "@robot" if message.autonomous and kind != "original" else message.sender, "scope": message.scope,
            "name": name, "path": str(target), "hash": digest, "size": len(data), "kind": kind,
            "task_id": task_id, "public": int(public), "metadata": json.dumps(metadata or {}, ensure_ascii=False), "created": now, "expires": now+days*86400})
        return ident

    def resolve(self, ident, message=None, admin=False):
        row = self.store.one("SELECT * FROM files WHERE id=?", (ident,))
        if not row or row["status"] != "ready" or not Path(row["path"]).is_file():
            raise FileNotFoundError("文件不存在或已被清理")
        if not admin and message is not None:
            accessible = row["bot"] == message.bot and (row["owner"] == message.sender or row["public"] or row["scope"] == message.scope and not message.private)
            if not accessible:
                raise PermissionError("无权读取该文件")
        return row, Path(row["path"])

    def reference(self, ident, kind, target):
        self.store.execute("INSERT OR IGNORE INTO file_refs VALUES(?,?,?)", (ident, kind, target))

    def mark_model_inputs(self, inputs, message, provider, state, error=""):
        """Record what a request contains; a successful request is not proof of comprehension."""
        for ident, detail in inputs.items():
            try:
                row, _ = self.resolve(ident, message)
            except (FileNotFoundError, PermissionError):
                continue
            metadata = json.loads(row["metadata"])
            metadata["last_model_input"] = {**detail, "state": state, "model": provider.get("model", provider["id"]),
                "updated": time.time(), "error": error[:240], "source_message": message.id}
            self.store.execute("UPDATE files SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), ident))

    def image_part(self, ident, message):
        from PIL import Image, ImageOps
        _, path = self.resolve(ident, message)
        with Image.open(path) as source:
            if source.width*source.height > 16000000:
                raise ValueError("图片超过1600万像素，请裁出需要分析的部分")
            image = ImageOps.exif_transpose(source)
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = io.BytesIO()
            image.save(output, "PNG")
        return {"type":"image_url","image_url":{"url":"data:image/png;base64,"+base64.b64encode(output.getvalue()).decode()}}

    def alias(self, ident, message, task_id):
        row, _ = self.resolve(ident, message)
        original = row["id"]
        row.update(id=uid("f_"), owner="@robot" if message.autonomous else message.sender, scope=message.scope, task_id=task_id, favorite=0, created=time.time(), expires=time.time()+self.config.get("files", "normal_days")*86400)
        metadata = json.loads(row["metadata"])
        metadata["reused_from"] = original
        row["metadata"] = json.dumps(metadata, ensure_ascii=False)
        self.store.insert("files", row)
        return row["id"]

    def read(self, ident, message, pages=None):
        row, path = self.resolve(ident, message)
        ext = path.suffix.lower()
        if ext == ".pdf":
            # Ordinary text extraction must not import the optional PyMuPDF
            # layout/NumPy/ONNX stack. Raster rendering stays a separate tool.
            from pypdf import PdfReader
            with path.open("rb") as source:
                doc = PdfReader(source)
                if doc.is_encrypted:
                    raise ValueError("PDF 已加密，需要先解密")
                count = len(doc.pages)
                requested = pages or list(range(1, min(count, 10)+1))
                if any(not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > count for n in requested):
                    raise ValueError("页码超出范围")
                content = [{"page": n, "text": doc.pages[n-1].extract_text() or ""} for n in requested]
                return {"name": row["name"], "pages": count, "content": content, "unread_pages": count-len(set(requested)),
                        "empty_text_pages": [item["page"] for item in content if not item["text"].strip()],
                        "extraction": "text_only", "instruction": "空文本页可能是扫描件；需要实际页面图像时调用render_document_pages，不要猜测内容。"}
        if ext == ".docx":
            from docx import Document
            doc = Document(path)
            return {"text": "\n".join(p.text for p in doc.paragraphs)+"\n"+"\n".join(" | ".join(c.text for c in r.cells) for t in doc.tables for r in t.rows)}
        if ext == ".pptx":
            from pptx import Presentation
            return {"slides": [{"slide": i+1, "text": "\n".join(s.text for s in slide.shapes if s.has_text_frame)} for i, slide in enumerate(Presentation(path).slides)]}
        if ext in {".xlsx", ".xlsm"}:
            import openpyxl
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                return {"sheets": {s.title: list(s.values)[:2000] for s in book.worksheets}}
            finally:
                book.close()
        if ext in {".txt", ".md", ".csv", ".json", ".py", ".sh", ".js", ".css", ".yaml", ".yml", ".xml", ".html", ".srt", ".vtt"}:
            return {"text": path.read_text("utf-8-sig", errors="replace")}
        if mimetypes.guess_type(row["name"])[0] and mimetypes.guess_type(row["name"])[0].startswith("image/"):
            return {"image": ident, "path": str(path)}
        raise ValueError("这个格式需要专用工具读取")

    def cleanup(self):
        self.store.execute("DELETE FROM file_refs WHERE target_kind='file' AND NOT EXISTS(SELECT 1 FROM files WHERE files.id=file_refs.target_id AND files.status='ready')")
        candidates = self.store.rows("SELECT f.* FROM files f WHERE f.status='ready' AND f.favorite=0 AND f.expires<? AND NOT EXISTS(SELECT 1 FROM file_refs r WHERE r.file_id=f.id) AND NOT EXISTS(SELECT 1 FROM tasks t WHERE t.id=f.task_id AND t.status IN ('running','queued','scheduled','waiting_approval'))", (time.time(),))
        for row in candidates:
            self.store.execute("UPDATE files SET status='expired' WHERE id=?", (row["id"],))
            others = self.store.one("SELECT COUNT(*) AS n FROM files WHERE path=? AND status='ready'", (row["path"],))["n"]
            if not others:
                path = Path(row["path"]).resolve()
                if path.is_relative_to(self.root.resolve()):
                    path.unlink(missing_ok=True)
        jobs = (self.store.root / "jobs").resolve()
        if jobs.is_dir():
            import shutil
            cutoff = time.time()-max(1, self.config.get("files", "temporary_days"))*86400
            for folder in jobs.iterdir():
                if not folder.is_dir() or folder.is_symlink() or not folder.resolve().is_relative_to(jobs) or folder.stat().st_mtime >= cutoff:
                    continue
                live = self.store.one("SELECT id FROM tasks WHERE id=? AND status IN ('queued','running','waiting_approval','scheduled')", (folder.name,))
                if not live:
                    shutil.rmtree(folder)
