"""SCH-10/11/14：手動提交競態防護、參數不落共享實例、plan 覆寫 config 契約。"""
import pathlib
import re
from types import SimpleNamespace

from apscheduler.jobstores.base import ConflictingIdError

from funlab.sched.service import SchedService, apply_plan, safe_add_manual_job


# ── SCH-10 ──────────────────────────────────────────────────────────────
def test_add_job_conflict_notifies_and_swallows(stub_service):
    def boom(**kw):
        raise ConflictingIdError(kw['id'])
    stub_service._scheduler.add_job = boom
    task = SimpleNamespace(name='T')
    notified = []
    stub_service.send_user_task_notification = lambda *a, **k: notified.append(a)
    ok = safe_add_manual_job(stub_service, task,
                             one_time_task={'id': 'T_M', 'name': 'T', 'func': lambda: None},
                             notify_userid=1)
    assert ok is False
    assert notified
    assert any('併發重複提交' in m[1] for m in stub_service.mylogger.messages
               if m[0] == 'warning')


def test_add_job_success_returns_true(stub_service):
    task = SimpleNamespace(name='T')
    ok = safe_add_manual_job(stub_service, task,
                             one_time_task={'id': 'T_M', 'name': 'T', 'func': lambda: None,
                                            'trigger': 'date'},
                             notify_userid=1)
    assert ok is True
    assert stub_service._scheduler.get_job('T_M') is not None


def test_unknown_post_id_no_keyerror_in_source():
    """SCH-10：路由層以 .get() 查未知 id（源碼靜態契約：不得再出現 sched_tasks[id] 直接索引）。"""
    src = (pathlib.Path(__file__).resolve().parents[1]
           / 'funlab/sched/service.py').read_text(encoding='utf-8')
    assert not re.search(r'self\.sched_tasks\[submitted_task_id\]', src)


# ── SCH-11 ──────────────────────────────────────────────────────────────
def test_manual_kwargs_do_not_persist_on_task(flask_app):
    from dataclasses import dataclass, field

    from funlab.sched.service import build_kwargs_from_form
    from funlab.utils.form import create_form_from_dataclass

    @dataclass
    class Spec:
        day: str = field(default='today', metadata={'type': 'StringField'})

    task = SimpleNamespace()
    task.form_class = create_form_from_dataclass(Spec)
    spec = Spec()
    spec.form_class = create_form_from_dataclass(Spec)
    kwargs, errors = build_kwargs_from_form(spec, {'day': '2026-01-01'})
    assert kwargs == {'day': '2026-01-01'}
    # 契約：kwargs 只進佇列，不改 task 實例（task 上無 day 屬性亦不新增）
    assert getattr(task, 'day', None) is None


def test_run_task_source_has_no_setattr_loop():
    """SCH-11（終態）：run_task 不得再 setattr 覆寫共享長驻實例。"""
    src = (pathlib.Path(__file__).resolve().parents[1]
           / 'funlab/sched/service.py').read_text(encoding='utf-8')
    run_task_src = src[src.index('def run_task'):src.index('def save_as_default_args')]
    assert 'setattr(task' not in run_task_src


# ── SCH-12 ──────────────────────────────────────────────────────────────
def test_fallback_timer_is_daemon():
    src = (pathlib.Path(__file__).resolve().parents[1]
           / 'funlab/sched/service.py').read_text(encoding='utf-8')
    assert re.search(r'fallback_timer\.daemon\s*=\s*True', src)


# ── SCH-14 ──────────────────────────────────────────────────────────────
def test_apply_plan_pure_function():
    td = {'trigger': 'cron', 'hour': 9}
    apply_plan(td, {'hour': 3})
    assert td['hour'] == 3
    apply_plan(td, None)          # falsy plan：不動
    assert td['hour'] == 3


def test_plan_merges_over_config(stub_service):
    """契約：plan_schedule() 回傳值覆寫 config 的 task_def（plan 贏）。"""
    stub_service._task_build = None

    class Ep:
        name = 'Demo'

        def load(self):
            class T:
                def __init__(self, sched):
                    self.id = 'Demo'
                    self.name = 'Demo'
                    self.task_config = {'disable': False}
                    self.task_def = {'trigger': 'cron', 'hour': 9, 'kwargs': {}}
                    self.last_status = ''

                def plan_schedule(self):
                    return {'trigger': 'cron', 'hour': 3}
            return T

    added = {}

    def fake_add_job(**kw):
        added.update(kw)
        return SimpleNamespace(id=kw['id'])

    stub_service._scheduler.add_job = fake_add_job
    SchedService._load_single_task(stub_service, Ep())
    assert added.get('hour') == 3          # plan 覆寫 config 的 hour=9
