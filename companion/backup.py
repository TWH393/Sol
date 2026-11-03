"""Consistent SQLite backups and explicit file manifests, without task resurrection."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path


class Backups:
    def __init__(self, app):
        self.app = app
        self.root = app.store.root / "backups"
        self.root.mkdir(exist_ok=True)
        self.lock = asyncio.Lock()

    async def create(self, reason="manual", include_files=True):
        async with self.lock:
            return await asyncio.to_thread(self._write, reason, include_files)

    def _write(self, reason, include_files):
        name = time.strftime("%Y%m%d-%H%M%S")+"-"+str(time.time_ns() % 1000000)+".zip"
        destination = self.root / name
        snapshot = self.root / (name+".sqlite3")
        connection = sqlite3.connect(snapshot)
        try:
            with self.app.store.lock:
                self.app.store.db.backup(connection)
        finally:
            connection.close()
        rows = self.app.store.rows("SELECT DISTINCT f.path,f.hash FROM files f WHERE f.status='ready' AND (f.favorite=1 OR EXISTS(SELECT 1 FROM file_refs r WHERE r.file_id=f.id))") if include_files else []
        manifest = {"version": 1, "data_root": str(self.app.store.root), "reason": reason, "created": time.time(), "files": [], "secrets": "excluded; reauthorize after moving machines"}
        try:
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, "companion.sqlite3")
                archive.write(self.app.config.path, "settings.json")
                for row in rows:
                    path = Path(row["path"])
                    if path.is_file() and path.is_relative_to(self.app.files.root):
                        relative = path.relative_to(self.app.store.root).as_posix()
                        archive.write(path, relative)
                        manifest["files"].append({"path": relative, "hash": row["hash"]})
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        finally:
            snapshot.unlink(missing_ok=True)
        self.app.store.checkpoint("last_backup", time.time())
        self.app.store.event("backup", "备份已保存", {"name": name, "files": len(rows)})
        keep = int(self.app.config.get("app", "backup_keep"))
        for old in sorted(self.root.glob("*.zip"), reverse=True)[max(1, keep):]:
            old.unlink()
        return {"name": name, "bytes": destination.stat().st_size, "files": len(rows)}

    async def prepare_restore(self, name):
        path = (self.root / name).resolve()
        if path.parent != self.root.resolve() or not path.is_file():
            raise ValueError("备份不存在")
        await self.create("before-restore")
        target = self.app.store.root / "restore-next-start"
        if target.exists():
            target.rename(self.app.store.root / ("restore-superseded-"+str(time.time_ns())))
        target.mkdir(exist_ok=True)
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                destination = (target / entry.filename).resolve()
                if not destination.is_relative_to(target.resolve()):
                    raise ValueError("备份包含无效路径")
                archive.extract(entry, target)
        return {"restart_required": True, "message": "恢复点已准备，下次启动应用时恢复；任务保持中断"}

    @staticmethod
    def apply_restore(root):
        staged = root / "restore-next-start"
        if not staged.is_dir():
            return
        manifest = json.loads((staged / "manifest.json").read_text("utf-8"))
        for row in manifest.get("files", []):
            path = (staged / row["path"]).resolve()
            if not path.is_relative_to(staged.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != row["hash"]:
                raise ValueError("备份文件校验不匹配，未应用恢复")
        for name in ("companion.sqlite3", "settings.json"):
            shutil.copy2(staged / name, root / name)
        for suffix in ("-wal", "-shm"):
            (root / ("companion.sqlite3"+suffix)).unlink(missing_ok=True)
        if (staged / "files").exists():
            shutil.copytree(staged / "files", root / "files", dirs_exist_ok=True)
        # Paths are local to the restored data root, never read the old installation.
        connection = sqlite3.connect(root / "companion.sqlite3")
        try:
            for ident, digest, old in connection.execute("SELECT id,hash,path FROM files").fetchall():
                path = root / "files" / digest[:2] / (digest+Path(old).suffix.lower())
                connection.execute("UPDATE files SET path=?,status=CASE WHEN ? THEN status ELSE 'missing' END WHERE id=?", (str(path), int(path.is_file()), ident))
            connection.execute("UPDATE tasks SET status='interrupted',stage='从备份恢复，未续跑',error='恢复备份不恢复任务' WHERE status IN ('queued','running','scheduled','waiting_approval')")
            # A backup can predate already delivered reminders. Keep arrangements
            # visible but require review, rather than replaying the old snapshot.
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='future_tasks'").fetchone():
                connection.execute("UPDATE future_tasks SET status='paused',error='从备份恢复，请核对QQ记录后手动恢复',updated=? WHERE status IN ('active','paused')", (time.time(),))
                connection.execute("UPDATE future_runs SET status='unknown',error='从备份恢复，历史送达状态需核对',finished=? WHERE status='sending'", (time.time(),))
            connection.execute("DELETE FROM collaborators")
            connection.execute("UPDATE approvals SET status='expired' WHERE status='pending'")
            for ident, raw in connection.execute("SELECT id,manifest FROM extensions").fetchall():
                record = json.loads(raw)
                old_base = Path(manifest.get("data_root") or str(root))
                current = Path(record.get("root", ""))
                if current.is_relative_to(old_base):
                    current = root / current.relative_to(old_base)
                    record["root"] = str(current)
                connection.execute("UPDATE extensions SET manifest=?,enabled=CASE WHEN ? THEN enabled ELSE 0 END,error=CASE WHEN ? THEN error ELSE '扩展源码未包含在此备份，需要重新安装' END WHERE id=?", (json.dumps(record, ensure_ascii=False), int(current.is_dir()), int(current.is_dir()), ident))
            connection.commit()
        finally:
            connection.close()
        shutil.rmtree(staged)
