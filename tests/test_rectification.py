from concurrent.futures import ThreadPoolExecutor

from conftest import appeal_payload, locate_or_fail


def rect_url(run):
    return f"/api/rectification/{run['manifest']['run_id']}"


def open_and_reduce(client, run, reduction=6000):
    url = f"/api/appeals/{run['manifest']['run_id']}/events"
    client.post(url, json=appeal_payload())
    client.post(url, json=appeal_payload(request_id='A-REDUCE-202611', expected_version=1, action='REDUCED',
                                         reduction_cents=reduction, note='部分补证确实', evidence_ref='回查票-27'))


def dispatch(client, plan_id, request_id='DISPATCH-000001', actor='区域调度'):
    return client.post(f'/api/rectification/plans/{plan_id}/dispatches',
                       json={'request_id': request_id, 'actor': actor})


def test_each_violation_slot_lands_exactly_once_and_matches_settlement(client, run):
    result = client.post(rect_url(run) + '/plans', json={}).json()
    violations = sorted(row['key'] for row in run['penalties']['rows'] if row['status'] == 'VIOLATION')
    tasks = result['tasks']
    assert sorted(task['key'] for task in tasks) == violations
    assert len({task['task_id'] for task in tasks}) == len(tasks) == 3
    assert result['reconciled']['balanced'] is True
    assert result['plan']['totals']['original_cents'] == run['settlement']['original_cents'] == 29500
    assert result['plan']['totals']['amount_cents'] == run['appeals']['net_cents'] == 29500
    assert sum(order['amount_cents'] for order in result['orders']) == 29500


def test_task_carries_photo_rule_and_amount_for_independent_review(client, run):
    tasks = client.post(rect_url(run) + '/plans', json={}).json()['tasks']
    missing = locate_or_fail(tasks, key='002:A03')
    assert (missing['rule'], missing['amount_cents'], missing['original_cents']) == ('MISSING_SKU', 15000, 15000)
    assert missing['photos'] and missing['photos'][0]['photo'] and missing['photos'][0]['sha256']
    wrong = locate_or_fail(tasks, key='005:A01')
    assert (wrong['rule'], wrong['amount_cents']) == ('WRONG_SKU', 9000)
    assert wrong['proof_sources']


def test_orders_group_slots_by_store_without_dropping_tasks(client, run):
    result = client.post(rect_url(run) + '/plans', json={}).json()
    assert {order['store'] for order in result['orders']} == {'001', '002', '005'}
    listed = sorted(task_id for order in result['orders'] for task_id in order['task_ids'])
    assert listed == sorted(task['task_id'] for task in result['tasks'])


def test_reapplying_exact_plan_version_adds_no_tasks(client, run):
    first = client.post(rect_url(run) + '/plans', json={}).json()
    dispatch(client, first['plan']['plan_id'])
    second = client.post(rect_url(run) + '/plans', json={'expected_version': 1}).json()
    assert second['reused'] is True and second['plan']['version'] == first['plan']['version'] == 1
    assert second['tasks'] == first['tasks']
    ledger = client.get(rect_url(run) + '/active').json()['ledger']
    assert ledger['totals']['tasks'] == len({task['task_id'] for task in ledger['tasks']}) == 3
    assert ledger['totals']['amount_cents'] == 29500


def test_dispatch_retry_on_same_version_does_not_duplicate_tasks(client, run):
    plan = client.post(rect_url(run) + '/plans', json={}).json()['plan']
    first = dispatch(client, plan['plan_id'], 'DISPATCH-000001').json()
    replay = dispatch(client, plan['plan_id'], 'DISPATCH-000002').json()
    assert (first['outcome'], first['reused']) == ('SENT', False)
    assert (replay['outcome'], replay['reused']) == ('SENT', True)
    assert replay['tasks'] == first['tasks'] and len(replay['tasks']) == 3
    attempts = client.get(f"/api/rectification/plans/{plan['plan_id']}/dispatches").json()['dispatches']
    assert [row['outcome'] for row in attempts] == ['SENT', 'SENT']


def test_reappearing_same_version_resends_nothing_new(client, run):
    plan = client.post(rect_url(run) + '/plans', json={}).json()['plan']
    dispatch(client, plan['plan_id'])
    again = client.post(rect_url(run) + '/plans', json={'expected_version': 1}).json()
    assert again['reused'] is True
    resent = dispatch(client, again['plan']['plan_id'], 'DISPATCH-000009').json()
    assert resent['reused'] is True and len(resent['tasks']) == 3


