# -*- coding: utf-8 -*-
"""ADR-053（t_014eee40）：手動入口 kwargs['manually'] 單點注入＋save_args 恆 pop。

語意錨點（ADR-053 D1/D2，使用者「准 ADR-053」2026-10-03）：
- D1：手動入口（run_task 佇列 *_M job）的 kwargs 由框架於單點強制
  ``kwargs['manually'] = True``（無條件注入，覆寫任何表單值/漏帶欄）；
  自動場 job（cron/plan_schedule/save_args 註冊路徑）框架不注入。
- D2：save_as_default_args 寫入自動 job 的 kwargs 恆 pop 掉 manually
  （現況存 '' 屬髒值；口徑統一後 last_status 不再出現 manually: '' 誤導字樣）。
- 比照 SCH-01「_M 後綴＝手動」契約：*_M 佇列本身即手動證據，入口語意在
  服務端釘死、不经表單、不可偽造（G1→G2→G3 表單往返鏈逐段失真之根修復）。

mock 模式沿用 tests/test_qa2supp_diffgap.py（真 Blueprint＋真路由閉包，
policy/模板以測試替身繞過，只為驅動閉包本體）。
"""
import types
import dataclasses
from dataclasses import dataclass, field
from types import SimpleNamespace

import flask
import pytest

import funlab.sched.service as svc_mod
from funlab.sched.service import SchedService
from funlab.utils.form import create_form_from_dataclass


@dataclass
class ManualSpec:
    """BookKeepingTask 表單形狀縮影：manually HiddenField＋布林欄位。"""
    mgrs_accs_cash: str = field(default='', metadata={'type': 'StringField'})
    book_yesterday: bool = field(default=False, metadata={'type': 'BooleanField'})
    manually: bool = field(default=True, metadata={'type': 'HiddenField'})


@dataclass
class NoManuallySpec:
    """無 manually 欄的任務（ADR-053 Q2 已裁：無條件注入，免維護欄位清單）。"""
    count: int = field(default=1, metadata={'type': 'IntegerField'})


@pytest.fixture
def tasks_client(monkeypatch, scheduler):
    """真 Blueprint＋真路由閉包（比照 test_qa2supp_diffgap 模式）。"""
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
    app.config.update(SECRET_KEY='adr053-key', WTF_CSRF_ENABLED=False, TESTING=True)
    app.register_blueprint(svc.blueprint)
    return app.test_client(), svc


def _register_task(svc, spec_cls, task_id='bk'):
    t = spec_cls()
    t.id = task_id
    t.name = task_id.title()
    t.form_class = create_form_from_dataclass(spec_cls)
    t.task_def = {'id': task_id, 'name': t.name, 'func': lambda **kw: None}
    t.last_manual_exec_info = {}
    svc.sched_tasks[task_id] = t
    return t


def _queued_kwargs(svc, task_id='bk'):
    job = svc._scheduler.get_job(task_id + '_M')
    assert job is not None, f'{task_id}_M 一次性 job 未佇列'
    return job.kwargs


# ════════════════════════════════════════════════════════════════════════
# D1：手動入口單點無條件注入 manually=True
# ════════════════════════════════════════════════════════════════════════

def test_run_task_injects_true_when_form_sends_empty_string(tasks_client):
    """實案重放（G2/G4：HiddenField 渲染 value="" → POST manually=''）：
    佇列 job 的 kwargs 必 manually is True，髒值不得進 execute。"""
    client, svc = tasks_client
    _register_task(svc, ManualSpec)
    resp = client.post('/sched/tasks', data={
        'id': 'bk', 'run_task': '1', 'mgrs_accs_cash': '',
        'book_yesterday': '', 'manually': ''})
    assert resp.status_code == 200
    kwargs = _queued_kwargs(svc)
    assert kwargs['manually'] is True


def test_run_task_injects_true_when_field_omitted(tasks_client):
    """漏帶 manually 欄（探針 P3：field.data=None 路徑）→ 仍注入 True。"""
    client, svc = tasks_client
    _register_task(svc, ManualSpec)
    resp = client.post('/sched/tasks', data={
        'id': 'bk', 'run_task': '1', 'mgrs_accs_cash': ''})
    assert resp.status_code == 200
    kwargs = _queued_kwargs(svc)
    assert kwargs['manually'] is True


