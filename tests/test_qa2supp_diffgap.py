"""[QA2-補測 t_d721078d] Wave2 diff 缺口——funlab-sched。

覆蓋點：
- _unstage_task 首建副本分支（223-225）／_align_task_job 帶 old_task（292）
- _load_single_task manual-only 暫存（269）
- tasks 路由 save_args 路徑：驗證拒絕＋合法儲存（509-523, 539）
- _on_reload SCH-06 補 shutdown 分支（610-615）與同步發佈（618）
"""
import types
from dataclasses import dataclass, field
from types import SimpleNamespace

import flask
import pytest
from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore

from funlab.core.config import Config
from funlab.core.plugin import Plugin
from funlab.sched import service as svc_mod
from funlab.sched.service import SchedService
from funlab.utils.form import create_form_from_dataclass


# ------------------------------------------------------------------ SCH-04 暫存原語
def test_unstage_first_call_snapshots_published_dict(scheduler, stub_service):
    """_task_build 尚不存在時，_unstage_task 先拍現有快照再移除（223-225, 292）。"""
    stub_service._task_build = None
    stub_service._unstage_task = types.MethodType(SchedService._unstage_task, stub_service)
    published = SimpleNamespace(id='A')
    stub_service.sched_tasks = {'A': published}
    SchedService._unstage_task(stub_service, 'A')          # 223-225
    assert stub_service._task_build == {}
    # _align_task_job 帶 old_task → 走 292 卸載分支
    SchedService._align_task_job(stub_service, published, None, None)
    assert 'A' not in stub_service._task_build


def test_publish_then_snapshot_roundtrip(scheduler, stub_service):
    stub_service._task_build = None
    t = SimpleNamespace(id='B')
    SchedService._stage_task(stub_service, t)
    SchedService._publish_tasks(stub_service)
    assert list(SchedService._snapshot_tasks(stub_service)) == [t]
    assert stub_service._task_build is None


# ------------------------------------------------------------------ manual-only 暫存
class _FakeEP:
    name = 'ManualTask'

    def __init__(self, task_cls):
        self._cls = task_cls

    def load(self):
        return self._cls


@dataclass
class _ManualArgs:
    pass


def test_load_single_task_manual_only_stages(scheduler, stub_service):
    """無 trigger 的任務：last_status=manual-only 且寫入建構中副本（269）。"""
    stub_service._task_build = None
    stub_service._stage_task = types.MethodType(SchedService._stage_task, stub_service)
    task = _ManualArgs()
    task.id = 'manual1'
    task.name = 'ManualTask'
    task.task_config = {}
    task.task_def = {'id': 'manual1', 'name': 'ManualTask', 'func': lambda: None}
    task.plan_schedule = lambda: None

    cls = lambda svc: task                                # noqa: E731 task_class(self) 呼叫形式
    SchedService._load_single_task(stub_service, _FakeEP(cls))
    assert task.last_status == 'Loaded (manual-only)'
    assert stub_service._task_build == {'manual1': task}
    assert 'manual1' not in stub_service.sched_tasks       # 尚未發佈（SCH-04）


# ------------------------------------------------------------------ tasks 路由 save_args
@dataclass
class _SaveSpec:
    count: int = field(default=1, metadata={'type': 'IntegerField', 'label': 'count'})


@pytest.fixture
def tasks_client(monkeypatch, scheduler):
    """真 Blueprint＋真路由閉包，policy/模板以測試替身繞過（只為驅動閉包本體）。"""
    svc = types.SimpleNamespace()
    svc._scheduler = scheduler
    svc.sched_tasks = {}
    svc.mylogger = SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None,
        warning=lambda *a, **k: None, progress=lambda *a, **k: None,
        end_progress=lambda *a, **k: None, error=lambda *a, **k: None)
    svc.notifications = []
    svc.send_user_task_notification = lambda *a, **k: svc.notifications.append(k)
    svc.running = True
    svc.state = 1
    svc.name = 'sched'
    svc.blueprint = flask.Blueprint('sched_bp', __name__, url_prefix='/sched')
    svc._snapshot_tasks = lambda: list(svc.sched_tasks.values())
    monkeypatch.setattr(svc_mod, 'policy_required', lambda policy: (lambda f: f))
    monkeypatch.setattr(flask, 'render_template', lambda *a, **k: 'RENDERED')
    monkeypatch.setattr('flask_login.current_user', SimpleNamespace(id=42))
    SchedService.register_routes(svc)

    app = flask.Flask(__name__)
    app.config.update(SECRET_KEY='qa2-supp-key', WTF_CSRF_ENABLED=False, TESTING=True)
    app.register_blueprint(svc.blueprint)
    return app.test_client(), svc


def _save_task(svc, scheduler):
    task = _SaveSpec()
    task.id = 't1'
    task.name = 'T1'
    task.form_class = create_form_from_dataclass(_SaveSpec)
    task.task_def = {'id': 't1', 'name': 'T1', 'func': lambda **kw: None}
    svc.sched_tasks['t1'] = task
    return task


