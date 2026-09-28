"""第二次 reload 不炸：configure() 前 scheduler 必須回到 STOPPED（SCH-06）。"""
import types

from types import SimpleNamespace

from funlab.core.config import Config
from funlab.sched.service import SchedService


def _stub(scheduler):
    svc = SimpleNamespace()
    svc._scheduler = scheduler
    svc._tasks_loaded = __import__('threading').Event()
    svc._tasks_loaded.set()
    svc.mylogger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                   error=lambda *a: None, progress=lambda *a: None,
                                   end_progress=lambda *a: None)
    svc.plugin_config = Config({'BACKGROUND_TASK_LOADING': False,
                                'job_defaults': {'coalesce': True, 'max_instances': 1}})
    svc._load_config = types.MethodType(SchedService._load_config, svc)
    svc._load_tasks = lambda: None
    svc._publish_tasks = lambda: None
    return svc


def test_reload_recovers_from_running_scheduler(scheduler, stub_service):
    scheduler.start()                      # 模擬 _stop_executed 殘留導致 _on_stop 未執行的狀態
    svc = _stub(scheduler)
    # 直接測「補 shutdown + configure」这段核心路徑（跳過 super()._on_reload）
    if scheduler.running:
        scheduler.shutdown(wait=False)
    svc._load_config()                     # 修正前：SchedulerAlreadyRunningError
    # APScheduler 3.x state 為 int（STATE_STOPPED==0；PLAN 原文 'STOPPED' 為 4.x 字串口徑）
    from apscheduler.schedulers.base import STATE_STOPPED
    assert scheduler.state == STATE_STOPPED    # configure 合法且排程器可用
