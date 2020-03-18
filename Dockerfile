FROM python:3-alpine

RUN pip install --no-cache-dir docker-cron

CMD ["/usr/local/bin/docker-cron"]