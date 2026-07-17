"""Monthly cron: generate next month's social batch for human review.
Writes queue CSV + review JSON to the persistent disk. Publishing stays
manual (Buffer upload) until you flip on the Meta poster deliberately."""
import os
from datetime import datetime, date
from social_agent import generate_batch, schedule_batch, export_buffer_csv, export_review_file
from trending_service import TrendingService

db_dir = os.environ.get("DB_DIR", "/var/data")
svc = TrendingService(os.path.join(db_dir, "trending.db"))
chart = svc.trending("US", "week", date.today())["chart"]
posts = generate_batch(24, trending_chart=chart, region_label="US")
sched = schedule_batch(posts, datetime.utcnow(), per_day=1)
export_buffer_csv(sched, os.path.join(db_dir, "social_queue.csv"))
export_review_file(sched, os.path.join(db_dir, "social_review.json"))
print(f"Generated {len(sched)} posts -> review social_review.json before queueing")