def test_appeal_change_only_moves_the_adjusted_slot(client, run):
    first = client.post(rect_url(run) + '/plans', json={}).json()
    dispatch(client, first['plan']['plan_id'])
    open_and_reduce(client, run)
    second = client.post(rect_url(run) + '/plans', json={'expected_version': 1}).json()
    changed_ids = second['diff']['changed']
    assert len(changed_ids) == 1
    changed = locate_or_fail(first['tasks'], task_id=changed_ids[0])
    assert changed['key'] == '002:A03'
    before = {task['task_id']: task['amount_cents'] for task in first['tasks']}
    after = {task['task_id']: task['amount_cents'] for task in second['tasks']}
    assert {task_id for task_id in before if before[task_id] != after[task_id]} == set(changed_ids)
    assert locate_or_fail(second['tasks'], key='002:A03')['amount_cents'] == 9000
    assert second['plan']['totals']['amount_cents'] == 23500


def test_old_plan_version_stays_queryable_after_adjustment(client, run):
    first = client.post(rect_url(run) + '/plans', json={}).json()
    dispatch(client, first['plan']['plan_id'])
    open_and_reduce(client, run)
    client.post(rect_url(run) + '/plans', json={}).json()
    old = client.get(f"/api/rectification/plans/{first['plan']['plan_id']}").json()
    assert old['plan']['version'] == 1 and old['plan']['status'] == 'DISPATCHED'
    assert locate_or_fail(old['tasks'], key='002:A03')['amount_cents'] == 15000
    history = client.get(rect_url(run) + '/plans').json()['plans']
    assert [(row['version'], row['status']) for row in history] == [(1, 'DISPATCHED'), (2, 'DRAFT')]
    second = client.post(rect_url(run) + '/plans', json={}).json()['plan']
    dispatch(client, second['plan_id'], 'DISPATCH-000002')
    assert client.get(f"/api/rectification/plans/{first['plan']['plan_id']}").json()['plan']['status'] == 'SUPERSEDED'


def test_stale_plan_version_is_rejected(client, run):
    client.post(rect_url(run) + '/plans', json={})
    open_and_reduce(client, run)
    client.post(rect_url(run) + '/plans', json={'expected_version': 1})
    stale = client.post(rect_url(run) + '/plans', json={'expected_version': 1})
    assert (stale.status_code, stale.json()['detail']['code']) == (409, 'STALE_PLAN')


def test_failed_dispatch_keeps_previous_version_active(settings, client, run):
    first = client.post(rect_url(run) + '/plans', json={}).json()
    assert dispatch(client, first['plan']['plan_id']).json()['outcome'] == 'SENT'
    open_and_reduce(client, run)
    second = client.post(rect_url(run) + '/plans', json={}).json()
    settings.dispatch_fault = 'GATEWAY_DOWN'
    failed = dispatch(client, second['plan']['plan_id'], 'DISPATCH-000002').json()
    assert failed['outcome'] == 'FAILED' and failed['error']['code'] == 'GATEWAY_DOWN'
    assert failed['active']['version'] == 1
    settings.dispatch_fault = None
    active = client.get(rect_url(run) + '/active').json()
    assert active['plan']['version'] == 1
    assert locate_or_fail(active['tasks'], key='002:A03')['amount_cents'] == 15000
    retry = dispatch(client, second['plan']['plan_id'], 'DISPATCH-000003').json()
    assert retry['outcome'] == 'SENT' and retry['plan']['version'] == 2
    assert client.get(rect_url(run) + '/active').json()['plan']['version'] == 2


def test_retrying_same_request_after_failure_sends_without_duplicate_attempt(settings, client, run):
    plan = client.post(rect_url(run) + '/plans', json={}).json()['plan']
    settings.dispatch_fault = 'GATEWAY_DOWN'
    failure = dispatch(client, plan['plan_id'], 'DISPATCH-000004', '区域调度')
    assert failure.json()['outcome'] == 'FAILED'
    assert client.get(f"/api/rectification/plans/{plan['plan_id']}/dispatches").json()['dispatches'][0]['outcome'] == 'FAILED'
    settings.dispatch_fault = None
    again = dispatch(client, plan['plan_id'], 'DISPATCH-000004', '区域调度').json()
    assert again['outcome'] == 'SENT' and again['reused'] is True and len(again['tasks']) == 3
    attempts = client.get(f"/api/rectification/plans/{plan['plan_id']}/dispatches").json()['dispatches']
    assert len(attempts) == 1 and attempts[0]['outcome'] == 'SENT'
    changed = dispatch(client, plan['plan_id'], 'DISPATCH-000004', '另一调度')
    assert (changed.status_code, changed.json()['detail']['code']) == (409, 'REQUEST_CONFLICT')


