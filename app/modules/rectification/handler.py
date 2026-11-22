from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.modules.rectification.service import dispatch, ledger, plan_summary, trace
from app.store import Store

router = APIRouter(prefix='/api/rectification')
RunId = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]
PlanId = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]
ShelfKey = Annotated[str, Path(pattern=r'^[A-Z0-9][A-Z0-9_-]{0,31}:[A-Z0-9][A-Z0-9_-]{0,31}$')]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
RequestId = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')]


class DraftInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: Annotated[int | None, Field(ge=0, le=1000)] = None


class DispatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: RequestId
    actor: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]


class ClosureInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: ShelfKey
    request_id: RequestId
    actor: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
    note: Text
    evidence_ref: Text


def _store(request: Request) -> Store:
    return request.app.state.store


@router.get('/{run_id}/plans')
def plans(request: Request, run_id: RunId):
    versions = _store(request).plans(run_id)
    latest = versions[-1] if versions else None
    active = _store(request).active_plan(run_id)
    return {'run_id': run_id, 'plans': [plan_summary(plan) for plan in versions],
            'plan': plan_summary(latest) if latest else None,
            'tasks': latest['tasks'] if latest else [],
            'orders': latest['orders'] if latest else [],
            'reconciled': latest['reconciliation'] if latest else None,
            'active': plan_summary(active) if active else None,
            'ledger': ledger(active)}


@router.post('/{run_id}/plans')
def draft(request: Request, run_id: RunId, form: DraftInput):
    result = _store(request).draft_plan(run_id, form.expected_version)
    return {'plan': plan_summary(result['plan']), 'tasks': result['plan']['tasks'],
            'orders': result['plan']['orders'], 'reconciled': result['plan']['reconciliation'],
            'reused': result['reused'], 'diff': result['plan']['diff'],
            'active': plan_summary(result['active']) if result['active'] else None,
            'ledger': ledger(result['active'])}


@router.get('/{run_id}/active')
def active(request: Request, run_id: RunId):
    plan = _store(request).active_plan(run_id)
    return {'plan': plan_summary(plan) if plan else None, 'tasks': plan['tasks'] if plan else [],
            'orders': plan['orders'] if plan else [], 'reconciled': plan['reconciliation'] if plan else None,
            'ledger': ledger(plan)}


@router.get('/plans/{plan_id}')
def version(request: Request, plan_id: PlanId):
    plan = _store(request).plan(plan_id)
    return {'plan': plan_summary(plan), 'tasks': plan['tasks'], 'orders': plan['orders'],
            'reconciled': plan['reconciliation']}


@router.post('/plans/{plan_id}/dispatches')
def send(request: Request, plan_id: PlanId, form: DispatchInput):
    result = dispatch(_store(request), request.app.state.settings, plan_id, form.model_dump())
    if result['outcome'] == 'FAILED':
        return JSONResponse({'detail': {'code': result['error']['code'], 'message': result['error']['message']},
                             **result}, status_code=502)
    return result


@router.get('/plans/{plan_id}/dispatches')
def attempts(request: Request, plan_id: PlanId):
    return {'plan': plan_summary(_store(request).plan(plan_id)),
            'dispatches': _store(request).dispatches(plan_id)}


@router.post('/{run_id}/closures')
def close(request: Request, run_id: RunId, form: ClosureInput):
    record, reused = _store(request).close_task(run_id, form.model_dump())
    result = _store(request).draft_plan(run_id)
    return {'closure': record, 'reused': reused, 'plan': plan_summary(result['plan']),
            'tasks': result['plan']['tasks'], 'orders': result['plan']['orders'],
            'diff': result['plan']['diff'], 'ledger': ledger(result['active'])}


@router.get('/{run_id}/closures')
def closures(request: Request, run_id: RunId):
    return {'run_id': run_id, 'closures': _store(request).closures(run_id)}


@router.get('/{run_id}/tasks/{key}')
def task_trace(request: Request, run_id: RunId, key: ShelfKey):
    snapshot = _store(request).get(run_id)
    return trace(snapshot, _store(request).plans(run_id), _store(request).closures(run_id), key)
