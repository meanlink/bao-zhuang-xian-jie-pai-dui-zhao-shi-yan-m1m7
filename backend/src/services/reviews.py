"""成组评审：锁定成员后独立执行、Holm 校正、整组批准与发布。"""
import io, json, csv, hashlib, time
from fastapi import HTTPException
from sqlalchemy import select, update, func
from src.models import ReviewGroup, ReviewItem, Dataset
from src.services import business
from src.services.audit import record
from src.utils.serialization import checksum
from src.config.settings import ARTIFACT_DIR
import os

CONFIDENCES = (.9, .95, .99)
ALPHAS = (.01, .05, .10)
SHIFTS = ("早班", "晚班")
ACTIVE = ('locked', 'running')
RUNNABLE = ('locked', 'running', 'failed')


def enqueue(group_id):
    from redis import Redis
    from src.config.settings import REDIS_URL
    try:
        client = Redis.from_url(REDIS_URL, socket_timeout=2)
        client.rpush('review:queue', group_id)
        client.close()
    except Exception:
        pass  # SQL 中的 locked 状态是持久兜底，worker 会按原成员恢复待算项


def _group_rule():
    return business.metadata()['group_review']


def _validate_confidence(value):
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        raise HTTPException(422, '区间置信度只接受 0.90/0.95/0.99')
    if confidence not in CONFIDENCES:
        raise HTTPException(422, '区间置信度只接受 0.90/0.95/0.99')
    return confidence


def _validate_alpha(value):
    try:
        alpha = float(value)
    except (TypeError, ValueError):
        raise HTTPException(422, '家族误报上限只接受 0.01/0.05/0.10')
    if not any(abs(alpha - a) < 1e-12 for a in ALPHAS):
        raise HTTPException(422, '家族误报上限只接受 0.01/0.05/0.10')
    return alpha


def _item_specs(db, items):
    if items is None:
        raise HTTPException(422, '成组评审需要至少两个评审项')
    if not (_group_rule()['min_items'] <= len(items) <= _group_rule()['max_items']):
        raise HTTPException(422, f"评审项须在 {_group_rule()['min_items']} 至 {_group_rule()['max_items']} 项之间")
    seen = set()
    out = []
    for index, item in enumerate(items):
        shift = str(item.shift)
        if shift not in SHIFTS:
            raise HTTPException(422, f'第 {index+1} 项班次无效，只支持早班或晚班')
        dataset = db.get(Dataset, int(item.dataset_id))
        if not dataset:
            raise HTTPException(404, f'第 {index+1} 项的数据批次不存在')
        key = (dataset.id, shift)
        if key in seen:
            raise HTTPException(422, f'第 {index+1} 项与前面的批次、班次重复')
        seen.add(key)
        try:
            business.preflight_member(dataset.records, shift)
        except ValueError as exc:
            raise HTTPException(422, f'第 {index+1} 项（{dataset.name} · {shift}）无法估计：{exc}')
        out.append({'dataset': dataset, 'shift': shift})
    return out


def _fingerprint(specs, confidence, alpha):
    basis = [{'dataset_id': spec['dataset'].id, 'dataset_checksum': spec['dataset'].checksum, 'shift': spec['shift']} for spec in specs]
    return checksum({'items': basis, 'confidence': confidence, 'alpha': alpha})


def _apply_specs(db, group, specs):
    for old in db.scalars(select(ReviewItem).where(ReviewItem.group_id == group.id)):
        db.delete(old)
    db.flush()
    for position, spec in enumerate(specs):
        db.add(ReviewItem(group_id=group.id, position=position, dataset_id=spec['dataset'].id,
                          dataset_name=spec['dataset'].name, dataset_checksum=spec['dataset'].checksum,
                          shift=spec['shift'], status='pending'))
    db.flush()


def _clean_name(name):
    value = (name or '').strip()
    if len(value) < 2:
        raise HTTPException(422, '评审组名称去除空格后至少需要两个字符')
    return value


def create(db, data, user):
    confidence = _validate_confidence(data.confidence)
    alpha = _validate_alpha(data.alpha)
    specs = _item_specs(db, data.items)
    group = ReviewGroup(name=_clean_name(data.name), confidence=confidence, alpha=alpha, status='draft',
                        created_by=user.id, created_at=time.time())
    db.add(group)
    db.flush()
    _apply_specs(db, group, specs)
    db.flush()
    return group


