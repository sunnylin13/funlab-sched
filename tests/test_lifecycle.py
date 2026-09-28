"""_on_start/_on_stop 冪等與未啟動防護（SCH-05）。"""
from funlab.sched.service import SchedService


def test_on_stop_safe_when_never_started(scheduler, stub_service):
    # scheduler 未 start；現況 _on_stop 直拋 SchedulerNotRunningError
    SchedService._on_stop(stub_service)          # 不拋即過


def test_on_stop_idempotent_after_started(scheduler, stub_service):
    scheduler.start()
    SchedService._on_stop(stub_service)          # 第一次正常
    SchedService._on_stop(stub_service)          # 第二次（reload 競態下可能發生）也不得拋外逃例外
