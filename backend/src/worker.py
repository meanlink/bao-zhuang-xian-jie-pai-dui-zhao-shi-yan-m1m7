import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report, ReviewGroup, ReviewItem
from src.services.business import analyze
from src.services.reports import deliver
from src.services import reviews
from src.services.audit import record
logging.basicConfig(level=logging.INFO)
def execute(job_id):
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return
        j.status='running'; db.commit()
        try:
            d=db.get(Dataset,j.dataset_id)
            result=analyze(d.records,j.params)
            json.dumps(result,allow_nan=False)
            j.result=result;j.status='completed';j.finished_at=time.time()
            record(db,'worker','job.completed',{'id':j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
        db.commit()
def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
    for job_id in queued:execute(job_id)
    with Session() as db:
        candidates=list(db.scalars(select(ReviewGroup).where(ReviewGroup.status.in_(reviews.RUNNABLE)).order_by(ReviewGroup.id).limit(5)))
        active=[g.id for g in candidates if any(item.status=='pending' for item in db.scalars(select(ReviewItem).where(ReviewItem.group_id==g.id)))]
    for group_id in active[:3]:reviews.process_group(group_id)
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())):
            d=deliver(db,r)
            if d:record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
        db.commit()
def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        db.commit()
    with Session() as db:
        reviews.recover_incomplete(db);db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            group=r.lpop('review:queue')
            if group:reviews.process_group(int(group))
        except Exception:
            # 队列不可用时不能挡住 SQL 兜底；按持久状态继续推进。
            logging.exception('Queue unavailable; continuing from durable SQL state')
            time.sleep(1)
        try:
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
