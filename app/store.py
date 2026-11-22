import json
import sqlite3
from contextlib import contextmanager

from app.errors import AuditError
from hashlib import sha256

from app.manifest import canonical, now
from app.modules.rectification.planner import build_plan, diff_plans

DDL = (
    '''CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS appeal_events (
        seq INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        shelf_key TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        request_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        event TEXT NOT NULL,
        UNIQUE (run_id, shelf_key, version),
        UNIQUE (run_id, request_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS rectification_plans (
        plan_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        version INTEGER NOT NULL CHECK (version > 0),
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('DRAFT', 'DISPATCHED', 'SUPERSEDED')),
        document TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (run_id, version)
    )''',
    '''CREATE TABLE IF NOT EXISTS rectification_dispatches (
        seq INTEGER PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES rectification_plans(plan_id),
        request_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('SENT', 'FAILED')),
        detail TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (plan_id, request_id)
    )''',
    '''CREATE TABLE IF NOT EXISTS rectification_closures (
        seq INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        shelf_key TEXT NOT NULL,
        request_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        note TEXT NOT NULL,
        evidence_ref TEXT NOT NULL,
        closed_at TEXT NOT NULL,
        UNIQUE (run_id, shelf_key),
        UNIQUE (run_id, request_id)
    )''',
)


def _plan_record(row):
    return {'plan_id': row['plan_id'], 'run_id': row['run_id'], 'version': row['version'],
            'content_hash': row['content_hash'], 'status': row['status'],
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
            **json.loads(row['document'])}