def update_draft(db, group_id, data, user):
    group = db.get(ReviewGroup, group_id)
    if not group:
        raise HTTPException(404, '评审组不存在')
    if group.status != 'draft':
        raise HTTPException(409, '评审组已锁定，拒绝增删或更换样本；请新建评审组')
    confidence = _validate_confidence(data.confidence)
    alpha = _validate_alpha(data.alpha)
    specs = _item_specs(db, data.items)
    group.name = _clean_name(data.name)
    group.confidence = confidence
    group.alpha = alpha
    _apply_specs(db, group, specs)
    db.flush()
    return group


def lock(db, group_id, user, spec=None):
    group = db.scalar(select(ReviewGroup).where(ReviewGroup.id == group_id).with_for_update())
    if not group:
        raise HTTPException(404, '评审组不存在')
    if group.status != 'draft':
        raise HTTPException(409, '评审组已锁定；锁定后拒绝增删或更换样本，重复锁定不会新建成员')
    items = list(db.scalars(select(ReviewItem).where(ReviewItem.group_id == group.id).order_by(ReviewItem.position)))
    if spec is not None:
        if spec.items is not None:
            current = [(item.dataset_id, item.shift) for item in items]
            incoming = [(item.dataset_id, item.shift) for item in spec.items]
            if current != incoming:
                raise HTTPException(409, '提交的成员与草稿不一致；锁定不得增删或更换样本')
        if spec.confidence is not None and abs(_validate_confidence(spec.confidence) - group.confidence) > 1e-12:
            raise HTTPException(409, '提交的置信度与草稿不一致；锁定后不可改动')
        if spec.alpha is not None and abs(_validate_alpha(spec.alpha) - group.alpha) > 1e-12:
            raise HTTPException(409, '提交的误报上限与草稿不一致；锁定后不可改动')
    if not (_group_rule()['min_items'] <= len(items) <= _group_rule()['max_items']):
        raise HTTPException(422, '评审项数量不在支持范围')
    specs = []
    for index, item in enumerate(items):
        dataset = db.get(Dataset, item.dataset_id)
        if not dataset:
            raise HTTPException(409, f'第 {index+1} 项的数据批次已不存在，锁定中止')
        if dataset.checksum != item.dataset_checksum:
            raise HTTPException(409, f'第 {index+1} 项数据批次指纹已变化，锁定中止')
        try:
            business.preflight_member(dataset.records, item.shift)
        except ValueError as exc:
            raise HTTPException(422, f'第 {index+1} 项无法估计：{exc}')
        specs.append({'dataset': dataset, 'shift': item.shift})
    if len({(spec['dataset'].id, spec['shift']) for spec in specs}) != len(specs):
        raise HTTPException(422, '评审项存在重复的批次与班次')
    group.spec_fingerprint = _fingerprint(specs, group.confidence, group.alpha)
    group.status = 'locked'
    group.locked_at = time.time()
    group.error = ''
    group.result = {}
    for position, item in enumerate(items):
        item.position = position
        item.status = 'pending'
        item.started_at = None
        item.finished_at = None
        item.error = ''
        item.result = {}
        item.p_value = None
        item.p_adjusted = None
        item.raw_significant = None
        item.adjusted_significant = None
    db.flush()
    record(db, user.email, 'review.lock', {'id': group.id, 'items': len(items), 'alpha': group.alpha, 'fingerprint': group.spec_fingerprint})
    return group


def resume(db, group_id, user):
    group = db.scalar(select(ReviewGroup).where(ReviewGroup.id == group_id).with_for_update())
    if not group:
        raise HTTPException(404, '评审组不存在')
    if group.status not in ('locked', 'running', 'failed'):
        raise HTTPException(409, '当前状态不允许恢复；已发布或待发布的评审组保持原结论')
    items = list(db.scalars(select(ReviewItem).where(ReviewItem.group_id == group.id).order_by(ReviewItem.position)))
    now = time.time()
    retry = [item for item in items
             if item.status == 'failed' or (item.status == 'running' and (not item.started_at or now - item.started_at > 120))]
    if not retry:
        if any(item.status == 'pending' for item in items):
            return group
        raise HTTPException(409, '没有待恢复的失败项')
    for item in retry:
        item.status = 'pending'
        item.started_at = None
        item.finished_at = None
        item.error = ''
        item.result = {}
        item.p_value = None
        item.p_adjusted = None
        item.raw_significant = None
        item.adjusted_significant = None
    group.status = 'locked'
    group.error = ''
    group.result = {}
    db.flush()
    record(db, user.email, 'review.resume', {'id': group.id, 'retried': [item.id for item in retry]})
    return group


