"""Local accounting metadata only: no prompts, replies or credentials."""
from contextlib import contextmanager
import json
import sqlite3
import time
import uuid
from datetime import datetime

class UsageLog:
    def __init__(self,path):
        self.path=str(path)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, started REAL, finished REAL, model TEXT,
                source TEXT, account TEXT, status TEXT, error_code TEXT,
                input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                reasoning_tokens INTEGER, total_tokens INTEGER)''')
            db.execute('CREATE INDEX IF NOT EXISTS calls_started ON calls(started)')
            columns={row[1] for row in db.execute('PRAGMA table_info(calls)')}
            if 'qq_user' not in columns:
                # SQLite backup runs in the actual bridge process/data directory.
                backup=self.path+'.before-qq-users.sqlite3'
                from pathlib import Path
                if not Path(backup).exists():
                    # Commit schema creation before taking the migration snapshot.
                    db.commit()
                    snapshot=sqlite3.connect(backup)
                    try: db.backup(snapshot)
                    finally: snapshot.close()
            for name,kind in [('purpose','TEXT'),('input_chars','INTEGER'),('image_count','INTEGER'),('message_count','INTEGER'),('tool_count','INTEGER'),('qq_user','TEXT'),('input_breakdown','TEXT')]:
                if name not in columns:db.execute(f'ALTER TABLE calls ADD COLUMN {name} {kind}')
            db.execute('CREATE INDEX IF NOT EXISTS calls_qq_started ON calls(qq_user,started)')
            db.execute('CREATE TABLE IF NOT EXISTS qq_users (qq_user TEXT PRIMARY KEY, is_admin INTEGER NOT NULL DEFAULT 0, daily_tokens INTEGER, last_seen REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS qq_policy (id INTEGER PRIMARY KEY CHECK(id=1), daily_tokens INTEGER NOT NULL DEFAULT 0)')
            db.execute('INSERT OR IGNORE INTO qq_policy(id,daily_tokens) VALUES(1,0)')
            db.execute('CREATE TABLE IF NOT EXISTS qq_quota_resets (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, reset_at REAL NOT NULL)')

    def reset_quota(self, payload):
        if not isinstance(payload, dict) or set(payload) not in ({'qq_user'}, {'all_users'}):
            raise ValueError('请选择一个 QQ 用户，或全部用户。')
        now = time.time()
        with self.connect() as db:
            if 'all_users' in payload:
                if payload['all_users'] is not True:
                    raise ValueError('全部重置参数无效。')
                scope = '*'
                count = db.execute('SELECT COUNT(*) FROM qq_users').fetchone()[0]
            else:
                scope = payload['qq_user']
                if (not isinstance(scope, str) or not scope.isascii() or not scope.isdigit()
                        or not 5 <= len(scope) <= 20):
                    raise ValueError('请输入有效 QQ 号。')
                if not db.execute('SELECT 1 FROM qq_users WHERE qq_user=?', (scope,)).fetchone():
                    raise ValueError('未找到该用户。')
                count = 1
            # Retain immutable usage records and policies; only move the quota baseline.
            db.execute('INSERT INTO qq_quota_resets(scope,reset_at) VALUES(?,?)', (scope, now))
        return {'reset': True, 'affected_users': count, 'reset_at': now}

    def quota_start(self, db, user):
        reset = db.execute('SELECT MAX(reset_at) FROM qq_quota_resets WHERE scope IN (?,\'*\')', (user,)).fetchone()[0]
        return max(self.day_start(), reset or 0)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db: yield db
        finally: db.close()

    def begin(self,model,source,account,metrics=None,identity=None):
        identifier=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO calls(id,started,model,source,account,status) VALUES(?,?,?,?,?,?)',
                (identifier,time.time(),str(model)[:100],source,account,'pending'))
            metrics=metrics or {}
            if identity:
                db.execute('INSERT INTO qq_users(qq_user,is_admin,last_seen) VALUES(?,?,?) ON CONFLICT(qq_user) DO UPDATE SET is_admin=excluded.is_admin,last_seen=excluded.last_seen',
                    (identity['qq_user'],int(identity['is_admin']),time.time()))
                db.execute('UPDATE calls SET qq_user=? WHERE id=?',(identity['qq_user'],identifier))
            db.execute('UPDATE calls SET purpose=?,input_chars=?,image_count=?,message_count=?,tool_count=?,input_breakdown=? WHERE id=?',
                (metrics.get('purpose'),metrics.get('input_chars'),metrics.get('image_count'),metrics.get('message_count'),metrics.get('tool_count'),json.dumps(metrics['input_breakdown'],ensure_ascii=False) if metrics.get('input_breakdown') else None,identifier))
        return identifier

    @staticmethod
    def day_start():
        return datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()

    def save_policy(self,payload):
        def valid(value):
            if type(value) is not int or not 0 <= value <= 1_000_000_000:
                raise ValueError('每日 token 上限必须是 0 到 1000000000 的整数；0 表示不限。')
            return value
        with self.connect() as db:
            if 'daily_tokens' in payload:
                db.execute('UPDATE qq_policy SET daily_tokens=? WHERE id=1',(valid(payload['daily_tokens']),))
            if 'qq_user' in payload:
                user=str(payload['qq_user'])
                if not user.isascii() or not user.isdigit() or not 5 <= len(user) <= 20:
                    raise ValueError('请输入有效 QQ 号。')
                value=payload.get('user_daily_tokens')
                if value is not None: value=valid(value)
                db.execute('INSERT INTO qq_users(qq_user,daily_tokens) VALUES(?,?) ON CONFLICT(qq_user) DO UPDATE SET daily_tokens=excluded.daily_tokens',(user,value))

    def quota_error(self,identity):
        if not identity or identity['is_admin']: return None
        with self.connect() as db:
            override=db.execute('SELECT daily_tokens FROM qq_users WHERE qq_user=?',(identity['qq_user'],)).fetchone()
            limit=override[0] if override and override[0] is not None else db.execute('SELECT daily_tokens FROM qq_policy WHERE id=1').fetchone()[0]
            if not limit: return None
            used,missing=db.execute("SELECT COALESCE(SUM(total_tokens),0),COALESCE(SUM(status='success' AND total_tokens IS NULL),0) FROM calls WHERE qq_user=? AND started>=?",(identity['qq_user'],self.quota_start(db,identity['qq_user']))).fetchone()
        if missing: return '今天有调用未返回用量，暂时停止后续模型调用，请管理员检查统计。'
        if used >= limit: return f'你今天的模型用量已达到每日上限（已用 {used:,} / 上限 {limit:,} token），明天零点恢复，或联系管理员调整。'
        return None

    def user_report(self,window='today'):
        start={'today':self.day_start(),'5h':time.time()-18000,'all':0}[window]
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            default=db.execute('SELECT daily_tokens FROM qq_policy WHERE id=1').fetchone()[0]
            rows=[dict(r) for r in db.execute('''SELECT u.*,COUNT(c.id) calls,COALESCE(SUM(c.status='success'),0) success,
                COALESCE(SUM(c.status='failed'),0) failed,COALESCE(SUM(c.status='pending'),0) pending,
                COALESCE(SUM(c.status='success' AND c.total_tokens IS NULL),0) missing_usage,
                COALESCE(SUM(c.input_tokens),0) input_tokens,COALESCE(SUM(c.output_tokens),0) output_tokens,
                COALESCE(SUM(c.cached_tokens),0) cached_tokens,COALESCE(SUM(c.total_tokens),0) total_tokens
                FROM qq_users u LEFT JOIN calls c ON c.qq_user=u.qq_user AND c.started>=?
                GROUP BY u.qq_user ORDER BY total_tokens DESC,u.qq_user''',(start,))]
            daily={r['qq_user']:dict(r) for r in db.execute('''SELECT qq_user,COALESCE(SUM(total_tokens),0) used,
                SUM(status='success' AND total_tokens IS NULL) missing FROM calls WHERE qq_user IS NOT NULL AND started>=? GROUP BY qq_user''',(self.day_start(),))}
            unassigned=dict(db.execute('''SELECT COUNT(*) calls,COALESCE(SUM(total_tokens),0) total_tokens FROM calls WHERE qq_user IS NULL AND started>=?''',(start,)).fetchone())
            quota = {}
            for row in rows:
                since = self.quota_start(db, row['qq_user'])
                used, missing = db.execute("SELECT COALESCE(SUM(total_tokens),0),COALESCE(SUM(status='success' AND total_tokens IS NULL),0) FROM calls WHERE qq_user=? AND started>=?", (row['qq_user'], since)).fetchone()
                quota[row['qq_user']] = (used, missing, since)
        for row in rows:
            row['effective_limit']=0 if row['is_admin'] else (default if row['daily_tokens'] is None else row['daily_tokens'])
            row['today_tokens']=daily.get(row['qq_user'],{}).get('used',0)
            row['today_missing_usage']=daily.get(row['qq_user'],{}).get('missing',0) or 0
            row['quota_used_tokens'], row['quota_missing_usage'], since = quota[row['qq_user']]
            row['quota_reset_at'] = since if since > self.day_start() else None
            row['remaining']=max(0,row['effective_limit']-row['quota_used_tokens']) if row['effective_limit'] else None
        return {'window':window,'daily_tokens':default,'users':rows,'unassigned':unassigned,'quota_reset_supported':True,'reset_at':datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()+86400,
            'note':'输入和输出 token 合计；缓存 token 已包含在输入内。统计每次模型调用，包含图片规划、检查和历史压缩，不等同于聊天消息数。达到上限后阻止后续调用，最后一次可能超过上限。每天按本机时间零点重置；重启保留记录。旧记录和开发测试无法归属用户。'}

    def finish(self,identifier,result=None,error=None):
        usage=(result or {}).get('usage') or {}
        def count(value): return value if type(value) is int and value>=0 else None
        input_details=usage.get('input_tokens_details') or {}
        output_details=usage.get('output_tokens_details') or {}
        with self.connect() as db:
            db.execute('''UPDATE calls SET finished=?,status=?,error_code=?,input_tokens=?,output_tokens=?,
                cached_tokens=?,reasoning_tokens=?,total_tokens=? WHERE id=?''',
                (time.time(),'failed' if error else 'success',error,
                 count(usage.get('input_tokens')),count(usage.get('output_tokens')),
                 count(input_details.get('cached_tokens')),count(output_details.get('reasoning_tokens')),
                 count(usage.get('total_tokens')),identifier))

    def report(self,window='5h'):
        now=time.time()
        start={'5h':now-5*3600,'today':datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp(),'all':0}[window]
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            totals=dict(db.execute('''SELECT COUNT(*) AS calls,
                SUM(status='success') AS success,SUM(status='failed') AS failed,SUM(status='pending') AS unfinished,
                SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
                SUM(cached_tokens) AS cached_tokens,SUM(reasoning_tokens) AS reasoning_tokens,SUM(total_tokens) AS total_tokens,
                SUM(status='success' AND total_tokens IS NULL) AS missing_usage
                FROM calls WHERE started>=?''',(start,)).fetchone())
            sources=[dict(row) for row in db.execute('''SELECT source,COUNT(*) AS calls,SUM(total_tokens) AS total_tokens
                FROM calls WHERE started>=? GROUP BY source''',(start,))]
            recent=[dict(row) for row in db.execute('''SELECT started,finished,model,source,status,error_code,
                input_tokens,output_tokens,total_tokens,purpose,input_chars,image_count,message_count,tool_count,input_breakdown FROM calls WHERE started>=? ORDER BY started DESC LIMIT 50''',(start,))]
            purposes=[dict(row) for row in db.execute('''SELECT COALESCE(purpose,'unclassified') purpose,COUNT(*) calls,
                SUM(total_tokens) total_tokens,SUM(input_tokens) input_tokens FROM calls WHERE started>=? GROUP BY purpose''',(start,))]
            first=db.execute('SELECT MIN(started) FROM calls').fetchone()[0]
            details=[json.loads(row[0]) for row in db.execute('SELECT input_breakdown FROM calls WHERE started>=? AND input_breakdown IS NOT NULL',(start,))]
        breakdown={'recorded_calls':len(details),'missing_calls':totals['calls']-len(details),'categories':{},'media':{},'estimated_text_tokens':0}
        for detail in details:
            breakdown['estimated_text_tokens']+=detail['estimated_text_tokens']
            for name,value in detail['categories'].items():
                target=breakdown['categories'].setdefault(name,{'estimated_tokens':0,'chars':0})
                for key in target:target[key]+=value[key]
            for name,value in detail['media'].items():breakdown['media'][name]=breakdown['media'].get(name,0)+value
        for call in recent:
            call['input_breakdown']=json.loads(call['input_breakdown']) if call['input_breakdown'] else None
        for key in ('success','failed','unfinished','missing_usage'): totals[key]=totals[key] or 0
        return {'window':window,'since':start,'recording_since':first,'totals':totals,'sources':sources,'purposes':purposes,'recent':recent,'input_breakdown':breakdown,
            'five_hour_percent':None,'note':'最近5小时为滚动统计，不是官方额度窗口；token数量不能换算套餐百分比。重启前未完成的调用保留为未完成，可能已消耗额度。缺失usage不会当作0。'}