class Store:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def connection(self, write=False):
        connection = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(write=True) as connection:
            for statement in DDL:
                connection.execute(statement)

    @staticmethod
    def read(connection, run_id, required=True):
        found = connection.execute('SELECT snapshot FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if found is None:
            if required:
                raise AuditError('RUN_NOT_FOUND', '未找到该巡检批次', 404)
            return None
        events = connection.execute('SELECT event FROM appeal_events WHERE run_id = ? ORDER BY seq', (run_id,))
        return {**json.loads(found['snapshot']), 'events': [json.loads(event['event']) for event in events]}

    def get(self, run_id, required=True):
        with self.connection() as connection:
            return self.read(connection, run_id, required)

    def save(self, snapshot):
        run_id = snapshot['manifest']['run_id']
        record = canonical(snapshot)
        with self.connection(write=True) as connection:
            inserted = connection.execute('INSERT INTO runs VALUES (?, ?) ON CONFLICT(run_id) DO NOTHING', (run_id, record))
            return self.read(connection, run_id), inserted.rowcount == 0

    def append_event(self, run_id, payload, build):
        with self.connection(write=True) as connection:
            snapshot = self.read(connection, run_id)
            raw = canonical(payload)
            old = connection.execute('SELECT payload, event FROM appeal_events WHERE run_id = ? AND request_id = ?',
                                     (run_id, payload['request_id'])).fetchone()
            if old:
                if old['payload'] != raw:
                    raise AuditError('REQUEST_CONFLICT', '提交编号已用于其他内容', 409)
                return json.loads(old['event']), True
            event = build(snapshot, payload)
            connection.execute('''INSERT INTO appeal_events
                (run_id, shelf_key, version, request_id, payload, event) VALUES (?, ?, ?, ?, ?, ?)''',
                (run_id, event['key'], event['version'], payload['request_id'], raw, canonical(event)))
            return event, False

    # ----- 整改批次 -----

    @staticmethod
    def closures_in(connection, run_id):
        rows = connection.execute('''SELECT shelf_key AS key, actor, note, evidence_ref, closed_at
            FROM rectification_closures WHERE run_id = ? ORDER BY shelf_key''', (run_id,)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def plans_in(connection, run_id):
        rows = connection.execute('SELECT * FROM rectification_plans WHERE run_id = ? ORDER BY version',
                                  (run_id,)).fetchall()
        return [_plan_record(row) for row in rows]

    @staticmethod
    def plan_in(connection, plan_id):
        row = connection.execute('SELECT * FROM rectification_plans WHERE plan_id = ?', (plan_id,)).fetchone()
        if row is None:
            raise AuditError('PLAN_NOT_FOUND', '未找到该整改批次', 404)
        return _plan_record(row)

    def closures(self, run_id):
        self.get(run_id)
        with self.connection() as connection:
            return self.closures_in(connection, run_id)

    def plan(self, plan_id):
        with self.connection() as connection:
            return self.plan_in(connection, plan_id)

    def plans(self, run_id):
        self.get(run_id)
        with self.connection() as connection:
            return self.plans_in(connection, run_id)

    def draft_plan(self, run_id, expected_version=None):
        """按当前异议与销项状态重算计划。每个内容都是不可变版本，旧版本原样保留。"""
        with self.connection(write=True) as connection:
            snapshot = self.read(connection, run_id)
            document = build_plan(snapshot, self.closures_in(connection, run_id))
            versions = self.plans_in(connection, run_id)
            latest = versions[-1] if versions else None
            previous_version = latest['version'] if latest else 0
            if expected_version is not None and expected_version != previous_version:
                raise AuditError('STALE_PLAN', '整改批次已有更新，请刷新', 409, previous_version)
            if latest is not None and latest['content_hash'] == document['content_hash']:
                return {'plan': {**latest, 'diff': {'added': [], 'removed': [], 'changed': []}},
                        'reused': True, 'active': self.active_in(connection, run_id)}
            diff = diff_plans(latest, document)
            version = previous_version + 1
            plan_id = sha256(f'{run_id}\0{version}\0{document["content_hash"]}'.encode()).hexdigest()
            stamp = now()
            connection.execute('''INSERT INTO rectification_plans
                (plan_id, run_id, version, content_hash, status, document, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?)''',
                (plan_id, run_id, version, document['content_hash'], canonical(document), stamp, stamp))
            connection.execute('''UPDATE rectification_plans SET status = 'SUPERSEDED', updated_at = ?
                WHERE run_id = ? AND plan_id != ? AND status = 'DRAFT' ''',
                (stamp, run_id, plan_id))
            return {'plan': {**self.plan_in(connection, plan_id), 'diff': diff}, 'reused': False,
                    'active': self.active_in(connection, run_id)}

    def close_task(self, run_id, payload):
        """销项：同一柜位只落一次；同请求重放返回原记录，改内容返回冲突。"""
        with self.connection(write=True) as connection:
            snapshot = self.read(connection, run_id)
            row = next((item for item in snapshot['penalties']['rows'] if item['key'] == payload['key']), None)
            if row is None:
                raise AuditError('SHELF_NOT_FOUND', '该柜位未在本次抽中', 404)
            if row['status'] != 'VIOLATION':
                raise AuditError('NO_PENALTY', '该柜位没有可销项的违规', 409, row['status'])
            prior = connection.execute('SELECT * FROM rectification_closures WHERE run_id = ? AND request_id = ?',
                                       (run_id, payload['request_id'])).fetchone()
            if prior:
                if (prior['shelf_key'], prior['actor'], prior['note'], prior['evidence_ref']) != (
                        payload['key'], payload['actor'], payload['note'], payload['evidence_ref']):
                    raise AuditError('REQUEST_CONFLICT', '提交编号已用于其他内容', 409)
                return {'key': prior['shelf_key'], 'actor': prior['actor'], 'note': prior['note'],
                        'evidence_ref': prior['evidence_ref'], 'closed_at': prior['closed_at']}, True
            existing = connection.execute('SELECT * FROM rectification_closures WHERE run_id = ? AND shelf_key = ?',
                                          (run_id, payload['key'])).fetchone()
            if existing:
                raise AuditError('ALREADY_CLOSED', '该柜位已销项', 409,
                                 {'closed_at': existing['closed_at'], 'evidence_ref': existing['evidence_ref']})
            stamp = now()
            connection.execute('''INSERT INTO rectification_closures
                (run_id, shelf_key, request_id, actor, note, evidence_ref, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (run_id, payload['key'], payload['request_id'], payload['actor'], payload['note'],
                 payload['evidence_ref'], stamp))
            return {'key': payload['key'], 'actor': payload['actor'], 'note': payload['note'],
                    'evidence_ref': payload['evidence_ref'], 'closed_at': stamp}, False

    def active_in(self, connection, run_id):
        """现行计划：最近一次成功下发的版本。草稿或失败尝试都不会顶替它。"""
        row = connection.execute('''SELECT * FROM rectification_plans
            WHERE run_id = ? AND status = 'DISPATCHED' ORDER BY version DESC LIMIT 1''', (run_id,)).fetchone()
        return _plan_record(row) if row else None

    def active_plan(self, run_id):
        self.get(run_id)
        with self.connection() as connection:
            return self.active_in(connection, run_id)

    def dispatch_plan(self, plan_id, payload, send):
        """下发：同一版重试不新增任务；发送失败时新版本保持草稿，旧版仍是现行版。"""
        with self.connection(write=True) as connection:
            plan = self.plan_in(connection, plan_id)
            raw = canonical(payload)
            prior = connection.execute('SELECT * FROM rectification_dispatches WHERE plan_id = ? AND request_id = ?',
                                       (plan_id, payload['request_id'])).fetchone()
            if prior:
                if prior['payload'] != raw:
                    raise AuditError('REQUEST_CONFLICT', '提交编号已用于其他内容', 409)
                if prior['outcome'] == 'SENT':
                    return {'plan': self.plan_in(connection, plan_id), 'outcome': 'SENT', 'reused': True,
                            'active': self.active_in(connection, plan['run_id'])}
                # 上次失败的尝试可用同一请求编号重试：不新增下发记录，也不重复生成任务。
                return self._send(connection, plan, payload, raw, prior['seq'], send)
            if plan['status'] == 'SUPERSEDED':
                raise AuditError('PLAN_SUPERSEDED', '该整改批次已被新版本替代，不能下发', 409, plan['version'])
            # 异议或销项变化后，旧版金额可能过期；下发前必须与当前结算状态一致。
            current = build_plan(self.read(connection, plan['run_id']), self.closures_in(connection, plan['run_id']))
            if current['content_hash'] != plan['content_hash']:
                raise AuditError('PLAN_STALE', '计划与当前结算不一致，请先生成新版本', 409, plan['version'])
            if plan['status'] == 'DISPATCHED':
                connection.execute('''INSERT INTO rectification_dispatches
                    (plan_id, request_id, payload, outcome, detail, created_at) VALUES (?, ?, ?, 'SENT', ?, ?)''',
                    (plan_id, payload['request_id'], raw, '已下发，重复提交不新增任务', now()))
                return {'plan': plan, 'outcome': 'SENT', 'reused': True,
                        'active': self.active_in(connection, plan['run_id'])}
            return self._send(connection, plan, payload, raw, None, send)

    def _send(self, connection, plan, payload, raw, retry_seq, send):
        """真正执行下发；成功才把该版设为现行版，失败只追加或更新失败尝试。"""
        plan_id, stamp = plan['plan_id'], now()
        try:
            detail = send(plan) or '已下发'
        except AuditError as error:
            detail, outcome = error.code, 'FAILED'
        else:
            outcome = 'SENT'
        if retry_seq is None:
            connection.execute('''INSERT INTO rectification_dispatches
                (plan_id, request_id, payload, outcome, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)''',
                (plan_id, payload['request_id'], raw, outcome, detail, stamp))
        else:
            connection.execute('UPDATE rectification_dispatches SET outcome = ?, detail = ?, created_at = ? WHERE seq = ?',
                               (outcome, detail, stamp, retry_seq))
        if outcome == 'FAILED':
            return {'plan': self.plan_in(connection, plan_id), 'outcome': 'FAILED', 'reused': False,
                    'active': self.active_in(connection, plan['run_id']),
                    'error': {'code': detail, 'message': '下发通道返回失败'}}
        connection.execute("UPDATE rectification_plans SET status = 'DISPATCHED', updated_at = ? WHERE plan_id = ?",
                           (stamp, plan_id))
        connection.execute('''UPDATE rectification_plans SET status = 'SUPERSEDED', updated_at = ?
            WHERE run_id = ? AND plan_id != ? AND status = 'DISPATCHED' ''',
            (stamp, plan['run_id'], plan_id))
        return {'plan': self.plan_in(connection, plan_id), 'outcome': 'SENT', 'reused': retry_seq is not None,
                'active': self.active_in(connection, plan['run_id'])}

    def dispatches(self, plan_id):
        with self.connection() as connection:
            self.plan_in(connection, plan_id)
            rows = connection.execute('''SELECT request_id, outcome, detail, created_at
                FROM rectification_dispatches WHERE plan_id = ? ORDER BY seq''', (plan_id,)).fetchall()
            return [dict(row) for row in rows]
