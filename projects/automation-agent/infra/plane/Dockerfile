FROM harnes-plane-aio:v1.3.1-fixed

USER root

# A single Celery process is sufficient for local webhook development and avoids
# spawning one process per host CPU. Periodic and live-collaboration processes
# are not required for the issue-to-agent development path.
RUN sed -i 's/celery -A plane worker -l info/celery -A plane worker -l info --concurrency=1/' /app/backend/bin/docker-entrypoint-worker.sh \
    && sed -i '/\[program:beat\]/,/^$/ s/autostart=true/autostart=false/' /etc/supervisor/conf.d/supervisor.conf \
    && sed -i '/\[program:live\]/,/^$/ s/autostart=true/autostart=false/' /etc/supervisor/conf.d/supervisor.conf
