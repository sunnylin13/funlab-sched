"""SCH-07：多進程 WSGI 守門。

PM 書面裁示（2026-09-28，本卡 comment）：Q6=WSGI 續用 waitress——
守門以「任何多進程部署器」為拒跑對象（gunicorn/uwsgi 皆拒絕，waitress/flask 放行），
不引入 gunicorn／多進程假設；opt-in 開關 ALLOW_MULTI_WORKER_SCHEDULER 保留 PLAN 出口。
"""
from types import SimpleNamespace

from funlab.sched.service import SchedService


def _stub(scheduler, wsgi, allow=False):
    svc = SimpleNamespace()
    svc._scheduler = scheduler
    svc.app = SimpleNamespace(config={'WSGI': wsgi})
    svc.plugin_config = {'ALLOW_MULTI_WORKER_SCHEDULER': allow}
    svc.mylogger = SimpleNamespace(error=lambda *a, **k: None, info=lambda *a, **k: None)
    return svc


def test_gunicorn_blocks_scheduler_start(scheduler):
    svc = _stub(scheduler, 'gunicorn')
    SchedService._on_start(svc)
    assert not scheduler.running


def test_waitress_starts_scheduler(scheduler):
    svc = _stub(scheduler, 'waitress')
    SchedService._on_start(svc)
    assert scheduler.running


def test_gunicorn_opt_in_starts(scheduler):
    svc = _stub(scheduler, 'gunicorn', allow=True)
    SchedService._on_start(svc)
    assert scheduler.running


def test_uwsgi_blocks_scheduler_start(scheduler):
    """守門泛化到任何多進程部署器（PLAN SCH-07 (a) 同族風險）。"""
    svc = _stub(scheduler, 'uwsgi')
    SchedService._on_start(svc)
    assert not scheduler.running
