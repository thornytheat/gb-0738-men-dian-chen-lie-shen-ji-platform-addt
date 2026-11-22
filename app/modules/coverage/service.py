from app.modules.coverage.sampling import choose_slots

# 图证因复用或清点矛盾失效时整柜隔离；材料不足则留在样本里待证。
ISOLATING_EVIDENCE = {'PHOTO_REUSED', 'PHOTO_ID_CONFLICT', 'OBSERVATION_CONFLICT', 'BAD_OBSERVATION'}
ISOLATING_PLAN = {'PLAN_MISSING', 'PLAN_OVERLAP'}


def _assess(decision, binding):
    visit = decision.get('visit')
    if visit is None or decision['reason'] == 'VISIT_MISSING':
        return 'PENDING', 'VISIT_MISSING', None
    if decision['reason'] in ISOLATING_PLAN:
        return 'ISOLATED', decision['reason'], None
    if binding is None:
        return 'PENDING', 'MISSING_PHOTO', None
    code = binding['reason']
    if code is None:
        return 'VERIFIED', None, binding
    if code in ISOLATING_EVIDENCE:
        return 'ISOLATED', code, binding
    return 'PENDING', code, binding


def coverage(intake, plans, photos, target):
    selected, strata = choose_slots(intake, target)
    decisions = {(row['store'], row['slot']): row for row in plans['decisions']}
    bindings = {(row['visit'], row['slot']): row for row in photos['bindings']}
    slots = []
    for pick in selected:
        decision = decisions[pick['store'], pick['slot']]
        binding = bindings.get((decision['visit']['visit'], pick['slot'])) if decision.get('visit') else None
        status, reason, evidence = _assess(decision, binding)
        slots.append({**pick, 'visit': decision.get('visit'), 'plan': decision.get('plan'),
                      'plan_candidates': decision.get('candidates', []), 'evidence_status': status,
                      'reason': reason, 'evidence': evidence})
    rows = []
    for stratum in strata:
        members = [row for row in slots if row['group'] == stratum['code']]
        counts = {name: sum(row['evidence_status'] == name for row in members)
                  for name in ('VERIFIED', 'PENDING', 'ISOLATED')}
        rows.append({**stratum, 'verified': counts['VERIFIED'], 'pending': counts['PENDING'],
                     'isolated': counts['ISOLATED'], 'gap': stratum['target'] - counts['VERIFIED']})
    verified = sum(row['evidence_status'] == 'VERIFIED' for row in slots)
    return {'target': target, 'selected': len(slots),
            'unfilled': sum(row['unfilled'] for row in strata), 'verified': verified,
            'slots': slots, 'strata': rows}
