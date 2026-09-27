"""pytest 共用 fixture：假 Flask app（表單需要 context）、真 BackgroundScheduler+MemoryJobStore。

原則：不啟動正式服務、不碰券商/DB；需要排程執行緒的測試自行 start()，
teardown 一律嘗試 shutdown（未 start 時吞 SchedulerNotRunningError）。
"""
import types

import pytest
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.base import SchedulerNotRunningError

from funlab.core.config import Config


class FakeLogger:
    """SchedService.mylogger 的測試替身（含 progress/end_progress 介面）。"""

    def __init__(self):
        self.messages = []

    def info(self, msg='', *a, **k):
        self.messages.append(('info', str(msg)))

    def warning(self, msg='', *a, **k):
        self.messages.append(('warning', str(msg)))

    def error(self, msg='', *a, **k):
        self.messages.append(('error', str(msg)))

    def debug(self, msg='', *a, **k):
        self.messages.append(('debug', str(msg)))

    def progress(self, msg='', *a, **k):
        self.messages.append(('progress', str(msg)))

    def end_progress(self, msg='', *a, **k):
        self.messages.append(('end_progress', str(msg)))


@pytest.fixture(scope='session')
def flask_app():
    app = Flask(__name__)
    app.config.update({'WTF_CSRF_ENABLED': False, 'SECRET_KEY': 'test-only-key'})
    return app


@pytest.fixture(autouse=True)
def app_request_context(flask_app):
    """FlaskForm 實例化需要 request context；CSRF 關閉。"""
    with flask_app.test_request_context('/'):
        yield


@pytest.fixture
def scheduler():
    """未啟動的 BackgroundScheduler + MemoryJobStore。"""
    sch = BackgroundScheduler(
        jobstores={'default': MemoryJobStore()},
        job_defaults={'misfire_grace_time': 300, 'coalesce': True, 'max_instances': 1},
    )
    yield sch
    try:
        sch.shutdown(wait=False)
    except SchedulerNotRunningError:
        pass


@pytest.fixture
def stub_service(scheduler):
    """SchedService 的最小函數替身：以未綁定方法呼叫真實 SchedService 邏輯。"""
    from funlab.sched.service import SchedService

    svc = types.SimpleNamespace()
    svc.sched_tasks = {}
    svc._scheduler = scheduler
    svc.mylogger = FakeLogger()
    svc.send_user_task_notification = lambda *a, **k: None
    svc.running = False
    svc.state = scheduler.state
    # _listener_all_event（SCH-13 防呆外層）內部會委派 self._handle_listener_event；
    # 把真實未綁定方法綁到替身，確保測的是真邏輯而非被 try/except 吞掉的 AttributeError。
    svc._handle_listener_event = types.MethodType(SchedService._handle_listener_event, svc)

    class _App:
        def get_section_config(self, section, default=None, keep_section=False):
            return default if default is not None else Config({section: {}})

    svc.app = _App()
    return svc
