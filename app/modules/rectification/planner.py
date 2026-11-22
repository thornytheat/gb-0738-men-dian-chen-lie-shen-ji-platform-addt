from hashlib import sha256

from app.errors import AuditError
from app.manifest import canonical
from app.modules.appeals.service import appeal_balances
from app.reasons import REASONS

VIOLATION = 'VIOLATION'


def _digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def task_identity(run_id, shelf_key):
    """任务身份只由批次和柜位决定：改判或重排版本不会复制出第二个任务。"""
    return sha256(f'{run_id}\0task\0{shelf_key}'.encode()).hexdigest()[:32]


def order_identity(run_id, store):
    return sha256(f'{run_id}\0order\0{store}'.encode()).hexdigest()[:32]


def _photos(evidence):
    """柜位可独立的图证链：保留全部有效原件，而不只第一条。"""
    shots = (evidence or {}).get('photos') or []
    return [{'photo': shot['photo'], 'file': shot['file'], 'sha256': shot['sha256'],
             'source': shot['source']['id']} for shot in shots]


def _proof_sources(applied):
    proof = (applied or {}).get('proof') or {}
    names = [*proof.get('sources', []), *proof.get('completed_sources', [])]
    return sorted({name for name in names if isinstance(name, str)})


def _store_titles(snapshot):
    return {row['store']: row['title'] for row in snapshot['intake']['stores']}


def _group_order(snapshot):
    return [group['code'] for group in snapshot['intake']['campaign']['groups']]


def build_plan(snapshot, closures=()):
    """把结算批次中的每个违规柜位落成恰好一条任务，并按门店合成可下发的工单。"""
    run_id = snapshot['manifest']['run_id']
    balances = {row['key']: row for row in appeal_balances(snapshot)['rows']}
    closed = {row['key']: row for row in closures}
    titles = _store_titles(snapshot)
    order = {code: index for index, code in enumerate(_group_order(snapshot))}
    tasks = []
    for row in snapshot['penalties']['rows']:
        if row['status'] != VIOLATION:
            continue
        balance = balances.get(row['key'])
        if balance is None:
            raise AuditError('RECTIFICATION_LEDGER_MISMATCH', '违规柜位缺少异议核算', 500, row['key'])
        applied = row['applied'] or {}
        closure = closed.get(row['key'])
        tasks.append({
            'task_id': task_identity(run_id, row['key']),
            'key': row['key'], 'store': row['store'], 'slot': row['slot'], 'group': row['group'],
            'category': row['category'],
            'rule': applied.get('code'), 'rule_label': REASONS.get(applied.get('code'), applied.get('code')),
            'original_cents': row['original_cents'],
            'reduction_cents': balance['reduction_cents'],
            'amount_cents': balance['net_cents'],
            'appeal_status': balance['status'], 'appeal_version': balance['version'],
            'photos': _photos(row.get('evidence')),
            'proof_sources': _proof_sources(applied),
            'plan_source': (row.get('plan') or {}).get('source', {}).get('id'),
            'closed': closure is not None,
            'closure': ({'actor': closure['actor'], 'note': closure['note'],
                         'evidence_ref': closure['evidence_ref'], 'closed_at': closure['closed_at']}
                        if closure else None),
        })
    tasks.sort(key=lambda item: (order.get(item['group'], len(order)), item['store'], item['slot']))

    grouped = {}
    for task in tasks:
        grouped.setdefault(task['store'], []).append(task)
    orders = []
    for store in sorted(grouped):
        members = grouped[store]
        open_ids = [task['task_id'] for task in members if not task['closed']]
        closed_ids = [task['task_id'] for task in members if task['closed']]
        orders.append({
            'order_id': order_identity(run_id, store), 'store': store,
            'store_title': titles.get(store, store),
            # 门店只收一张工单；柜位任务仍独立，销项后的柜位移入 closed_task_ids 以便继续追溯。
            'task_ids': [task['task_id'] for task in members],
            'open_task_ids': open_ids, 'closed_task_ids': closed_ids,
            'original_cents': sum(task['original_cents'] for task in members),
            'reduction_cents': sum(task['reduction_cents'] for task in members),
            'amount_cents': sum(task['amount_cents'] for task in members),
            'open_tasks': len(open_ids), 'closed_tasks': len(closed_ids),
        })

    totals = {
        'tasks': len(tasks), 'orders': len(orders),
        'original_cents': sum(task['original_cents'] for task in tasks),
        'reduction_cents': sum(task['reduction_cents'] for task in tasks),
        'amount_cents': sum(task['amount_cents'] for task in tasks),
        'closed_tasks': sum(task['closed'] for task in tasks),
        'open_tasks': sum(not task['closed'] for task in tasks),
    }
    reconciliation = _reconcile(snapshot, tasks, orders, totals, balances)
    content = {'run_id': run_id, 'tasks': tasks, 'orders': orders, 'totals': totals,
               'reconciliation': reconciliation}
    return {**content, 'content_hash': _digest(content)}