def _aggregate(db, group):
    """按成员当前状态推进组状态；只有全部成功才生成校正结论。"""
    items = list(db.scalars(select(ReviewItem).where(ReviewItem.group_id == group.id).order_by(ReviewItem.position)))
    if not items:
        group.status = 'draft'
        return group
    pending = [item for item in items if item.status in ('pending', 'running')]
    failed = [item for item in items if item.status == 'failed']
    if pending:
        group.status = 'running'
        group.result = {}
        return group
    if failed:
        group.status = 'failed'
        group.error = f"{len(failed)} 项失败，未剔除；恢复失败项后才能共同发布"
        group.result = {}
        return group
    if all(item.status == 'completed' for item in items):
        group.result = _corrected_result(group, items)
        group.status = 'computed'
        group.error = ''
    return group


def _corrected_result(group, items):
    p_values = [float(item.p_value) for item in items]
    correction = business.holm_adjust(p_values, group.alpha)
    members = []
    for index, item in enumerate(items):
        stats = (item.result or {}).get('stats', {})
        members.append({
            'position': index + 1, 'item_id': item.id, 'shift': item.shift,
            'dataset_id': item.dataset_id, 'dataset_name': item.dataset_name, 'dataset_checksum': item.dataset_checksum,
            'p_value': float(item.p_value), 'p_adjusted': correction['adjusted'][index],
            'raw_significant': p_values[index] <= group.alpha + 1e-12,
            'adjusted_significant': correction['significant'][index],
            'diff': stats.get('diff'), 'low': stats.get('low'), 'high': stats.get('high'),
            'n_a': stats.get('n_a'), 'n_b': stats.get('n_b'),
        })
    # 标记显著与否只写入组结论，单项记录不得单独批准或分发。
    for index, item in enumerate(items):
        item.p_adjusted = correction['adjusted'][index]
        item.raw_significant = members[index]['raw_significant']
        item.adjusted_significant = correction['significant'][index]
    raw_count = sum(1 for m in members if m['raw_significant'])
    adjusted_count = sum(1 for m in members if m['adjusted_significant'])
    survivors = [f"{m['shift']}（第{m['position']}项）" for m in members if m['adjusted_significant']]
    m = len(members)
    summary = (f"本组 {m} 项按 Holm 校正，家族误报上限 α={group.alpha:g}；"
               f"原始显著 {raw_count} 项，校正后显著 {adjusted_count} 项。")
    if survivors:
        summary += '校正后仍显著：' + '、'.join(survivors) + '。'
    else:
        summary += '校正后无单项显著，不能作为单独改善结论。'
    labels = [f"{m['dataset_name'].replace('A 原动作 / B 改动作 · ', '')}·{m['shift']}" for m in members]
    table = {'columns': [{'key': 'position', 'label': '项'}, {'key': 'dataset_name', 'label': '数据批次'}, {'key': 'shift', 'label': '班次'},
                         {'key': 'diff', 'label': '改善量 A−B/秒'}, {'key': 'p_value', 'label': '原始 p 值'},
                         {'key': 'p_adjusted', 'label': 'Holm 校正 p'}, {'key': 'raw_significant', 'label': '原始显著'},
                         {'key': 'adjusted_significant', 'label': '校正后显著'}],
             'rows': [{'position': m['position'], 'dataset_name': m['dataset_name'], 'shift': m['shift'],
                       'diff': round(m['diff'], 4) if m['diff'] is not None else None,
                       'p_value': float(f"{m['p_value']:.6g}"), 'p_adjusted': float(f"{m['p_adjusted']:.6g}"),
                       'raw_significant': '是' if m['raw_significant'] else '否',
                       'adjusted_significant': '是' if m['adjusted_significant'] else '否'} for m in members]}
    return {
        'family_size': m, 'alpha': group.alpha, 'confidence': group.confidence,
        'summary': summary, 'members': members, 'table': table,
        'chart': {'type': 'bar', 'labels': labels, 'series': [
            {'name': '原始 p 值', 'values': [round(m['p_value'], 6) for m in members]},
            {'name': 'Holm 校正 p', 'values': [round(m['p_adjusted'], 6) for m in members]}]},
        'findings': [
            summary,
            f"校正规则：{business.metadata()['group_method']}",
            '组内原始显著项不构成可批准或可分发的结论；只有整组校正结论可共同发布。',
            '锁定后成员、样本与误报上限不再变更；失败项不得剔除。',
        ],
        'warnings': ['Holm 校正控制家族误报率，但不消除未纳入本组的其他比较。',
                     '显著节拍差不能证明成品质检合格。'],
        'method': business.metadata()['group_method'],
        'stats': {'raw_significant_count': raw_count, 'adjusted_significant_count': adjusted_count,
                  'p_values': p_values, 'p_adjusted': correction['adjusted'],
                  'spec_fingerprint': group.spec_fingerprint},
    }


