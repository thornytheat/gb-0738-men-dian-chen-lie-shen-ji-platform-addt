from app.errors import AuditError


def plan_summary(plan):
    return {'plan_id': plan['plan_id'], 'run_id': plan['run_id'], 'version': plan['version'],
            'status': plan['status'], 'content_hash': plan['content_hash'],
            'created_at': plan['created_at'], 'updated_at': plan['updated_at'],
            'totals': plan['totals'], 'diff': plan.get('diff')}


def _deliver(settings, plan):
    """把工单写入下发通道。门店侧只收一张按门店合并的工单，柜位仍是独立任务。"""
    if settings.dispatch_fault:
        raise AuditError(settings.dispatch_fault, '下发通道返回失败', 502)
    for order in plan['orders']:
        if set(order['task_ids']) != {task['task_id'] for task in plan['tasks'] if task['store'] == order['store']}:
            raise AuditError('RECTIFICATION_LEDGER_MISMATCH', '工单柜位与任务不符', 500, order['order_id'])
        if order['amount_cents'] != sum(task['amount_cents'] for task in plan['tasks']
                                        if task['store'] == order['store']):
            raise AuditError('RECTIFICATION_LEDGER_MISMATCH', '工单金额与任务不符', 500, order['order_id'])
    return f"已向 {len(plan['orders'])} 家门店下发 {plan['totals']['tasks']} 个柜位任务"


def dispatch(store, settings, plan_id, payload):
    result = store.dispatch_plan(plan_id, payload, lambda plan: _deliver(settings, plan))
    plan = result['plan']
    return {'outcome': result['outcome'], 'reused': result['reused'],
            'plan': plan_summary(plan),
            'active': plan_summary(result['active']) if result['active'] else None,
            'orders': plan['orders'], 'tasks': plan['tasks'],
            **({'error': result['error']} if result.get('error') else {})}


def ledger(plan):
    """下发台账：同一柜位在一个批次里只有一个任务身份，重试不会多出任务。

    金额取自该版本自身的快照，便于核对旧计划当时的金额；后续改判只影响新版本。
    """
    if plan is None:
        return {'plan_id': None, 'version': 0, 'status': None, 'tasks': [],
                'totals': {'tasks': 0, 'amount_cents': 0}}
    seen, tasks = set(), []
    for task in plan['tasks']:
        if task['task_id'] in seen:
            continue
        seen.add(task['task_id'])
        tasks.append(task)
    return {'plan_id': plan['plan_id'], 'version': plan['version'], 'status': plan['status'],
            'tasks': tasks,
            'totals': {'tasks': len(tasks), 'amount_cents': sum(task['amount_cents'] for task in tasks)}}


def trace(snapshot, plans, closures, key):
    """单个柜位的独立追溯：历次版本中的任务、照片、罚则与销项。"""
    versions = []
    for plan in plans:
        task = next((item for item in plan['tasks'] if item['key'] == key), None)
        if task is not None:
            versions.append({'plan_id': plan['plan_id'], 'version': plan['version'], 'status': plan['status'],
                             'task_id': task['task_id'], 'rule': task['rule'],
                             'amount_cents': task['amount_cents'], 'original_cents': task['original_cents'],
                             'reduction_cents': task['reduction_cents'], 'photos': task['photos'],
                             'proof_sources': task['proof_sources'], 'closed': task['closed']})
    return {'run_id': snapshot['manifest']['run_id'], 'key': key, 'versions': versions,
            'closure': next((row for row in closures if row['key'] == key), None)}
