"""_listener_all_event 的 job_id 解析與防呆（SCH-01/SCH-13）。"""
from datetime import datetime

from apscheduler.events import (EVENT_JOB_EXECUTED, EVENT_JOB_ERROR,
                                JobExecutionEvent)

from funlab.sched.service import SchedService


class FakeTask:
    def __init__(self, name='Demo'):
        self.name = name
        self.last_status = ''
        self.last_manual_exec_info = {}


def make_event(job_id, exception=None):
    return JobExecutionEvent(
        code=EVENT_JOB_ERROR if exception else EVENT_JOB_EXECUTED,
        job_id=job_id, jobstore='default',
        scheduled_run_time=datetime.now(),
        retval=None, exception=exception)


def test_auto_job_with_M_inside_id_updates_status(stub_service):
    task = FakeTask()
    stub_service.sched_tasks['Fetch_MonthlyRevenue'] = task
    # 現況 replace('_M','') 會查 'FetchonthlyRevenue' → KeyError（實證 E1/E3）
    SchedService._listener_all_event(stub_service, make_event('Fetch_MonthlyRevenue'))
    assert task.last_status.startswith('Executed')


def test_manual_M_job_resolves_base_task_and_notifies(stub_service):
    task = FakeTask()
    task.last_manual_exec_info = {'is_manual': True, 'summit_userid': 7,
                                  'kwargs': {'x': 1}, 'args': None}
    stub_service.sched_tasks['Fetch_MonthlyRevenue'] = task
    notified = []
    stub_service.send_user_task_notification = lambda *a, **k: notified.append((a, k))

    SchedService._listener_all_event(stub_service, make_event('Fetch_MonthlyRevenue_M'))

    assert task.last_status.startswith('Executed')
    assert task.last_manual_exec_info['result_status'] == 'Executed'
    assert notified, '手動執行完成必須通知提交者'


def test_failed_manual_job_records_exception(stub_service):
    task = FakeTask()
    task.last_manual_exec_info = {'is_manual': True, 'summit_userid': 7,
                                  'kwargs': {}, 'args': None}
    stub_service.sched_tasks['Demo'] = task
    SchedService._listener_all_event(
        stub_service, make_event('Demo_M', exception=RuntimeError('broker down')))
    assert task.last_status.startswith('Failed')
    assert 'broker down' in task.last_manual_exec_info['exception']


def test_unknown_job_id_is_ignored_not_raised(stub_service):
    SchedService._listener_all_event(stub_service, make_event('GhostJob'))
    assert stub_service.sched_tasks == {}


def test_listener_swallows_internal_exceptions(stub_service):
    """事件處理內部炸任何例外，例外不得逃出 listener（SCH-13）。"""
    stub_service.sched_tasks = None      # 強迫內部 AttributeError（.get on None）
    # PLAN 原文 SchedulerEvent(code=0, job_id=...) 為 APScheduler 4.x 簽名且 code=0
    # 不會觸及 sched_tasks；改以 JobExecutionEvent（3.x 簽名）強制走內部存取路徑
    SchedService._listener_all_event(stub_service, make_event('AnyJob'))
    assert any(m[0] == 'warning' for m in stub_service.mylogger.messages)