def recover_incomplete(db):
    """worker 启动或轮询时按原成员恢复中断的待算项，不新建成员。"""
    recovered = []
    for group in db.scalars(select(ReviewGroup).where(ReviewGroup.status.in_(ACTIVE))):
        items = list(db.scalars(select(ReviewItem).where(ReviewItem.group_id == group.id)))
        changed = False
        for item in items:
            if item.status == 'running':
                item.status = 'pending'
                item.started_at = None
                changed = True
        if changed:
            group.status = 'locked'
            recovered.append(group.id)
            record(db, 'worker', 'review.recovered', {'id': group.id, 'items': [i.id for i in items if i.status == 'pending']})
        elif items and all(item.status == 'completed' for item in items):
            # 中断在最后一项提交与聚合之间时，按原成员补算整组校正，不新建成员。
            _aggregate(db, group)
            if group.status == 'computed':
                recovered.append(group.id)
                record(db, 'worker', 'review.reconciled', {'id': group.id})
    return recovered


def process_group(group_id):
    """独立执行本组待算项；同项重试复用原记录。"""
    from src.config.database import Session
    with Session() as db:
        group = db.get(ReviewGroup, group_id)
        if not group or group.status not in RUNNABLE:
            return
        items = list(db.scalars(select(ReviewItem).where(ReviewItem.group_id == group.id, ReviewItem.status == 'pending').order_by(ReviewItem.position)))
        for item in items:
            claimed = db.execute(update(ReviewItem).where(ReviewItem.id == item.id, ReviewItem.status == 'pending')
                                 .values(status='running', started_at=time.time(), attempts=func.coalesce(ReviewItem.attempts, 0) + 1))
            if claimed.rowcount != 1:
                continue
            db.flush()
            dataset = db.get(Dataset, item.dataset_id)
            try:
                if not dataset:
                    raise ValueError('锁定时的数据批次已不存在')
                if dataset.checksum != item.dataset_checksum:
                    raise ValueError('数据批次指纹与锁定时不一致，拒绝改用新样本')
                result = business.analyze(dataset.records, {'confidence': group.confidence, 'shift': item.shift})
                json.dumps(result, allow_nan=False)
                stats = result['stats']
                item.result = result
                item.p_value = float(stats['p_value'])
                item.p_adjusted = None
                item.raw_significant = None
                item.adjusted_significant = None
                item.status = 'completed'
                item.finished_at = time.time()
                item.error = ''
            except Exception as exc:
                item.status = 'failed'
                item.error = str(exc)[:990]
                item.finished_at = time.time()
                record(db, 'worker', 'review.item.failed', {'group': group.id, 'item': item.id, 'error': item.error})
            db.flush()
            _aggregate(db, group)
            db.flush()
            db.commit()
        if group.status in RUNNABLE:
            _aggregate(db, group)
            if group.status == 'computed':
                record(db, 'worker', 'review.completed', {'id': group.id, 'family_size': group.result.get('family_size')})
            db.commit()


def _require_all_completed(db, group):
    items = list(db.scalars(select(ReviewItem).where(ReviewItem.group_id == group.id)))
    if not items or any(item.status != 'completed' for item in items):
        raise HTTPException(409, '整组尚未全部成功，不放行半组结论')


def approve(db, group_id, user):
    group = db.get(ReviewGroup, group_id)
    if not group:
        raise HTTPException(404, '评审组不存在')
    if group.status != 'computed':
        raise HTTPException(409, '仅整组计算完成且待发布的校正结论可以批准')
    _require_all_completed(db, group)
    if not group.result or group.result.get('family_size') != len([i for i in db.scalars(select(ReviewItem).where(ReviewItem.group_id == group.id))]):
        raise HTTPException(409, '整组校正结论不完整，不放行半组')
    changed = db.execute(update(ReviewGroup).where(ReviewGroup.id == group.id, ReviewGroup.status == 'computed')
                         .values(status='approved', approved_by=user.id, approved_at=time.time(), error=''))
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, '评审组状态已变化，请刷新后再批准')
    db.expire(group)
    record(db, user.email, 'review.approve', {'id': group.id, 'fingerprint': group.spec_fingerprint, 'family_size': group.result.get('family_size')})
    return group


