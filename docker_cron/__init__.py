#!/usr/bin/env python
# coding:utf-8

import os
import sys
import time
import docker
import logging
import hashlib

from crontab import CronTab

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor, ProcessPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import JobExecutionEvent, JobSubmissionEvent, EVENT_JOB_SUBMITTED, EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.job import Job as SchedulerJob

from docker.models.containers import Container

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


def get_current_user(container: Container) -> str:
    exit_code, output = container.exec_run(
        cmd="whoami", stderr=False, tty=True)
    if exit_code == 0:
        return output.decode('utf8').replace('\n', '')
    return 'root'


def get_user_crontab(container: Container, user: str) -> CronTab:
    cmd = "cat /etc/crontabs/{user}".format(user=user)
    exit_code, output = container.exec_run(cmd, stderr=False, tty=True)
    if exit_code != 0:
        return None

    tab = output.decode('utf8').replace('\t', ' ')
    if tab == '':
        return None

    return CronTab(tab=tab, user=user)


def get_system_crontab(container: Container) -> CronTab:
    cmd = "sh -c '[ -d /etc/cron.d ] && find /etc/cron.d ! -name \".*\" -type f -exec cat \{\} \;'"
    exit_code, output = container.exec_run(cmd, stderr=False, tty=True)
    if exit_code != 0:
        return None

    tab = output.decode().replace('\t', ' ')
    if tab == '':
        return None

    return CronTab(tab=tab, user=False)


def main():
    TIMEZONE = os.getenv('TIMEZONE', 'Asia/Shanghai')

    client = docker.DockerClient(
        base_url='unix://var/run/docker.sock', timeout=100)

    scheduler = BackgroundScheduler(
        executors={
            'default': ThreadPoolExecutor(max_workers=40),
            'processpool': ProcessPoolExecutor(max_workers=5)
        }, timezone=TIMEZONE)

    logging.basicConfig(stream=sys.stdout)
    logging.getLogger('apscheduler').setLevel(logging.ERROR)
    logger = logging.getLogger('docker-cron')
    logger.setLevel(logging.INFO)

    try:
        scheduler.start()
    except:
        pass

    def log_job_execution(event: JobExecutionEvent):
        job: SchedulerJob = scheduler.get_job(event.job_id)
        container: Container = job.args[0]
        if event.exception:
            logger.error("not executed %s container=[%s] exec=[%s] at=[%s] exception=[%s]", job.name,
                         'EVENT_JOB_ERROR' if event.code is EVENT_JOB_ERROR else 'EVENT_JOB_MISSED', container.name,
                         event.scheduled_run_time, event.exception)
        else:
            logger.info("executed container=[%s] exec=[%s] at=[%s]", container.name, job.name,
                        event.scheduled_run_time)

    def log_job_submission(event: JobSubmissionEvent):
        job: SchedulerJob = scheduler.get_job(event.job_id)
        container: Container = job.args[0]
        logger.info("submitted container=[%s] exec=[%s] at=[%s]", container.name, job.name,
                    '/'.join(map(str, event.scheduled_run_times)))

    scheduler.add_listener(log_job_execution, EVENT_JOB_ERROR |
                           EVENT_JOB_MISSED | EVENT_JOB_EXECUTED)
    scheduler.add_listener(log_job_submission, EVENT_JOB_SUBMITTED)

    # 监控services的启动、更新、关闭，实时进行定时任务的创建和修改
    while True:

        # 反复查询正在运行中的容器的cron.d目录，调整job列表
        try:
            containers = client.containers.list()
        except:
            continue

        scheduled_jobs: list[SchedulerJob] = scheduler.get_jobs()
        hashed_scheduled_jobs: dict[str, SchedulerJob] = {}
        for job in scheduled_jobs:
            # 若存储器中的任务所属容器当前不存在，则在存储请中删除此任务
            job_container = job.args[0]
            # job_command = job.args[1]
            job_hash = job.args[2]
            if job_container not in containers:
                scheduler.remove_job(job_id=job.id)
            else:
                hashed_scheduled_jobs[job_hash] = job

        def crontab_exec(container: Container, cmd: str, _: str):
            _, output = container.exec_run(cmd, tty=True, stream=True)
            for chunk in output:
                for line in chunk.decode().split('\n'):
                    print("\033[90m{cmd} : \033[0m {line}".format(
                        cmd=cmd, line=line))
            return 0

        def crontab_to_schedule(container: Container, crontab: CronTab):
            added = 0
            for it in crontab:
                if not it.is_enabled():
                    continue
                job_hash = hashsum(container.name + ': ' + str(it))
                if job_hash not in hashed_scheduled_jobs:
                    slices = str(it.slices)
                    if slices.startswith('@'):
                        slices = SPECIALS[slices.lstrip('@')]
                    job = scheduler.add_job(crontab_exec,
                                            CronTrigger.from_crontab(slices),
                                            args=[container,
                                                  it.command, job_hash],
                                            id=job_hash,
                                            name=it.command,
                                            replace_existing=True)
                    logger.debug('found %s.', job.name)
                    added += 1
                else:
                    del hashed_scheduled_jobs[job_hash]
            if added > 0:
                logger.info('%d jobs added.', added)
            return added

        for container in containers:
            try:
                user = get_current_user(container)
                user_crontab = get_user_crontab(container, user)
                if user_crontab:
                    crontab_to_schedule(container, user_crontab)

                system_crontab = get_system_crontab(container)
                if system_crontab:
                    crontab_to_schedule(container, filter(
                        lambda it: it.user == user, system_crontab))

            except:
                continue

        removing = len(hashed_scheduled_jobs)
        if removing > 0:
            for job_hash, job in hashed_scheduled_jobs.items():
                # 未命中的
                scheduler.remove_job(job_id=job.id)
                logger.debug('removed %s.', job.name)
            logger.info('%d jobs removed.', removing)

        time.sleep(10)


if __name__ == "__main__":
    main()
