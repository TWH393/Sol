"""SQLite repository shared by all modules; all persisted ownership is explicit."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path


def uid(prefix=""):
    return prefix + uuid.uuid4().hex[:16]


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = root / "companion.sqlite3"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("search_terms", 1, lambda text: " ".join(sorted(set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", (text or "").lower())))))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS messages(
          id TEXT PRIMARY KEY, bot TEXT NOT NULL, scope TEXT NOT NULL, sender TEXT NOT NULL,
          name TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, quote_id TEXT,
          topic TEXT, private INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_scope ON messages(bot,scope,created);
        CREATE TABLE IF NOT EXISTS memories(
          id TEXT PRIMARY KEY, bot TEXT NOT NULL, owner TEXT NOT NULL, subject TEXT NOT NULL,
          scope TEXT NOT NULL, kind TEXT NOT NULL, visibility TEXT NOT NULL, text TEXT NOT NULL,
          evidence TEXT NOT NULL, confidence REAL NOT NULL, valid_until REAL, supersedes TEXT,
          active INTEGER NOT NULL DEFAULT 1, importance REAL NOT NULL DEFAULT 0.5,
          created REAL NOT NULL, updated REAL NOT NULL, accessed REAL);
        CREATE INDEX IF NOT EXISTS memories_owner ON memories(bot,owner,active);
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, bot TEXT NOT NULL, owner TEXT NOT NULL, scope TEXT NOT NULL,
          title TEXT NOT NULL, kind TEXT NOT NULL, autonomous INTEGER NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL, model TEXT NOT NULL, payload TEXT NOT NULL,
          result TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
          updated REAL NOT NULL, fingerprint TEXT, deadline REAL);
        CREATE INDEX IF NOT EXISTS tasks_live ON tasks(bot,status,created);
        CREATE TABLE IF NOT EXISTS requests(
          id TEXT PRIMARY KEY, bot TEXT NOT NULL, scope TEXT NOT NULL, owner TEXT NOT NULL,
          message_id TEXT NOT NULL, task_id TEXT, parent_id TEXT, status TEXT NOT NULL,
          stage TEXT NOT NULL, created REAL NOT NULL, finished REAL, reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS request_messages(
          request_id TEXT NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY(request_id,message_id));
        CREATE INDEX IF NOT EXISTS request_message_lookup ON request_messages(message_id);
        CREATE INDEX IF NOT EXISTS requests_task ON requests(task_id,created);
        CREATE TABLE IF NOT EXISTS request_spans(
          id TEXT PRIMARY KEY, request_id TEXT NOT NULL, task_id TEXT, stage TEXT NOT NULL,
          name TEXT NOT NULL, status TEXT NOT NULL, started REAL NOT NULL, finished REAL,
          detail TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', fault_id TEXT);
        CREATE INDEX IF NOT EXISTS request_spans_time ON request_spans(request_id,started);
        CREATE TABLE IF NOT EXISTS request_memories(
          request_id TEXT NOT NULL, memory_id TEXT NOT NULL, snapshot TEXT NOT NULL,
          selected INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'candidate',
          calls TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(request_id,memory_id));
        CREATE TABLE IF NOT EXISTS faults(
          id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
          stage TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
          count INTEGER NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
          recovered REAL, suggestion TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fault_occurrences(
          id TEXT PRIMARY KEY, fault_id TEXT NOT NULL, request_id TEXT, task_id TEXT,
          span_id TEXT, message_id TEXT, status TEXT NOT NULL, detail TEXT NOT NULL,
          created REAL NOT NULL, recovered REAL);
        CREATE INDEX IF NOT EXISTS fault_occurrences_request ON fault_occurrences(request_id,created);
        CREATE INDEX IF NOT EXISTS fault_occurrences_fault ON fault_occurrences(fault_id,created);
        CREATE TABLE IF NOT EXISTS tool_receipts(
          task_id TEXT NOT NULL, name TEXT NOT NULL, signature TEXT NOT NULL,
          args TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(task_id,name,signature));
        CREATE TABLE IF NOT EXISTS deliveries(
          id TEXT PRIMARY KEY, request_id TEXT, task_id TEXT, bot TEXT NOT NULL,
          scope TEXT NOT NULL, source_message TEXT NOT NULL, file_id TEXT, text TEXT,
          status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
          finished REAL, qq_message_id TEXT);
        CREATE INDEX IF NOT EXISTS deliveries_task ON deliveries(task_id,created);
        CREATE TABLE IF NOT EXISTS artifacts(
          id TEXT PRIMARY KEY, bot TEXT NOT NULL, scope TEXT NOT NULL, owner TEXT NOT NULL,
          title TEXT NOT NULL, latest_file TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS artifact_versions(
          file_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, number INTEGER NOT NULL,
          parent_file TEXT, task_id TEXT NOT NULL, summary TEXT NOT NULL, recipe TEXT NOT NULL,
          sources TEXT NOT NULL, created REAL NOT NULL, UNIQUE(artifact_id,number));
        CREATE INDEX IF NOT EXISTS artifact_task ON artifact_versions(task_id,created);
        CREATE TABLE IF NOT EXISTS future_tasks(
          id TEXT PRIMARY KEY REFERENCES tasks(id), bot TEXT NOT NULL, qq TEXT NOT NULL,
          owner TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL, text TEXT NOT NULL,
          next_due REAL, interval REAL NOT NULL, remaining INTEGER NOT NULL,
          status TEXT NOT NULL, source TEXT NOT NULL, peer TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS future_due ON future_tasks(bot,status,next_due);
        CREATE TABLE IF NOT EXISTS future_runs(
          id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL REFERENCES future_tasks(id),
          due REAL NOT NULL, count INTEGER NOT NULL, status TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT '', started REAL NOT NULL, finished REAL,
          UNIQUE(schedule_id,due));
        CREATE TABLE IF NOT EXISTS task_steps(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, name TEXT NOT NULL, input TEXT NOT NULL,
          result TEXT, status TEXT NOT NULL, started REAL NOT NULL, finished REAL);
        CREATE TABLE IF NOT EXISTS collaborators(
          task_id TEXT NOT NULL, user TEXT NOT NULL, permissions TEXT NOT NULL,
          approved_by TEXT NOT NULL, PRIMARY KEY(task_id,user));
        CREATE TABLE IF NOT EXISTS approvals(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, owner TEXT NOT NULL, requester TEXT NOT NULL,
          scope TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
          id TEXT PRIMARY KEY, bot TEXT NOT NULL, owner TEXT NOT NULL, scope TEXT NOT NULL,
          name TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL, size INTEGER NOT NULL,
          kind TEXT NOT NULL, task_id TEXT, public INTEGER NOT NULL DEFAULT 0,
          favorite INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ready',
          metadata TEXT NOT NULL, created REAL NOT NULL, expires REAL);
        CREATE TABLE IF NOT EXISTS file_refs(
          file_id TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
          PRIMARY KEY(file_id,target_kind,target_id));
        CREATE TABLE IF NOT EXISTS usage(
          id TEXT PRIMARY KEY, bot TEXT NOT NULL, owner TEXT NOT NULL, task_id TEXT,
          model TEXT NOT NULL, source TEXT NOT NULL, input INTEGER, output INTEGER,
          cached INTEGER, reasoning INTEGER, cost REAL, status TEXT NOT NULL,
          duration REAL NOT NULL, breakdown TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS usage_owner ON usage(bot,owner,created);
        CREATE TABLE IF NOT EXISTS persona(
          id TEXT PRIMARY KEY, bot TEXT NOT NULL, text TEXT NOT NULL, dimensions TEXT NOT NULL,
          reason TEXT NOT NULL, evidence TEXT NOT NULL, parent TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS checkpoints(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
          level TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS reports(
          id TEXT PRIMARY KEY, bot TEXT NOT NULL, text TEXT NOT NULL,
          delivered INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS extensions(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
          compatibility TEXT NOT NULL, enabled INTEGER NOT NULL, manifest TEXT NOT NULL,
          error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS preferences(
          bot TEXT NOT NULL, user TEXT NOT NULL, text TEXT NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(bot,user));
        CREATE TABLE IF NOT EXISTS cache(
          key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS document_indexes(hash TEXT PRIMARY KEY,status TEXT NOT NULL,error TEXT,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS document_chunks(hash TEXT NOT NULL,location TEXT NOT NULL,text TEXT NOT NULL,PRIMARY KEY(hash,location));
        CREATE VIRTUAL TABLE IF NOT EXISTS document_search USING fts5(hash UNINDEXED,location UNINDEXED,text);
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts5(id UNINDEXED,text);
        CREATE TRIGGER IF NOT EXISTS memory_search_insert AFTER INSERT ON memories BEGIN
          INSERT INTO memory_search(id,text) VALUES(new.id,search_terms(new.text));
        END;
        CREATE TRIGGER IF NOT EXISTS memory_search_update AFTER UPDATE OF text ON memories BEGIN
          DELETE FROM memory_search WHERE id=old.id;
          INSERT INTO memory_search(id,text) VALUES(new.id,search_terms(new.text));
        END;
        CREATE TRIGGER IF NOT EXISTS memory_search_delete AFTER DELETE ON memories BEGIN
          DELETE FROM memory_search WHERE id=old.id;
        END;
        INSERT INTO memory_search(id,text) SELECT id,search_terms(text) FROM memories WHERE id NOT IN(SELECT id FROM memory_search);
        """)
        self.db.commit()

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def insert(self, table, values):
        columns = list(values)
        self.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(values.values()))

    def event(self, kind, title, detail=None, level="info"):
        import re
        raw = json.dumps(detail or {}, ensure_ascii=False, default=str)
        raw = re.sub(r"(?i)(bearer\s+|sk-)[\w.\-]+", "[redacted]", raw)
        self.insert("events", {"id": uid("e_"), "kind": kind, "title": title, "detail": raw, "level": level, "created": time.time()})
        observer = getattr(self, "event_observer", None)
        if observer:
            observer(kind, title, detail or {}, level)

    def checkpoint(self, key, value=None):
        if value is not None:
            self.execute("INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))
        row = self.one("SELECT value FROM checkpoints WHERE key=?", (key,))
        return json.loads(row["value"]) if row else None

    def cached(self, key, value=None, ttl=300):
        if value is not None:
            self.execute("INSERT INTO cache VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires=excluded.expires", (key, json.dumps(value, ensure_ascii=False), time.time()+ttl))
        row = self.one("SELECT value FROM cache WHERE key=? AND expires>?", (key, time.time()))
        return json.loads(row["value"]) if row else None

    def close(self):
        with self.lock:
            self.db.close()