def _reconcile(snapshot, tasks, orders, totals, balances):
    settlement = snapshot['settlement']
    appeals = appeal_balances(snapshot)
    violation_rows = {row['key']: row for row in snapshot['penalties']['rows'] if row['status'] == VIOLATION}
    plan_slots = {task['key']: task for task in tasks}
    if set(plan_slots) != set(violation_rows) or len(tasks) != len(violation_rows):
        raise AuditError('RECTIFICATION_LEDGER_MISMATCH', '违规柜位未一一落单', 500)
    for key, task in plan_slots.items():
        settlement_row = violation_rows[key]
        if (task['original_cents'] != settlement_row['original_cents']
                or task['amount_cents'] != balances[key]['net_cents']
                or task['reduction_cents'] != balances[key]['reduction_cents']):
            raise AuditError('RECTIFICATION_LEDGER_MISMATCH', '柜位罚额与结算不符', 500, key)
    order_tasks = [task_id for order in orders for task_id in order['task_ids']]
    if sorted(order_tasks) != sorted(task['task_id'] for task in tasks):
        raise AuditError('RECTIFICATION_LEDGER_MISMATCH', '工单与柜位任务未一一对应', 500)
    checks = {
        'slots': {'plan': sorted(plan_slots), 'settlement': sorted(violation_rows)},
        'violation_count': {'plan': totals['tasks'], 'settlement': settlement['counts']['VIOLATION']},
        'original_cents': {'plan': totals['original_cents'], 'settlement': settlement['original_cents']},
        'net_cents': {'plan': totals['amount_cents'], 'settlement': appeals['net_cents']},
        'reduction_cents': {'plan': totals['reduction_cents'], 'settlement': appeals['reduction_cents']},
    }
    if any(pair['plan'] != pair['settlement'] for pair in checks.values()):
        raise AuditError('RECTIFICATION_LEDGER_MISMATCH', '整改任务与结算批次不符', 500, checks)
    return {'run_id': snapshot['manifest']['run_id'], 'balanced': True,
            'settlement_original_cents': settlement['original_cents'],
            'settlement_net_cents': appeals['net_cents'], 'checks': checks}


def diff_plans(previous, current):
    """只报告真正变化的柜位，证明改判不会重写其他任务。"""
    if previous is None:
        return {'added': sorted(task['task_id'] for task in current['tasks']),
                'removed': [], 'changed': []}
    before = {task['task_id']: task for task in previous['tasks']}
    after = {task['task_id']: task for task in current['tasks']}
    return {
        'added': sorted(after.keys() - before.keys()),
        'removed': sorted(before.keys() - after.keys()),
        'changed': sorted(task_id for task_id in before.keys() & after.keys()
                          if before[task_id] != after[task_id]),
    }
