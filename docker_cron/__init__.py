#!/usr/bin/env python
# coding:utf-8

import os
import sys
import time
import docker
import logging
import hashlib
import apscheduler

from crontab import CronTab
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger

SPECIALS = {"reboot":   '@reboot',
            "hourly":   '0 * * * *',
            "daily":    '0 0 * * *',
            "weekly":   '0 0 * * 0',
            "monthly":  '0 0 1 * *',
            "yearly":   '0 0 1 1 *',
            "annually": '0 0 1 1 *',
            "midnight": '0 0 * * *'}


def hashsum(str):
    md5 = hashlib.md5()
    md5.update(str.encode('utf-8'))
    return md5.hexdigest()


def main():
    TIMEZONE = os.getenv('TIMEZONE', 'Asia/Shanghai')

    client = docker.DockerClient(
        base_url='unix://var/run/docker.sock', timeout=100)
    events = client.events(decode=True)

    scheduler = BackgroundScheduler(
        executors={'default': ThreadPoolExecutor(40)}, timezone=TIMEZONE)

    logging.basicConfig(stream=sys.stdout)
    logging.getLogger('apscheduler').setLevel(logging.INFO)

    try:
        scheduler.start()
    except:
        pass

    # 监控services的启动、更新、关闭，实时进行定时任务的创建和修改
    while True:

        # 反复查询正在运行中的容器的cron.d目录，调整job列表
        try:
            containers = client.containers.list()
        except:
            continue

        scheduled_jobs = scheduler.get_jobs()
        hashed_scheduled_jobs = {}
        for job in scheduled_jobs:
            # 若存储器中的任务所属容器当前不存在，则在存储请中删除此任务
            job_container = job.args[0]
            job_command = job.args[1]
            job_hash = job.args[2]
            if job_container not in containers:
                scheduler.remove_job(job_id=job.id)
            else:
                hashed_scheduled_jobs[job_hash] = job

        for container in containers:
            try:
                cmd = "sh -c '[ -d /etc/cron.d ] && find /etc/cron.d ! -name \".*\" -type f -exec cat \{\} \;'"
                exit_code, output = container.exec_run(
                    cmd=cmd, stderr=False, tty=True)
                if exit_code != 0:
                    continue

                tab = output.decode().replace('\t', ' ')
                if tab == '':
                    continue

                cron_jobs = CronTab(tab=tab, user=False)
                job_hashes = []
                for job in cron_jobs:
                    if not job.is_enabled():
                        continue
                    job_hash = hashsum(container.name + ': ' + str(job))
                    if job_hash not in hashed_scheduled_jobs:
                        slices = str(job.slices)
                        if slices.startswith('@'):
                            slices = SPECIALS[slices.lstrip('@')]
                        scheduler.add_job(lambda c, cmd, _: c.exec_run(cmd=cmd, stderr=False, tty=True),
                                          CronTrigger.from_crontab(slices),
                                          args=[container,
                                                job.command, job_hash],
                                          name=job.command)
                    else:
                        del hashed_scheduled_jobs[job_hash]
            except:
                continue

        for job_hash, job in hashed_scheduled_jobs.items():
            # 未命中的
            scheduler.remove_job(job_id=job.id)

        time.sleep(10)


if __name__ == "__main__":
    main()
