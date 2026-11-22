from collections import defaultdict
from hashlib import sha256


def apportion(groups, target):
    """按店组权重用最大余数分配柜位目标，余数并列时按组编号升序。"""
    total = sum(group['weight'] for group in groups)
    quotas, remainders = {}, []
    for group in groups:
        base, remainder = divmod(target * group['weight'], total)
        quotas[group['code']] = base
        remainders.append((-remainder, group['code']))
    left = target - sum(quotas.values())
    for _, code in sorted(remainders)[:left]:
        quotas[code] += 1
    return quotas


def rank_score(seed, store, slot):
    return sha256(f'{seed}\0{store}\0{slot}'.encode()).hexdigest()


def choose_slots(intake, target):
    """在名册供给内按店组抽取柜位；供给不足时保留缺额，不跨组补位。"""
    campaign = intake['campaign']
    quotas = apportion(campaign['groups'], target)
    groups = {row['store']: row['group'] for row in intake['stores']}
    pools = defaultdict(list)
    for slot in intake['slots']:
        pools[groups[slot['store']]].append({
            'key': f"{slot['store']}:{slot['slot']}", 'store': slot['store'], 'slot': slot['slot'],
            'category': slot['category'], 'group': groups[slot['store']], 'source': slot['source'],
            'score': rank_score(campaign['seed'], slot['store'], slot['slot']),
        })
    selected, strata = [], []
    for group in campaign['groups']:
        code = group['code']
        ranked = sorted(pools[code], key=lambda item: (item['score'], item['key']))
        take = min(quotas[code], len(ranked))
        selected.extend(ranked[:take])
        strata.append({'code': code, 'title': group['title'], 'weight': group['weight'],
                       'target': quotas[code], 'pool': len(ranked), 'selected': take,
                       'unfilled': quotas[code] - take, 'ranked_candidates': ranked})
    return selected, strata
