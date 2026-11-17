from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
import os
from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import ReviewGroup, ReviewItem
from src.schemas.review import ReviewGroupInput, ReviewLockInput
from src.services import reviews
from src.services.audit import record
router = APIRouter(prefix='/reviews')


@router.get('')
def listing(status: list[str] | None = Query(default=None), db=Depends(get_db), u=Depends(current_user)):
    query = select(ReviewGroup).order_by(ReviewGroup.id.desc()).limit(100)
    if status:
        allowed = {'draft', 'locked', 'running', 'computed', 'failed', 'approved', 'published'}
        unknown = [s for s in status if s not in allowed]
        if unknown:
            raise HTTPException(422, '未知评审组状态')
        query = query.where(ReviewGroup.status.in_(status))
    groups = list(db.scalars(query))
    if not groups:
        return []
    items = db.scalars(select(ReviewItem).where(ReviewItem.group_id.in_([g.id for g in groups])).order_by(ReviewItem.position))
    by_group = {}
    for item in items:
        by_group.setdefault(item.group_id, []).append(item)
    return [reviews.group_dict(g, by_group.get(g.id, [])) for g in groups]


@router.post('', status_code=201)
def create(data: ReviewGroupInput, db=Depends(get_db), u=Depends(can_write)):
    group = reviews.create(db, data, u)
    record(db, u.email, 'review.create', {'id': group.id, 'items': len(data.items), 'alpha': group.alpha})
    db.commit()
    return reviews.group_dict(group, reviews.item_rows(group.id, db))


@router.get('/{id}')
def detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    group = db.get(ReviewGroup, id)
    if not group:
        raise HTTPException(404, '评审组不存在')
    return reviews.group_dict(group, reviews.item_rows(group.id, db), include_item_result=True)


@router.put('/{id}')
def update(id: int, data: ReviewGroupInput, db=Depends(get_db), u=Depends(can_write)):
    group = reviews.update_draft(db, id, data, u)
    record(db, u.email, 'review.draft.update', {'id': id, 'items': len(data.items)})
    db.commit()
    return reviews.group_dict(group, reviews.item_rows(group.id, db))


@router.post('/{id}/lock')
def lock(id: int, data: ReviewLockInput | None = None, db=Depends(get_db), u=Depends(can_write)):
    group = reviews.lock(db, id, u, data)
    record(db, u.email, 'review.locked', {'id': id, 'fingerprint': group.spec_fingerprint})
    db.commit()
    reviews.enqueue(id)
    return reviews.group_dict(group, reviews.item_rows(group.id, db))


@router.post('/{id}/resume')
def resume(id: int, db=Depends(get_db), u=Depends(can_write)):
    group = reviews.resume(db, id, u)
    db.commit()
    reviews.enqueue(id)
    return reviews.group_dict(group, reviews.item_rows(group.id, db))


@router.post('/{id}/approve')
def approve(id: int, db=Depends(get_db), u=Depends(admin_only)):
    group = reviews.approve(db, id, u)
    db.commit()
    return reviews.group_dict(group, reviews.item_rows(group.id, db))


@router.post('/{id}/publish')
def publish(id: int, db=Depends(get_db), u=Depends(admin_only)):
    group = reviews.publish(db, id, u)
    db.commit()
    return reviews.group_dict(group, reviews.item_rows(group.id, db))


@router.get('/{id}/brief')
def brief(id: int, db=Depends(get_db), u=Depends(current_user)):
    group = db.get(ReviewGroup, id)
    if not group:
        raise HTTPException(404, '评审组不存在')
    if group.status != 'published' or not group.brief_filename:
        raise HTTPException(409, '仅已整组发布的校正结论提供简报；原始显著性不单独分发')
    path = os.path.join(ARTIFACT_DIR, group.brief_filename)
    if not os.path.exists(path):
        raise HTTPException(404, '简报归档不存在，请联系业务负责人')
    response = FileResponse(path, filename=group.brief_filename, media_type='text/csv; charset=utf-8')
    response.headers['X-Checksum-SHA256'] = group.brief_checksum
    return response