def test_save_args_rejects_invalid_form(tasks_client):
    """save_as_default_args 驗證拒絕：警告＋通知＋不落寫（509-519, 539）。"""
    client, svc = tasks_client
    task = _save_task(svc, svc._scheduler)
    resp = client.post('/sched/tasks', data={'id': 't1', 'save_args': '1', 'count': 'abc'})
    assert resp.status_code == 200
    assert task.task_def.get('kwargs') is None             # 拒絕寫入


def test_save_args_valid_updates_job_and_taskdef(tasks_client):
    """驗證通過：job.modify(kwargs)＋task_def 更新（520-523, 539）。"""
    client, svc = tasks_client
    task = _save_task(svc, svc._scheduler)
    svc._scheduler.add_job(id='t1', name='T1', func=lambda **kw: None, trigger='date',
                           run_date='2099-01-01 00:00:00', kwargs={})
    resp = client.post('/sched/tasks', data={'id': 't1', 'save_args': '1', 'count': '7'})
    assert resp.status_code == 200
    assert task.task_def['kwargs'] == {'count': 7}
    assert svc._scheduler.get_job('t1').kwargs == {'count': 7}


def test_unknown_task_id_warning_only(tasks_client):
    """SCH-10：未知 id 的 save_args POST 不 500（537 分支連帶）。"""
    client, svc = tasks_client
    resp = client.post('/sched/tasks', data={'id': 'ghost', 'save_args': '1'})
    assert resp.status_code == 200


# ------------------------------------------------------------------ _on_reload SCH-06
@pytest.fixture
def reload_scheduler():
    sch = BackgroundScheduler(jobstores={'default': MemoryJobStore()},
                              job_defaults={'misfire_grace_time': 300})
    yield sch
    try:
        sch.shutdown(wait=False)
    except SchedulerNotRunningError:
        pass


def test_on_reload_shuts_down_zombie_scheduler(reload_scheduler, monkeypatch):
    """SCH-06：plugin 基底跳過 _on_stop 致 scheduler 仍 running →
    _on_reload 必須補 shutdown 再 configure（610-615），並於同步載入後發佈（618）。"""
    monkeypatch.setattr(Plugin, '_init_configuration', lambda self: None)  # 跳過 plugin.toml 讀檔
    monkeypatch.setattr(Plugin, '_on_menu_reload', lambda self: None)

    svc = SchedService.__new__(SchedService)
    svc._scheduler = reload_scheduler
    svc.mylogger = SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None)
    svc.plugin_config = Config({'BACKGROUND_TASK_LOADING': False,
                                'job_defaults': {'coalesce': True}})
    svc._task_build = {'x': SimpleNamespace(id='x')}
    svc.sched_tasks = {}
    import threading
    svc._tasks_loaded = threading.Event()
    svc._tasks_loaded.set()
    loaded = []
    svc._load_tasks = lambda: loaded.append(True)

    reload_scheduler.start()
    assert reload_scheduler.running
    from apscheduler.schedulers.base import STATE_STOPPED
    svc._on_reload()                                      # 修正前：SchedulerAlreadyRunningError
    assert reload_scheduler.state == STATE_STOPPED        # 610-615 補 shutdown 生效
    assert loaded == [True]                               # 617 同步載入
    assert list(svc.sched_tasks.values())                 # 618 已發佈（x 在發佈快照中）
    assert svc._task_build is None
    assert svc._tasks_loaded.is_set()


def test_on_reload_shutdown_race_not_running_tolerated(reload_scheduler, monkeypatch):
    """610-615 競態防護：running 檢查通過後 shutdown 仍拋 SchedulerNotRunningError
    → 必須吞掉，reload 流程不中斷。"""
    monkeypatch.setattr(Plugin, '_init_configuration', lambda self: None)
    monkeypatch.setattr(Plugin, '_on_menu_reload', lambda self: None)

    svc = SchedService.__new__(SchedService)
    import threading
    svc._tasks_loaded = threading.Event()
    svc._tasks_loaded.set()
    svc.mylogger = SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None,
                                   warning=lambda *a, **k: None, error=lambda *a, **k: None)
    svc.plugin_config = Config({'BACKGROUND_TASK_LOADING': False,
                                'job_defaults': {'coalesce': True}})
    svc._task_build = None
    svc.sched_tasks = {}
    svc._load_tasks = lambda: None
    # running=True 但 shutdown 拋 NotRunning —— 模擬檢查與呼叫之間的狀態競態
    zombie = SimpleNamespace(running=True,
                             configure=lambda **kw: None,
                             shutdown=lambda wait=True: (_ for _ in ()).throw(SchedulerNotRunningError()))
    svc._scheduler = zombie
    svc._on_reload()                       # 614-615：except 吞掉，不向上拋