def test_run_task_overrides_client_supplied_false(tasks_client):
    """不可偽造：表單謊報 manually=False → 入口語意以 *_M 佇列為準，覆寫為 True。"""
    client, svc = tasks_client
    _register_task(svc, ManualSpec)
    resp = client.post('/sched/tasks', data={
        'id': 'bk', 'run_task': '1', 'manually': 'false'})
    assert resp.status_code == 200
    assert _queued_kwargs(svc)['manually'] is True


def test_run_task_injects_even_without_manually_field(tasks_client):
    """Q2 已裁「無條件注入」：任務無 manually 欄亦注入（契约單一，
    所有 execute 簽名皆收 manually=False 或 **kwargs）。"""
    client, svc = tasks_client
    _register_task(svc, NoManuallySpec, task_id='nm')
    resp = client.post('/sched/tasks', data={'id': 'nm', 'run_task': '1', 'count': '3'})
    assert resp.status_code == 200
    kwargs = _queued_kwargs(svc, task_id='nm')
    assert kwargs['manually'] is True
    assert kwargs['count'] == 3   # 其餘欄位零變動


def test_last_manual_exec_info_reflects_injection(tasks_client):
    """UI 除錯面：last_manual_exec_info['kwargs'] 與佇列一致（不再呈現 '' 髒值）。"""
    client, svc = tasks_client
    task = _register_task(svc, ManualSpec)
    client.post('/sched/tasks', data={'id': 'bk', 'run_task': '1', 'manually': ''})
    assert task.last_manual_exec_info['kwargs']['manually'] is True


def test_run_task_injection_precedes_job_assembly_source_lock():
    """源碼契約鎖：注入必在 build_kwargs_from_form 成功之後、
    one_time_task 組裝之前（SCH-11 唯一漏斗內，先改 dict 再入 job）。"""
    import pathlib
    import re
    src = (pathlib.Path(__file__).resolve().parents[1]
           / 'funlab/sched/service.py').read_text(encoding='utf-8')
    run_task_src = src[src.index('def run_task'):src.index('def save_as_default_args')]
    m_inj = re.search(r"kwargs\[\s*['\"]manually['\"]\s*\]\s*=\s*True", run_task_src)
    assert m_inj, 'run_task 需有 kwargs["manually"] = True 單點注入'
    m_asm = run_task_src.index("'kwargs'") if "'kwargs'" in run_task_src \
        else run_task_src.index('"kwargs"')
    assert m_inj.start() < m_asm, '注入必須在 one_time_task kwargs 組裝之前'


# ════════════════════════════════════════════════════════════════════════
# D2：save_args（自動 job 預設參數）恆 pop manually
# ════════════════════════════════════════════════════════════════════════

def test_save_args_never_stores_manually(tasks_client):
    """髒值清零：表單帶 manually='' 儲存預設 → 自動 job kwargs 與 task_def
    皆無 manually 鍵（last_status 不再誤導）。"""
    client, svc = tasks_client
    task = _register_task(svc, ManualSpec)
    svc._scheduler.add_job(id='bk', name='Bk', func=lambda **kw: None,
                           trigger='date', run_date='2099-01-01T00:00:00')
    resp = client.post('/sched/tasks', data={
        'id': 'bk', 'save_args': '1', 'mgrs_accs_cash': 'A=1', 'manually': ''})
    assert resp.status_code == 200
    assert 'manually' not in task.task_def['kwargs']
    assert 'manually' not in svc._scheduler.get_job('bk').kwargs
    assert task.task_def['kwargs']['mgrs_accs_cash'] == 'A=1'   # 其餘欄位照存


def test_save_args_pops_even_truthy_manually(tasks_client):
    """save 路徑＝自動 job 預設，入口非 *_M 佇列：即使表單謊報 True 亦 pop。"""
    client, svc = tasks_client
    task = _register_task(svc, ManualSpec)
    svc._scheduler.add_job(id='bk', name='Bk', func=lambda **kw: None,
                           trigger='date', run_date='2099-01-01T00:00:00')
    client.post('/sched/tasks', data={
        'id': 'bk', 'save_args': '1', 'manually': 'true'})
    assert 'manually' not in task.task_def['kwargs']
    assert 'manually' not in svc._scheduler.get_job('bk').kwargs
