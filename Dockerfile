FROM python:3-alpine

RUN pip install --no-cache-dir docker-cron==0.5.2

CMD ["/usr/local/bin/docker-cron"]