def publish(db, group_id, user):
    group = db.get(ReviewGroup, group_id)
    if not group:
        raise HTTPException(404, '评审组不存在')
    if group.status != 'approved' or not group.approved_at:
        raise HTTPException(409, '仅获整组批准的校正结论可以发布')
    _require_all_completed(db, group)
    if not group.result or group.result.get('family_size') != len(list(db.scalars(select(ReviewItem).where(ReviewItem.group_id == group.id)))):
        raise HTTPException(409, '整组校正结论不完整，不放行半组发布')
    filename, checksum_value = _write_brief(group)
    changed = db.execute(update(ReviewGroup).where(ReviewGroup.id == group.id, ReviewGroup.status == 'approved')
                         .values(status='published', published_by=user.id, published_at=time.time(),
                                 brief_filename=filename, brief_checksum=checksum_value, error=''))
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, '评审组已被并发发布或状态已变化，请刷新')
    db.expire(group)
    record(db, user.email, 'review.publish', {'id': group.id, 'fingerprint': group.spec_fingerprint,
                                              'brief': filename, 'checksum': checksum_value})
    return group


def brief_bytes(group):
    out = io.StringIO()
    out.write('﻿')
    writer = csv.writer(out)
    def safe(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        text = str(v)
        return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text
    writers = [['成组评审简报', group.name], ['整组依据', '组内所有成员共同校正，原始显著性不得单独批准或分发'],
               ['数据批次', '、'.join(sorted({m['dataset_name'] for m in group.result['members']}))],
               ['家族项数', group.result['family_size']], ['家族误报上限 alpha', group.alpha],
               ['区间置信度', group.confidence], ['锁定指纹', group.spec_fingerprint],
               ['整组结论', group.result['summary']], ['整组校正规则', group.result['method']]]
    for row in writers:
        writer.writerow([safe(v) for v in row])
    writer.writerow([])
    table = group.result['table']
    writer.writerow([safe(c['label']) for c in table['columns']])
    for row in table['rows']:
        writer.writerow([safe(row.get(c['key'], '')) for c in table['columns']])
    writer.writerow([])
    for finding in group.result.get('findings', []):
        writer.writerow([safe(finding)])
    for warning in group.result.get('warnings', []):
        writer.writerow([safe(warning)])
    return out.getvalue().encode('utf-8')


def _write_brief(group):
    data = brief_bytes(group)
    digest = hashlib.sha256(data).hexdigest()
    filename = f'review-{group.id}-brief-{digest[:12]}.csv'
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    path = os.path.join(ARTIFACT_DIR, filename)
    with open(path, 'wb') as handle:
        handle.write(data)
    payload = {'group': group.name, 'spec_fingerprint': group.spec_fingerprint,
               'alpha': group.alpha, 'confidence': group.confidence,
               'whole_group_basis': '成员共同校正，原始显著性不得单独批准或分发',
               'result': group.result, 'checksum': digest}
    with open(path + '.json', 'w') as handle:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
    return filename, digest


def group_dict(group, items=None, include_item_result=False):
    data = {k: getattr(group, k) for k in ['id', 'name', 'confidence', 'alpha', 'status', 'spec_fingerprint',
                                            'error', 'result', 'brief_filename', 'brief_checksum', 'created_at',
                                            'locked_at', 'approved_at', 'published_at', 'created_by', 'approved_by', 'published_by']}
    if items is not None:
        released = group.status in ('computed', 'approved', 'published')
        data['items'] = [item_dict(item, released, include_item_result) for item in items]
    return data


def item_dict(item, released=False, include_result=False):
    data = {k: getattr(item, k) for k in ['id', 'group_id', 'position', 'dataset_id', 'dataset_name', 'dataset_checksum',
                                           'shift', 'status', 'attempts', 'error', 'started_at', 'finished_at']}
    if released:
        # 原始显著性只有在整组校正结论生成后才随组展示，不能单独读取或分发。
        data.update({k: getattr(item, k) for k in ['p_value', 'p_adjusted', 'raw_significant', 'adjusted_significant']})
        if include_result:
            data['result'] = item.result
    else:
        data.update({'p_value': None, 'p_adjusted': None, 'raw_significant': None, 'adjusted_significant': None})
        data['result'] = {}
    return data


def item_rows(group_id, db):
    return list(db.scalars(select(ReviewItem).where(ReviewItem.group_id == group_id).order_by(ReviewItem.position)))
