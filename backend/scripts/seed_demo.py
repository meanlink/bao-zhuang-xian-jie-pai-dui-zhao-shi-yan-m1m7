import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, ReviewGroup, ReviewItem
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import reviews
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        if not db.scalar(select(ReviewGroup).where(ReviewGroup.name=='班次改善成组评审')):
            analyst=db.scalar(select(User).where(User.role=='analyst'))
            admin=db.scalar(select(User).where(User.role=='admin'))
            g=ReviewGroup(name='班次改善成组评审',confidence=.95,alpha=.05,status='draft',created_by=analyst.id,created_at=time.time()-600)
            db.add(g);db.flush()
            for position,shift in enumerate(['早班','晚班']):
                db.add(ReviewItem(group_id=g.id,position=position,dataset_id=d.id,dataset_name=d.name,dataset_checksum=d.checksum,shift=shift,status='pending'))
            db.flush()
            reviews.lock(db,g.id,analyst)
            db.add(Audit(actor='seed',action='demo.review_group',detail={'group':g.id,'items':2},at=time.time()))
            group_id=g.id
        else:
            group_id=None
        db.commit()
        if group_id:
            # 独立会话按锁定的原成员执行、整组批准并发布，演示校正前后的完整依据。
            reviews.process_group(group_id)
            with Session() as worker_db:
                seeded=worker_db.get(ReviewGroup,group_id)
                admin=worker_db.scalar(select(User).where(User.role=='admin'))
                if seeded and seeded.status=='computed' and admin:
                    reviews.approve(worker_db,group_id,admin)
                    worker_db.commit()
                    reviews.publish(worker_db,group_id,admin)
                    worker_db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,ReviewGroup,ReviewItem]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
