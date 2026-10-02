# -*- coding: utf-8 -*-
"""t_b6d1f78d P3：手動執行異常白話化呈現（funlab-sched 呈現層映射）。

語意：任務若實作 humanize_exception(exc) → str|None 鉤子，listener 在手動
執行失敗時把白話一句存入 last_manual_exec_info['exception_plain']，通知訊息
以白話開頭、原始 exception 保留為明細；無法映射（無鉤子/回 None）→ 回落
原始文本，既有行為零變動。tasks.html 以 <details> 呈現「白話一句＋可展開明細」。

mock 模式沿用 tests/test_listener_job_id.py（FakeTask + 未綁定真邏輯）。
"""
from datetime import datetime

from apscheduler.events import (EVENT_JOB_ERROR, JobExecutionEvent)

from funlab.sched.service import SchedService


class HookTask:
    """帶 humanize_exception 鉤子的任務替身（BookKeepingTask 契約形狀）。"""

    def __init__(self, plain='白話：系統依設計拒算（E_X，設計行為）。'):
        self.name = 'BookKeeping'
        self.last_status = ''
        self.last_manual_exec_info = {}
        self._plain = plain

    def humanize_exception(self, exc):
        return self._plain


class PlainNoneTask(HookTask):
    def humanize_exception(self, exc):
        return None


def make_error_event(job_id, exception):
    return JobExecutionEvent(
        code=EVENT_JOB_ERROR, job_id=job_id, jobstore='default',
        scheduled_run_time=datetime.now(),
        retval=None, exception=exception)


def _seed(task):
    task.last_manual_exec_info = {'is_manual': True, 'summit_userid': 7,
                                  'kwargs': {}, 'args': None}
    return task


def test_hook_task_gets_plain_exception_field(stub_service):
    """有 humanize_exception 鉤子 → exception_plain 存入、白話進通知訊息。"""
    task = _seed(HookTask())
    stub_service.sched_tasks['BookKeeping'] = task
    notified = []
    stub_service.send_user_task_notification = lambda *a, **k: notified.append((a, k))

    SchedService._listener_all_event(
        stub_service,
        make_error_event('BookKeeping_M',
                         RuntimeError('Check ebokkeeping with error: [...]')))

    assert task.last_manual_exec_info['exception_plain'] == \
        '白話：系統依設計拒算（E_X，設計行為）。'
    # 原始 exception 保留（明細可展開溯源）
    assert 'Check ebokkeeping' in task.last_manual_exec_info['exception']
    # 通知以白話開頭，非 raw 堆疊
    msgs = [a[1] if len(a) > 1 else '' for a in notified]
    assert any('白話：系統依設計拒算' in str(m) for m in notified), notified


def test_no_hook_task_behaviour_unchanged(stub_service):
    """無鉤子任務：exception_plain 不入 key（或空），原始 exception 照舊。"""
    class PlainTask:
        name = 'Demo'
        last_status = ''

        def __init__(self):
            self.last_manual_exec_info = {}
    task = _seed(PlainTask())
    stub_service.sched_tasks['Demo'] = task
    SchedService._listener_all_event(
        stub_service, make_error_event('Demo_M', RuntimeError('broker down')))
    assert 'broker down' in task.last_manual_exec_info['exception']
    assert not task.last_manual_exec_info.get('exception_plain')


def test_hook_returning_none_falls_back(stub_service):
    """鉤子回 None（無法映射）→ 不寫白話、不假翻訳；原始文本照舊。"""
    task = _seed(PlainNoneTask())
    stub_service.sched_tasks['BookKeeping'] = task
    SchedService._listener_all_event(
        stub_service, make_error_event('BookKeeping_M', RuntimeError('weird')))
    assert 'weird' in task.last_manual_exec_info['exception']
    assert not task.last_manual_exec_info.get('exception_plain')


def test_hook_exception_never_breaks_listener(stub_service):
    """鉤子自身拋例外 → listener 自我吸收，原始 exception 仍入帳（SCH-13 紀律）。"""
    class BoomTask(HookTask):
        def humanize_exception(self, exc):
            raise ValueError('mapping bug')
    task = _seed(BoomTask())
    stub_service.sched_tasks['BookKeeping'] = task
    SchedService._listener_all_event(
        stub_service, make_error_event('BookKeeping_M', RuntimeError('real err')))
    assert 'real err' in task.last_manual_exec_info['exception']


def test_tasks_template_plain_plus_details():
    """tasks.html 契約：exception_plain 白話一句＋<details> 可展開原始明細。"""
    import os
    from funlab.sched import __file__ as sched_init
    tpl = os.path.join(os.path.dirname(sched_init), 'templates', 'tasks.html')
    src = open(tpl, encoding='utf-8').read()
    assert 'exception_plain' in src
    assert '<details' in src and '</details>' in src