def test_closure_closes_one_slot_and_keeps_other_tasks(client, run):
    first = client.post(rect_url(run) + '/plans', json={}).json()
    assert len(first['tasks']) == 3
    closure = client.post(rect_url(run) + '/closures', json={
        'key': '001:A03', 'request_id': 'CLOSE-0000001', 'actor': '店长甲',
        'note': '已补齐排面并重拍', 'evidence_ref': '整改回执-31'}).json()
    assert closure['closure']['key'] == '001:A03' and closure['reused'] is False
    assert closure['diff']['changed'] and not closure['diff']['added']
    assert locate_or_fail(closure['tasks'], key='001:A03')['closed'] is True
    assert locate_or_fail(closure['tasks'], key='002:A03')['closed'] is False
    assert closure['plan']['totals']['closed_tasks'] == 1
    assert client.post(rect_url(run) + '/closures', json={
        'key': '001:A03', 'request_id': 'CLOSE-0000002', 'actor': '店长甲',
        'note': '重复', 'evidence_ref': '整改回执-31'}).status_code == 409


def test_closure_replay_is_idempotent_and_conflicting_reuse_is_rejected(client, run):
    client.post(rect_url(run) + '/plans', json={})
    body = {'key': '005:A01', 'request_id': 'CLOSE-0000010', 'actor': '店长乙',
            'note': '换签完成', 'evidence_ref': '整改回执-32'}
    first = client.post(rect_url(run) + '/closures', json=body).json()
    replay = client.post(rect_url(run) + '/closures', json=body).json()
    assert replay['reused'] is True and replay['closure'] == first['closure']
    changed = client.post(rect_url(run) + '/closures', json={**body, 'note': '改口'})
    assert (changed.status_code, changed.json()['detail']['code']) == (409, 'REQUEST_CONFLICT')


def test_non_violation_slot_cannot_be_closed(client, run):
    response = client.post(rect_url(run) + '/closures', json={
        'key': '001:A04', 'request_id': 'CLOSE-0000020', 'actor': '店长甲', 'note': '待证', 'evidence_ref': 'x'})
    assert (response.status_code, response.json()['detail']['code']) == (409, 'NO_PENALTY')


def test_per_slot_trace_spans_versions_and_closure(client, run):
    first = client.post(rect_url(run) + '/plans', json={}).json()
    dispatch(client, first['plan']['plan_id'])
    open_and_reduce(client, run)
    client.post(rect_url(run) + '/plans', json={}).json()
    client.post(rect_url(run) + '/closures', json={
        'key': '002:A03', 'request_id': 'CLOSE-0000030', 'actor': '店长丙', 'note': '补货完成', 'evidence_ref': '回执-33'})
    trace = client.get(rect_url(run) + '/tasks/002:A03').json()
    assert [row['version'] for row in trace['versions']] == [1, 2, 3]
    assert [row['amount_cents'] for row in trace['versions']] == [15000, 9000, 9000]
    assert [row['closed'] for row in trace['versions']] == [False, False, True]
    assert trace['closure']['actor'] == '店长丙'


def test_concurrent_dispatches_send_one_order_set(client, run):
    plan = client.post(rect_url(run) + '/plans', json={}).json()['plan']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda request_id: dispatch(client, plan['plan_id'], request_id),
                                ('DISPATCH-000011', 'DISPATCH-000012')))
    sent = [response.json() for response in results]
    assert {row['outcome'] for row in sent} == {'SENT'}
    assert sum(not row['reused'] for row in sent) == 1
    assert all(len(row['tasks']) == 3 for row in sent)


def test_draft_and_closure_survive_application_restart(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as initial:
        session = initial.post('/api/settlement/runs', json={}).json()
        plan = initial.post(rect_url(session) + '/plans', json={}).json()['plan']
        initial.post(rect_url(session) + '/closures', json={
            'key': '002:A03', 'request_id': 'CLOSE-0000040', 'actor': '店长丁', 'note': '补货', 'evidence_ref': '回执-40'})
    with TestClient(create_app(settings)) as restored:
        versions = restored.get(rect_url(session) + '/plans').json()
        assert versions['plans'][-1]['totals']['closed_tasks'] == 1
        assert restored.get(f"/api/rectification/plans/{plan['plan_id']}").json()['plan']['version'] == 1
        assert [row['key'] for row in restored.get(rect_url(session) + '/closures').json()['closures']] == ['002:A03']


def test_dispatch_rejects_plan_outdated_by_later_appeal(client, run):
    first = client.post(rect_url(run) + '/plans', json={}).json()
    open_and_reduce(client, run)
    stale = dispatch(client, first['plan']['plan_id'], 'DISPATCH-000021')
    assert (stale.status_code, stale.json()['detail']['code']) == (409, 'PLAN_STALE')
    assert client.get(f"/api/rectification/plans/{first['plan']['plan_id']}").json()['plan']['status'] == 'DRAFT'
