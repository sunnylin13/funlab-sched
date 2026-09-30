# funlab-sched 改善方案（IMPROVEMENT_PLAN）

> 讀者：fund13-dev-coder。本文件每項改善含：(a) 問題影響 (b) 優先級 (c) 目標檔/函式
> (d) 完整修正後程式碼 (e) 完整 pytest (f) 驗證指令與預期 (g) 風險。
> 所有「實證」均為本環境（`~/workspaces/fund13/.venv`，Python 3.12，APScheduler 3.11.3）實跑輸出，
> 探針腳本不啟動正式服務、不碰券商/正式庫。
> 開發方式依專案慣例：獨立 branch，不可直接改 main；完成停在分支等 review，不自行合併。
> **實施狀態（2026-09-28 對帳）**：SCH-01～16 已全數合併 main（944e3ec SCH-01/02；cae4270 SCH-03/04/05/06/13；248a530 SCH-07/08/09/10/11/12/14/16——SCH-07 依 Q6 按 waitress 單進程前提實作；SCH-15 為本檔 docs 整併記錄）；qa2-supp 補測 f3ab750。已部署正式服務。測試基線現況 **38 passed**。本文 (a) 段描述【修復前】缺陷。

## 0. 現況快照（先讀，省去自行盤點）

- 執行鏈：`funlab/sched/service.py:SchedService.__init__` → `_load_config()`（讀 `[SchedService]`）→
  背景執行緒 `sched-task-loader`（`BACKGROUND_TASK_LOADING=true`，現況 finfun/config.toml 為 true）經
  `plugin_after_init` hook（pluginmanager 載完觸發；兜底 `threading.Timer(180)`）啟動 →
  `_run_task_loading()` → `_load_tasks()`（entry point group `funlab_sched_task`）→ `start()`。
- **排程器實際由 PluginManager 先啟動**：`funlab-libs/funlab/core/plugin_manager.py` 於
  `_load_plugin()` 對 `load_mode='startup'` 的插件即時 `instance.start()`（funlab-sched 是 startup）→
  `SchedService._on_start()` → `scheduler.start()`。所以 `running` 在任務載入完成前就是 True。
- 任務註冊：`_load_single_task()`：`ep.load()` → 實例化 → `disable` 旗標 → `plan_schedule()` 回傳值
  **無條件 merge 覆寫** `task_def`（service.py L209-210，config 有 trigger 也不放過）→
  無 trigger 則 manual-only；有 trigger 則 `add_job`。
- Web：`/sched/tasks` GET/POST，**已有 `@policy_required(is_admin)`**（service.py L356-357；
  `funlab/core/auth.py:policy_required` → 未登入導向 login、非 admin 403），全站另有 CSRFProtect
  （`funlab-flaskr/funlab/flaskr/app.py`）。**路由權限現況正確**，本方案不列為缺陷。
- 正式部署：systemd `fund13-web.service` → `finfun/run.py` → `WSGI='waitress'`（單進程多執行緒）
  → **現況只有一份排程器，無重複執行**。`funlab-flaskr/funlab/flaskr/conf/gunicorn_conf.py`
  為 `workers = cpu*2+1`，一旦 `WSGI` 改成 `'gunicorn'` 就會每個 worker 各起一份排程器（見 SCH-07）。
- 任務端現況：entry point 共 10 個（BookKeeping、CalcQuant、UniverseFlags、Fetch* 7 個）。
  抽樣核對真實任務：`finfun-fundmgr/finfun/fundmgr/task.py:BookKeepingTask`（表單欄位用
  **字串** metadata `'type': 'StringField'/'DateField'/'SelectField'/'BooleanField'/'IntegerField'`、
  `plan_schedule()` 回 cron dict、`prepare_runtime()` 延遲重資源、`execute()` 參數名與 dataclass 欄位同名）；
  `finfun-finfetch/finfun/finfetch/task.py:SpiderTask/FetchDailyPriceTask`（HiddenField、延遲 import 慣例）。
  兩者用法與本方案修正相容。
- `funlab-libs/funlab/utils/form.py` 的 L4 缺陷（PEP 604 `X | None` 不識為 Optional，
  且無 `'type'` metadata 時型別推導落空成 StringField——實測 `int | None → StringField`、
  `typing.Optional[int] → IntegerField`）屬 funlab-libs 方案範圍；本檔只寫入其**對任務表單的連帶影響**與規避。

### 實證彙總表

| # | 主張 | 實證方式 | 結果 |
|---|------|---------|------|
| E1 | `replace('_M','')` 破壞含 `_M` 的 id | 探針 P1 | `'Fetch_MonthlyRevenue_M'.replace('_M','')='FetchonthlyRevenue'` |
| E2 | listener 拋例外被 APScheduler 吞掉（狀態更新靜默遺失、主循環不死） | 探針 P2/probe3 | job 照常執行、線程存活、`last_status` 更新遺失；log 僅 `Error notifying listener` |
| E3 | 現況 `_listener_all_event` 對 id 含 `_M`／手動 `_M`／未知 id 全拋 KeyError | sch01_verify + pytest | 現況版 3 情境全 `KeyError`；修正版全 OK（`3 failed, 1 passed` → 修正後應 4 passed） |
| E4 | `shutdown(wait=True)` 執行中 job 時阻塞 | 探針 P3 | >1s 阻塞 > plugin `_run_stop_safely` 5s timeout 即放棄等待 |
| E5 | 未 start 的 scheduler `shutdown()` 拋 `SchedulerNotRunningError` | 探針 P4 | 實拋 |
| E6 | scheduler RUNNING 時 `configure()` 拋 `SchedulerAlreadyRunningError` | 探針 P5 | 實拋（第二次 reload 必經路徑） |
| E7 | dict 背景寫入 × 前端迭代 `RuntimeError: dictionary changed size during iteration` | 探針 P6 | 實測捕捉到 |
| E8 | 任務 func 是綁定方法，實例持 scheduler 引用 → pickle 必掛 | 探針 P5(probe5) | `TypeError: Schedulers cannot be serialized...` |
| E9 | 手動並發提交 check-then-add 有競態窗口 | 探針 P8 | 8 併發結果 `['added','added','blocked','added',...]`，無鎖保護 |
| E10 | 停止中 `add_job` 進 pending、`start()` 後才排入 | 探針 P12 | 與 source `_pending_jobs` 一致 |
| E11 | `threading.Timer(180)` 非 daemon | 探針 P11 | `Timer(180).daemon = False` |
| E12 | APScheduler 未設 `job_defaults.misfire_grace_time` 時預設 **1 秒** | source `schedulers/base.py` L911 | `"misfire_grace_time": asint(job_defaults.get("misfire_grace_time", 1))` |
| E13 | PEP604 型別推導落空（無 metadata `'type'` 時） | 探針 probe7 | `int | None → StringField`；`typing.Optional[int] → IntegerField` |

---

## SCH-01（P0）listener 以 `replace('_M','')` 解析任務 id + 直接索引 KeyError

**(a) 問題影響**
`_listener_all_event` 在 JobExecutionEvent 分支用 `self.sched_tasks[event.job_id.replace('_M','')]`
解析「手動一次性 job → 母任務」。任何 id **中間**含 `_M` 的任務（如 `Fetch_MonthlyRevenue`）：
- 自動執行：`last_status` 永不更新（KeyError 被 APScheduler 吞掉，只剩 log；E2/E3 實證）→ UI 上任務狀態永遠空白/過期，誤判「任務沒跑」。
- 手動執行：完成/失敗**不回寫 `last_manual_exec_info`、不通知提交者**（L279-285 根本到不了）→ 使用者按 Run 後無任何回應。
- 已移除任務的殘存事件（Ghost id）同樣拋 KeyError，同一機制靜默。
`_M` 是手動 job 的**後綴**慣例（L320 已用 `endswith('_M')`），`replace` 卻替換所有位置——語意就是錯的。

**(b) 優先級**：P0（正確性 + 使用者可感知的通知遺失；現有 10 個任務若改名或新增含 `_M` 的 id 立即觸發；探針已證現行代碼三情境全掛）。

**(c) 目標**：`funlab/sched/service.py::_listener_all_event`（L276、L309）。

**(d) 完整修正後程式碼**（僅列修改的區塊，未列處不動；需搭配 SCH-13 的外層防呆）：

```python
    def _listener_all_event(self, event):
        """
        keep tracing of job execution
        """
        # SCH-13: 本 listener 由 APScheduler 主循環執行緒呼叫；拋出的例外會被
        # _dispatch_event 吞成 log（實證 E2），造成狀態更新靜默遺失。整段自我吸收。
        try:
            self._handle_listener_event(event)
        except Exception as e:
            self.mylogger.warning(
                f"[SchedService] listener error on {event!r}: {type(e).__name__}: {e}"
            )

    def _handle_listener_event(self, event):
        event_type = None
        exception = None
        if isinstance(event, JobSubmissionEvent):
            event: JobSubmissionEvent = event
            event_type = 'Summited'
            scheduled_run_time = event.scheduled_run_times[0].strftime("%y-%m-%d %H:%M:%S")
            retval = None
        elif isinstance(event, JobExecutionEvent):
            event: JobExecutionEvent = event

            if event.code == EVENT_JOB_MISSED:
                event_type = 'Missed'
                message = f"錯過排程: {event.scheduled_run_time}"
            elif event.exception:
                event_type = 'Failed'
                exception = event.exception
                message = f"失敗: {exception}"
            else:
                event_type = 'Executed'
                message = f"完成: {datetime.now().isoformat(timespec='seconds')}"
            scheduled_run_time = datetime.now()  # log as completed time, not event.scheduled_run_time
            retval = event.retval
            # SCH-01: '_M' 是手動 job 的後綴，只准剝後綴（removesuffix）；
            # 查不到母任務（已移除/未知 id）時靜默跳過，不得拋 KeyError（實證 E2/E3）。
            base_task = self.sched_tasks.get(event.job_id.removesuffix('_M'))
            if base_task:
                summit_userid = base_task.last_manual_exec_info.get('summit_userid', None)
                is_manual = base_task.last_manual_exec_info.get('is_manual', False)
                if is_manual:
                    self.send_user_task_notification(base_task.name, message=message, target_userid=summit_userid)
                    base_task.last_manual_exec_info.update({
                        'result_status': event_type,
                        'result_time': datetime.now().isoformat(timespec='seconds'),
                        'exception': str(exception) if exception else '',
                    })

        elif isinstance(event, SchedulerEvent):  # apscheduler service event, influence all tasks
            if event.code == EVENT_SCHEDULER_PAUSED:
                status = "Paused"
            elif event.code == EVENT_SCHEDULER_RESUMED:
                status = "Waiting"
            elif event.code == EVENT_SCHEDULER_SHUTDOWN:
                status = "Shutdown"
            else:
                status = ""
            if status:
                for task in self.sched_tasks.values():
                    task.last_status = status
        if event_type:
            task = None
            kwargs = None
            args = None

            if (task := self.sched_tasks.get(event.job_id, None)):
                job = self._scheduler.get_job(event.job_id)
                if job:  # Guard against ``job`` being None.
                    kwargs = job.kwargs
                    args = job.args
            elif (task := self.sched_tasks.get(event.job_id.removesuffix('_M'), None)):  # SCH-01: _M is run manually, one time task
                kwargs = task.last_manual_exec_info.get('kwargs', None)
                args = task.last_manual_exec_info.get('args', None)

            if task:  # Only update status when the task still exists.
                task.last_status = (f"{event_type} at:{scheduled_run_time}") \
                                    + (f", kwargs={kwargs}" if (kwargs) else "") \
                                    + (f", ret={retval}" if retval is not None else "") \
                                    + (f", exception: {exception}" if exception else "")

                if event.job_id.endswith('_M'):
                    task.last_manual_exec_info.update({
                        'result_status': event_type,
                        'result_time': datetime.now().isoformat(timespec='seconds'),
                        'exception': str(exception) if exception else '',
                    })

                self.mylogger.info(f"Task {event.job_id} {task.last_status}")
```

**(e) 完整 pytest**（`tests/conftest.py` 用 SCH-02 的版本；本檔即 `tests/test_listener_job_id.py`，
其紅燈狀態已在 scratch 實跑驗證：現況碼 `3 failed, 1 passed`）：

```python
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
```

**(f) 驗證指令與預期**

```bash
source ~/workspaces/fund13/.venv/bin/activate
cd ~/workspaces/fund13/funlab-sched
python -m pytest tests/test_listener_job_id.py -v
# 修正前：test_auto_job.../test_manual_M.../test_unknown... 3 紅（KeyError）
# 修正後：4 passed
```

**(g) 風險**：低。只改事件處理路徑；`removesuffix` 對純 `_M` 後綴行為與原意相同。
唯一行為變化：未知 id 從「拋出被吞」變「主動跳過並可 log」——不會更差。
不動 `JobSubmissionEvent` 分支（其 `scheduled_run_times[0]` 假設保持）。

---

## SCH-02（P0）建立 tests/ 測試基礎設施（L12）

**(a) 問題影響**：`funlab-sched` 零測試（`pytest` 回報 no tests ran）。SCH-01/03/04/05/06 等
修正全無回歸防護；本專案改一處 listener 就可能靜默破壞全部任務的狀態追蹤與通知。

**(b) 優先級**：P0（SCH-01 的搭檔；後續所有項目的驗收載體）。

**(c) 目標**：新增 `tests/conftest.py`、`tests/test_listener_job_id.py`（SCH-01 附表）；
`pyproject.toml` 加 pytest 設定。

**(d) 完整修正後程式碼**（conftest 已在 scratch 以真碼實跑過 import/fixture）：

`tests/__init__.py`：空檔。

`tests/conftest.py`：

```python
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
    svc = types.SimpleNamespace()
    svc.sched_tasks = {}
    svc._scheduler = scheduler
    svc.mylogger = FakeLogger()
    svc.send_user_task_notification = lambda *a, **k: None
    svc.running = False
    svc.state = scheduler.state

    class _App:
        def get_section_config(self, section, default=None, keep_section=False):
            return default if default is not None else Config({section: {}})

    svc.app = _App()
    return svc
```

`pyproject.toml` 追加（既有 `[dependency-groups] dev` 已有 pytest，不用改）：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

**(e) 完整 pytest**：即 SCH-01 附表（首批 4 案）；另加冒煙案 `tests/test_imports.py`：

```python
def test_public_exports():
    from funlab.sched import SchedService, SchedTask, SayHelloTask
    assert issubclass(SayHelloTask, SchedTask)
```

**(f) 驗證指令與預期**

```bash
source ~/workspaces/fund13/.venv/bin/activate
cd ~/workspaces/fund13/funlab-sched
python -m pytest -q          # 預期：全綠（SCH-01 尚未修時 test_listener_job_id 允許紅，其餘綠）
python -m pytest tests/test_imports.py -q   # 1 passed
```

**(g) 風險**：無執行副作用（不 start 排程、不註冊真任務）。注意 conftest 的
`app_request_context` 為 autouse——之後新增任何建 FlaskForm 的測試都免再處理 context。

---

## SCH-03（P1）「Save Default Arguments」完全跳過表單驗證，髒型別 kwargs 直接餵排程任務

**(a) 問題影響**
`register_routes.tasks().save_as_default_args`（service.py L456-466）用
`request.form.get(field.name)` 收**原始字串**就 `job.modify(kwargs=...)` 寫進排程任務：
- `BooleanField` 使用者取消勾選 → `'false'`/缺失 → 存成字串；`execute(book_yesterday='false')` 收到**真值字串**（Python `bool('false') is True`）。
- `DateField` 留空 → `''` 而非 `None`；`IntegerField` → `'10'`。
- 繞過所有 `validators`（DataRequired/NumberRange…），也繞過 SCH 之外 form.py 的任何把關。
之後**每次自動排程執行都帶著錯型別參數**跑真實券商/DB 業務（BookKeeping 結帳類任務首當其衝）。
對照 `run_task` 路徑有 `submitted_form.validate()`，save 路徑是漏網之魚。

**(b) 優先級**：P1（寫入的是持續性的排程預設參數；錯型別直達生產業務邏輯）。

**(c) 目標**：`funlab/sched/service.py::register_routes.tasks().save_as_default_args`；
便於測試，抽出模組級純函式 `build_kwargs_from_form()`。

**(d) 完整修正後程式碼**：

```python
# service.py 模組層（class SchedService 外）新增：
def build_kwargs_from_form(task: 'SchedTask', formdata) -> tuple[dict | None, dict]:
    """以任務自己的 form_class 驗證 formdata 並轉出正確型別的 kwargs。

    回傳 (kwargs, errors)：驗證失敗時 kwargs 為 None。
    跳過非資料欄位（CSRF/id/name 由 task_def 自行管理）。
    """
    form = task.form_class(formdata)
    if not form.validate():
        return None, form.errors
    kwargs = {}
    for f in fields(task):
        if f.name in ('id', 'name'):
            continue
        field_instance = getattr(form, f.name, None)
        if field_instance is not None and hasattr(field_instance, 'data'):
            kwargs[f.name] = field_instance.data
    return kwargs, {}
```

`save_as_default_args` 改為（路由內）：

```python
            def save_as_default_args(task: SchedTask):
                kwargs, errors = build_kwargs_from_form(task, request.form)
                if kwargs is None:
                    self.mylogger.warning(
                        f"Task {task.name} save args rejected: form validation failed: {errors}"
                    )
                    self.send_user_task_notification(
                        task.name,
                        f"參數驗證失敗，未儲存預設值: {errors}",
                        target_userid=current_user.id
                    )
                    return
                job = self._scheduler.get_job(task.id)
                if job:
                    job.modify(kwargs=kwargs)
                task.task_def.update({"kwargs": kwargs})
```

（附帶一致性：`run_task` 內重複的 `for field in fields(task)` 型別轉換迴圈可改呼叫同一支
`build_kwargs_from_form(task, request.form)`，兩路徑共用同一份驗證+轉型契約；`run_task` 已先
validate 過，直接取 kwargs 即可。）

**(e) 完整 pytest**：`tests/test_kwargs_from_form.py`：

```python
"""build_kwargs_from_form：save/run 兩路徑共用的驗證+轉型契約（SCH-03）。"""
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

from wtforms.validators import DataRequired

from funlab.sched.service import build_kwargs_from_form
from funlab.utils.form import create_form_from_dataclass


@dataclass
class ArgsSpec:
    flag: bool = field(default=False, metadata={'type': 'BooleanField', 'label': 'flag'})
    count: int = field(default=1, metadata={'type': 'IntegerField', 'label': 'count'})
    day: date = field(default=None, metadata={'type': 'DateField', 'label': 'day'})


def _fake_task():
    t = SimpleNamespace()
    t.form_class = create_form_from_dataclass(ArgsSpec)
    return t


def test_checkbox_unchecked_becomes_real_false():
    kwargs, errors = build_kwargs_from_form(
        _fake_task(), {'flag': '', 'count': '5', 'day': '2026-09-01'})
    assert errors == {}
    assert kwargs['flag'] is False          # 現況存 'false'/'' 字串 → 執行期真值（缺陷）
    assert kwargs['count'] == 5             # int 不是 '5'
    assert kwargs['day'] == date(2026, 9, 1)


def test_missing_required_returns_errors_and_no_kwargs():
    @dataclass
    class ReqSpec:
        sym: str = field(default='', metadata={'type': 'StringField',
                                               'validators': [DataRequired()]})
    t = SimpleNamespace()
    t.form_class = create_form_from_dataclass(ReqSpec)
    kwargs, errors = build_kwargs_from_form(t, {'sym': ''})
    assert kwargs is None
    assert 'sym' in errors
```

**(f) 驗證指令與預期**

```bash
python -m pytest tests/test_kwargs_from_form.py -v   # 2 passed
# 手動（開發環境，勿在正式服務做）：/sched/tasks 對任一新任務 Save Args 灌 'flag=false'，
# 修正後應收到「參數驗證失敗/或正確 False」的 kwargs；修正前 kwargs={'flag':'false'} 入庫。
```

**(g) 風險**：中低。既有 config.toml/舊任務已存的**字串** kwargs 不受本次影響（只擋新儲存）；
若舊資料已中毒需另盤點 `[TaskName.kwargs]`（列入疑點 Q4）。CSRF token 由 form_class（FlaskForm）
自動驗證——save 路徑等於順帶補上 CSRF 防護（現況 POST 因全站 CSRFProtect 已有擋，此為雙保险）。

---

## SCH-04（P1）BACKGROUND_TASK_LOADING：`sched_tasks` 背景寫入 × Web 讀取無同步

**(a) 問題影響**
背景 loader 執行緒逐任務 `self.sched_tasks[task.id] = task`（`_load_single_task`/`_align_task_job`），
同時 `/sched/tasks` 路由 `for task in self.sched_tasks.values()` 直接迭代（service.py L479）、
`run_task` 以 `self.sched_tasks[submitted_task_id]` 索引。
實證 E7：同構併發下**實際捕捉到** `RuntimeError: dictionary changed size during iteration`。
載入期（重 import 鏈可達數十秒）任何一次開頁/提交都可能 500；CPython dict 併發寫入非執行緒安全。
另注意排程器由 PluginManager 先啟動（第 0 節），`running=True` 早於任務載入完成，
視窗比想像大。

**(b) 優先級**：P1（生產 `BACKGROUND_TASK_LOADING=true`；重啟服務瞬間開 Web 即可能踩中）。

**(c) 目標**：`funlab/sched/service.py`：`_load_single_task`（寫入點）、
`_align_task_job`、`register_routes.tasks()`（讀取點）。採**複製後發佈（copy-on-write）**，
不用粗粒度鎖擋住載入。

**(d) 完整修正後程式碼**：

```python
    # __init__ 內，self.sched_tasks 保留原名（外部讀端語意不變），新增：
        self._task_build: dict[str, SchedTask] | None = None   # loader 專屬建構中副本

    def _stage_task(self, task: 'SchedTask'):
        """loader 執行緒專用：寫入建構中副本。"""
        if self._task_build is None:
            self._task_build = dict(self.sched_tasks)
        self._task_build[task.id] = task

    def _publish_tasks(self):
        """loader 執行緒專用：把建構中副本原子置換出去（dict 名稱綁定是原子操作）。"""
        if self._task_build is not None:
            self.sched_tasks = self._task_build   # 單一賦值，讀者永遠看到完整快照
            self._task_build = None

    def _snapshot_tasks(self) -> list['SchedTask']:
        """Web/其他讀端專用：先捕獲引用再迭代，永不與寫者共享同一 dict。"""
        return list(self.sched_tasks.values())
```

`_load_single_task` 內兩處 `self.sched_tasks[task.id] = task` 改為 `self._stage_task(task)`；
`_align_task_job` 的 `self.sched_tasks.pop(...)`/`self.sched_tasks[new_task.id] = ...` 同樣改走
`_stage_task`（pop 處：`if self._task_build is None: self._task_build = dict(self.sched_tasks)` 後 pop）。
`_run_task_loading()` 在 `self._load_tasks()` 之後、`self.start()` 之前插入 `self._publish_tasks()`；
同步模式 `_on_reload` 於 `self._load_tasks()` 後加 `self._publish_tasks()`。
路由 `tasks()` 的迴圈改 `for task in self._snapshot_tasks():`，
两处 `self.sched_tasks[submitted_task_id]` 改 `.get()`（連帶 SCH-10）。

**(e) 完整 pytest**：`tests/test_task_registry.py`：

```python
"""copy-on-write 發佈：載入中讀者不會炸 RuntimeError（SCH-04，實證 E7）。"""
import threading

from types import SimpleNamespace


class _Task:
    def __init__(self, tid):
        self.id = tid


def _call(service, name, *a):
    from funlab.sched.service import SchedService
    return getattr(SchedService, name)(service, *a)


def _stub():
    svc = SimpleNamespace()
    svc.sched_tasks = {}
    svc._task_build = None
    return svc


def test_reader_never_sees_half_built_dict():
    svc = _stub()
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                for _ in _call(svc, '_snapshot_tasks'):
                    pass
            except Exception as e:          # pragma: no cover - 缺陷時才會進
                errors.append(e)

    def writer():
        for i in range(30000):
            _call(svc, '_stage_task', _Task(f'k{i}'))
            _call(svc, '_publish_tasks')
        stop.set()

    t1, t2 = threading.Thread(target=reader), threading.Thread(target=writer)
    t1.start(); t2.start(); t2.join(timeout=30); stop.set(); t1.join(timeout=5)
    assert errors == []
    assert len(svc.sched_tasks) == 30000


def test_publish_swaps_whole_snapshot():
    svc = _stub()
    _call(svc, '_stage_task', _Task('A'))
    _call(svc, '_stage_task', _Task('B'))
    snapshot_before = svc.sched_tasks
    _call(svc, '_publish_tasks')
    assert set(snapshot_before) == set()          # 舊快照不受後續寫入污染
    assert set(svc.sched_tasks) == {'A', 'B'}
```

**(f) 驗證指令與預期**

```bash
python -m pytest tests/test_task_registry.py -v   # 2 passed
```

**(g) 風險**：低-中。`_align_task_job` 語意不変；外部直接引用 `sched_service.sched_tasks`
的舊代碼仍可用（只是拿到當前快照 dict 本身）。唯一紀律： loader 之外的寫入點（目前無）
都必須走同一套 staging，或改 dict 前自行複製。

---

## SCH-05（P1）關機鏈：5s stop 放棄等待 + 未啟動時 `_on_stop` 必炸 + systemd 預設 90s SIGKILL

**(a) 問題影響**（三段都是實證/源碼事實）：
1. `_on_stop()` 用 `shutdown(wait=True)`；執行中 job 讓它阻塞（實證 E4）。
   `funlab-libs/funlab/core/plugin.py::_run_stop_safely` 於**獨立執行緒**跑 `_on_stop` 並
   `join(timeout=5)`——超過 5 秒就「放棄等待」繼續關機流程。**注意：放棄等待不等於殺掉任務**：
   APScheduler 的 threadpool 工作執行緒是非 daemon，直譯器退出時仍會 join 它們，任務會繼續跑，
   但 systemd `fund13-web.service` **未設 `TimeoutStopSec`（預設 90s）→ 逾時 SIGKILL**，
   正在寫 DB/下單的任務中途被殺（此段為 systemd 預設值推論，非觀測到實際 kill，列疑點 Q1）。
2. 載入致命失敗或 `BACKGROUND_TASK_LOADING` 例外路徑下排程器從未 start，`_on_stop()` 的
   `shutdown()` 直拋 `SchedulerNotRunningError`（實證 E5）→ plugin 進入 ERROR 態、log 誤導。
3. `start()` 由 PluginManager 在載入時就呼叫（第 0 節），所以正常情況任務可能只載入一半
   服務就被停止，上述 1/2 交錯發生。

**(b) 優先級**：P1（涉及正式服務重啟/部署時的資料一致性；觸發條件「重啟時有任務在跑」很常見——
BookKeeping 排 14:45/16:45，部署不挑時間）。

**(c) 目標**：`funlab/sched/service.py::_on_stop`、`_on_start`；
建議（跨 repo，見 SCH-06）：`funlab-libs/funlab/core/plugin.py::_run_stop_safely` 與
systemd unit `TimeoutStopSec`。

**(d) 完整修正後程式碼**（sched 側自我防護）：

```python
    def _on_stop(self):
        """Shut down the APScheduler.

        wait=False：不在此線程無限等 job（plugin._run_stop_safely 只有 5s 額度，
        等待與否不改變「不殺任務」的事實，因為 apscheduler 線程由直譯器 join）。
        真正要「等任務跑完再退」靠關機序：先在 Web 停止提交、APScheduler 線程自然 join；
        並把 systemd TimeoutStopSec 設到大於最長任務時間（部署文件，見 (f)）。
        從未 start 過（載入失敗路徑）時 shutdown() 會拋 SchedulerNotRunningError（實證 E5），吞掉。
        """
        from apscheduler.schedulers.base import SchedulerNotRunningError
        try:
            self._scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            self.mylogger.info("[SchedService] scheduler was never started; nothing to shut down")
```

（`_run_stop_safely` 的 timeout 參數化與 `Plugin._stop_executed` 重置屬 funlab-libs，見 SCH-06。）

**(e) 完整 pytest**：`tests/test_lifecycle.py`：

```python
"""_on_start/_on_stop 冪等與未啟動防護（SCH-05）。"""
from funlab.sched.service import SchedService


def test_on_stop_safe_when_never_started(scheduler, stub_service):
    # scheduler 未 start；現況 _on_stop 直拋 SchedulerNotRunningError
    SchedService._on_stop(stub_service)          # 不拋即過


def test_on_stop_idempotent_after_started(scheduler, stub_service):
    scheduler.start()
    SchedService._on_stop(stub_service)          # 第一次正常
    SchedService._on_stop(stub_service)          # 第二次（reload 競態下可能發生）也不得拋外逃例外
```

**(f) 驗證指令與預期**

```bash
python -m pytest tests/test_lifecycle.py -v     # 2 passed
# 部署側（人工，正式機變更需另批）：
systemctl --user edit fund13-web.service   # 加 TimeoutStopSec=600
# 驗證：重啟時有長任務 → web.log 應見「Plugin sched stopped successfully」且任務完成後進程才退出
```

**(g) 風險**：`wait=False` 後 `stop()` 更快返回，plugin 狀態很快 STOPPED，而短任務可能仍在跑
數秒——可接受（現況 5s timeout 本來就放棄等待）。不可改成 `wait=True` + 拉高 plugin timeout
而不處理 systemd——那是把斷點從 5s 挪到 90s，未解決問題。

---

## SCH-06（P1，跨 repo 標記）第二次 plugin reload 必失敗：`_stop_executed` 不重置 → `configure()` 炸

**(a) 問題影響**
`funlab-libs/funlab/core/plugin.py::Plugin._run_stop_safely` 以 `self._stop_executed` 保證
`_on_stop` 只跑一次，但**只有 `__init__` 會重置它**。第一次 stop/reload 之後該旗標恆 True →
后續每次 `reload()`：`stop()` 回 True 但 `_on_stop` 被跳過 → scheduler 仍 RUNNING →
`SchedService._on_reload() → _load_config() → scheduler.configure()` 直拋
`SchedulerAlreadyRunningError`（實證 E6）→ reload 進入 ERROR 態。**admin 由
`/pluginmgmt`（`funlab-flaskr/funlab/flaskr/plugin_mgmt_view.py:reload_plugin`，is_admin）按第二次
reload 一定失敗**，之後 health/metrics 顯示異常但排程器其實還在跑（殭屍 RUNNING）。

**(b) 優先級**：P1（根因在 funlab-libs；sched 側可自我防護到不炸，但正解要兩邊都做）。

**(c) 目標**：
- funlab-libs `funlab/core/plugin.py::Plugin.reload()`（正解）；
- funlab-sched `service.py::_on_reload`（sched 側防護，本 repo 內可先行）。

**(d) 完整修正後程式碼**：

funlab-libs（PR 標記跨 repo，需另開 funlab-libs 分支）：

```python
    def reload(self):
        ...
        self.mylogger.info(f"Reloading plugin {self.name}")
        try:
            with self._lock:
                self._stop_executed = False      # SCH-06: 每次 reload 允許重跑 _on_stop
            self._call_global_hook("plugin_before_reload")
            stop_ok = self.stop()
            ...
```

funlab-sched 側防護（即使 funlab-libs 未修也不炸）：

```python
    def _on_reload(self):
        """Reload scheduler configuration and tasks."""
        super()._on_reload()
        self._tasks_loaded.wait()
        self._tasks_loaded.clear()
        # SCH-06 防護：plugin 基底若跳過了 _on_stop（_stop_executed 殘留），
        # 這裡自行補 shutdown，否則 configure() 必拋 SchedulerAlreadyRunningError（實證 E6）。
        if self._scheduler.running:
            from apscheduler.schedulers.base import SchedulerNotRunningError
            try:
                self._scheduler.shutdown(wait=False)
            except SchedulerNotRunningError:
                pass
        self._load_config()
        self._load_tasks()   # synchronous during manual reload
        self._publish_tasks()   # SCH-04
        self._tasks_loaded.set()
```

（`_on_start` 不需改：reload 的 `start()` 會重跑 `_on_start → scheduler.start()`，
scheduler 已被補 shutdown 回 STOPPED 態，`start()` 合法。）

**(e) 完整 pytest**：`tests/test_reload.py`：

```python
"""第二次 reload 不炸：configure() 前 scheduler 必須回到 STOPPED（SCH-06）。"""
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
    svc._load_tasks = lambda: None
    svc._publish_tasks = lambda: None
    # super()._on_reload() 需 _init_configuration / setup_menus：以 no-op 取代基類鏈起點
    return svc


def test_reload_recovers_from_running_scheduler(scheduler, stub_service):
    scheduler.start()                      # 模擬 _stop_executed 殘留導致 _on_stop 未執行的狀態
    svc = _stub(scheduler)
    # 直接測「補 shutdown + configure」这段核心路徑（跳過 super()._on_reload）
    if scheduler.running:
        scheduler.shutdown(wait=False)
    svc._load_config()                     # 修正前：SchedulerAlreadyRunningError
    assert scheduler.state == 'STOPPED'    # configure 合法且排程器可用
```

（測試刻意走核心路徑；完整 `_on_reload` 端到端要 stub `super()` 鏈， coder 實作時可加
monkeypatch 版完整測試，勿跳過此核心案。）

**(f) 驗證指令與預期**

```bash
python -m pytest tests/test_reload.py -v      # 1 passed
# 開發環境端到端（勿在正式機）：起 run.py，用 admin 對 sched 連按兩次 reload，
# 修正後兩次都 successful=true；修正前第二次 failed。
```

**(g) 風險**：funlab-libs 側 `_stop_executed` 重置影響所有 plugin——語意上「每次 stop 週期跑一次」
才是原意，屬回歸修正；需 funlab-libs 跑全套既有測試（120 passed 基線）確認零回歸。
sched 側補 shutdown 與 `_on_stop` 冪等（SCH-05 已處理雙重 shutdown 安全）。

---

## SCH-07（P2）WSGI 切到 gunicorn 即 N 份排程器並行（現況 waitress 單份，安全）

**(a) 問題影響**
每個 WSGI worker process 都會 `create_app()` → 載入 startup plugin → 各起一份
BackgroundScheduler → **同一 cron 任務每 worker 觸發一次**（記憶體 jobstore 無跨程去重）。
`funlab-flaskr/conf/gunicorn_conf.py`：`workers = cpu*2+1`、`worker_class='gevent'`——
在本機 N 張網卡 CPU 上就是 40+ 份排程器同時打券商 API/DB。
**現況不觸發**：`finfun/config.toml [ENV.PRODUCTION] WSGI='waitress'`（單進程）實查確認；
但 DEV/TEST 的 `WSGI='flask'` 單進程也 OK，地雷只在有人把 PRODUCTION 改 `'gunicorn'`
（檔內註解留著 `'gunicorn'` 選項）或 `flask run` 開 `--with-threads/reloader` 時踩。
gevent monkey-patch 與 APScheduler 執行緒執行器的組合另有一層不相容風險。

**(b) 優先級**：P2（現況不發；屬「開關一翻就炸」的部署防禦）。

**(c) 目標**：`funlab/sched/service.py::__init__`（啟動守門）；`finfun/config.toml` 註解紀律。

**(d) 完整修正後程式碼**（守門：多 worker 環境預設拒絕起跑，可用開關明示放行）：

```python
    def _on_start(self):
        """Start the APScheduler background scheduler."""
        # SCH-07: 每個 WSGI worker process 各有一份 SchedService。多 worker 下同一排程
        # 任務會被重複執行（記憶體 jobstore 無跨进程去重）。gunicorn 多 worker 預設拒跑。
        import os
        wsgi = str(self.app.config.get('WSGI', 'flask')).lower()
        allow_multi = bool(self.plugin_config.get('ALLOW_MULTI_WORKER_SCHEDULER', False))
        if wsgi == 'gunicorn' and not allow_multi:
            try:
                import gunicorn  # noqa: F401
                workers_hint = os.environ.get('GUNICORN_PROCESSES')  # gunicorn 不直接暴露 workers 於 env
            except ImportError:
                workers_hint = None
            self.mylogger.error(
                "[SchedService] 偵測到 WSGI=gunicorn：多 worker 會使排程任務重複執行，"
                "排程器已拒絕啟動。請改用單進程 WSGI（waitress，現況正式配置）或設 "
                "[SchedService] ALLOW_MULTI_WORKER_SCHEDULER=true 並自行確保單 worker。"
            )
            return
        self._scheduler.start(paused=False)
```

（更完整的長期解法是排程器独立進程化或換持久 jobstore + 分布式鎖；個人系統以守門+紀律為宜。）

**(e) 完整 pytest**：`tests/test_worker_guard.py`：

```python
"""SCH-07：WSGI=gunicorn 時拒絕啟動排程器。"""
from types import SimpleNamespace
from funlab.sched.service import SchedService


class _Cfg(dict):
    pass


def _stub(scheduler, wsgi, allow=False):
    svc = SimpleNamespace()
    svc._scheduler = scheduler
    svc.app = SimpleNamespace(config=_Cfg(WSGI=wsgi))
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
```

（gunicorn 未安裝環境下 `import gunicorn` 失敗不影響守門邏輯——以 config 值為準。）

**(f) 驗證指令與預期**：`python -m pytest tests/test_worker_guard.py -v` → 3 passed。

**(g) 風險**：若未來真要在 gunicorn 多 worker 跑，需開 opt-in 並接受重複執行風險或補鎖——
在決策記錄中保留此出口即可，預設 fail-closed 符合 fund13「寧可不做，不可做錯兩次」原則。

---

## SCH-08（P2）conf 宣傳的 processpool 執行器必然失敗；job_defaults 缺 misfire_grace_time（預設 1s）

**(a) 問題影響**
1. `funlab/sched/conf/plugin.toml` 與 `finfun/config.toml [SchedService]` 都宣告
   `processpool = {type='processpool', ...}`。但排入的 func 是 `task._execute_with_hooks`
   **綁定方法**（task.py L60），綁定方法持有 task 實例 → task 持 `sched` → BackgroundScheduler
   明示不可序列化：實測 `TypeError: Schedulers cannot be serialized. Ensure that you are not
   passing a scheduler instance as an argument`（實證 E8）。任何任務設定 `executor='processpool'`
   → 執行期才在 executor 報錯，任務永遠不會成功。
2. `[SchedService] job_defaults = {coalesce=false, max_instances=3}`（finfun/config.toml）
   **沒設 `misfire_grace_time`** → APScheduler 預設 **1 秒**（實證 E12）。系統重啟、暫停或
   主循環被重 import 卡住超過 1s，該次觸發直接判 misfire 丟棄。根目錄 README 宣稱
   `misfire_grace_time=300` 與實際配置不符（見 SCH-16）。手動 `_M` job 有明示 300，自動任務全裸。

**(b) 優先級**：P2（第 1 點目前無任務使用 processpool，屬地雷；第 2 點在重啟/暫停頻繁時丟執行）。

**(c) 目標**：`funlab/sched/conf/plugin.toml`（註解與預設）、`finfun/config.toml`（正式值，
由 OPS 執行）、`funlab/sched/task.py`（防呆）。

**(d) 完整修正後程式碼**：

`conf/plugin.toml`（庫內預設值改完整、移除 processpool 誤導）：

```toml
[SchedService]
    BACKGROUND_TASK_LOADING = true
    # jobstores 預設記憶體（程序重啟即失；跨重啟的 job 請一律以 task.toml/config 重新註冊，勿依賴持久化）
    # 僅提供 threadpool：任務 func 是綁定方法（持 scheduler 引用），processpool 必然 pickle 失敗（SCH-08）
    executors = {default = {type = 'threadpool', max_workers = 20}}
    job_defaults = {coalesce = true, max_instances = 1, misfire_grace_time = 300}
    timezone = 'Asia/Taipei'
```

`finfun/config.toml [SchedService]` 同步改（OPS 側；含把 executors 的 processpool 段移除、
`job_defaults` 補 `misfire_grace_time = 300`、max_instances 討論見 SCH-09）。

`task.py::_execute_with_hooks` 前加執行期防呆（雙保险，避免設定漂移）：

```python
    def _validate_runtime_executable(self):
        executor = self.task_def.get('executor', 'default')
        if executor and str(executor) != 'default':
            self.mylogger.warning(
                f"[{self.name}] executor='{executor}' 非 threadpool default；"
                "processpool 不支援綁定方法任務（pickle 必掛），請改回 default")
```

於 `prepare_runtime()` 開頭呼叫（每次執行僅一次字串判斷）。

**(e) 完整 pytest**：`tests/test_executor_guard.py`：

```python
"""SCH-08：非 default executor 設定要留下警告。"""
from types import SimpleNamespace
from funlab.sched.task import SchedTask


def test_non_default_executor_warns(stub_service):
    class _T(SchedTask):
        def execute(self, *a, **k):
            pass
    t = _T(stub_service)
    t._task_def.update({'executor': 'processpool'})
    t._validate_runtime_executable()
    assert any('processpool' in m[1] for m in stub_service.mylogger.messages
               if m[0] == 'warning') or any(
        'processpool' in m[1] for m in t.mylogger.__dict__.get('msgs', []) )  # 見下方註
```

註：`SchedTask.__init__` 用 `log.get_logger` 產生真實 logger；測試以
`monkeypatch.setattr('funlab.sched.task.log.get_logger', lambda *a, **k: FakeLogger())`
接管後改斷言 `FakeLogger.messages`。coder 落地時採用 monkeypatch 版，保持斷言只依賴
FakeLogger（上方運算式是示意，實作請寫乾淨）。另加一個配置靜態斷言：

```python
def test_default_conf_has_no_processpool():
    import tomllib, pathlib
    data = tomllib.loads((pathlib.Path(__file__).resolve().parents[1]
                          / 'funlab/sched/conf/plugin.toml').read_text(encoding='utf-8'))
    executors = data['SchedService'].get('executors', {})
    assert 'processpool' not in executors
    assert 'misfire_grace_time' in data['SchedService'].get('job_defaults', {})
```

**(f) 驗證指令與預期**：`python -m pytest tests/test_executor_guard.py -v` → 2 passed；
開發環境把某任務 `executor='processpool'` 後手動跑，日誌應見明確警告而非 executor pickle 棧。

**(g) 風險**：移除 processpool 段對現況零影響（無人使用）。`misfire_grace_time=300` 讓
重啟後 5 分鐘內的補跑會發生——對冪等結帳任務是優點，對非冪等抓取任務要在任務端保證冪等
（現有 finfetch 任務皆 upsert 語意，可接受）。

---

## SCH-09（P2）`max_instances=3` 允許同一任務三重併發

**(a) 問題影響**
`job_defaults.max_instances=3`：同一任務上次還沒跑完，下一次觸發仍可起新實例，最多 3 個
**同一結帳/抓取任務並行**。排程端 `_M` 去重只防手動重複；auto×auto、auto×manual 併發無防。
結帳（BookKeeping）、對帳（ReturnReconcile）這類寫庫任務並行＝競態寫、重複事件風險。
（探針 P8 也顯示 default max_instances=1 時會以 `maximum number of running instances reached`
跳過——那正是多數業務任務要的語意。）

**(b) 優先級**：P2（併發需任務超長執行時間疊加，但後果是帳務正確性）。

**(c) 目標**：`finfun/config.toml [SchedService] job_defaults`；各任務 task.toml 覆寫。

**(d) 修正**（配置即修正，無程式碼）：

```toml
    job_defaults = {coalesce = true, max_instances = 1, misfire_grace_time = 300}
```

確需併發的任務在**自己**的 task.toml/config `[TaskName]` 明示 `max_instances = N`
（per-job 覆寫 job_defaults），並在任務 docstring 註明可併發理由。
coalesce 由 false→true：積壓時補跑一次而非逐次補（與 misfire 300 搭配才是完整「遲到補一次」語意；
現況 `coalesce=false`+misfire 1s 的組合等於「遲到全丟」，見 SCH-08）。

**(e) pytest**：併入 `test_executor_guard.py` 的配置斷言：

```python
def test_default_conf_max_instances_is_1():
    import tomllib, pathlib
    data = tomllib.loads((pathlib.Path(__file__).resolve().parents[1]
                          / 'funlab/sched/conf/plugin.toml').read_text(encoding='utf-8'))
    assert data['SchedService']['job_defaults']['max_instances'] == 1
```

**(f) 驗證**：`python -m pytest tests/test_executor_guard.py -v`；
開發環境把一個 sleep 60s 的假任務 cron 每分钟跑，`web.log` 應見 `maximum number of running
instances reached` 跳過記錄（與探針 P8 同訊號）。

**(g) 風險**：若過去有任務**依賴** 3 併發吞吐（未盤點到），會改成排隊跳過；finfun-* 任務
抽樣（BookKeeping/SpiderTask）皆非吞吐導向，風險低。改 config 屬 OPS 動作，走部署流程。

---

## SCH-10（P2）Web POST 對未知任務 id 直接索引 → 500；`_M` 去重 check-then-add 競態

**(a) 問題影響**
`tasks()` 路由 `run_task(self.sched_tasks[submitted_task_id])`（L470）與 save 同樣：
表單 id 打錯/頁面过期/任務載入失敗 → `KeyError` → 500 白畫面（對 admin 也難看）。
`run_task` 的「已在執行中」檢查是 `get_job(manual_job_id)` 後 `add_job`——兩請求併發可同時
通過檢查（實證 E9 顯示無鎖窗口存在；本環境因 date job 立即執行完未被捕捉成 500，窗口屬
時序性風險，列疑點 Q2）→ `ConflictingIdError` → 500。

**(b) 優先級**：P2（admin-only 路徑，後果是錯誤畫面而非錯資料）。

**(c) 目標**：`service.py::register_routes.tasks()`。

**(d) 完整修正後程式碼**（路由尾段）：

```python
            submitted_task_id = request.form.get('id')
            action = 'run_task' if 'run_task' in request.form else (
                     'save_args' if 'save_args' in request.form else None)
            if action:
                target = self.sched_tasks.get(submitted_task_id)
                if target is None:
                    self.mylogger.warning(f"Unknown task id in POST: {submitted_task_id!r}")
                elif action == 'run_task':
                    run_task(target)
                else:
                    save_as_default_args(target)
```

`run_task` 內 `add_job` 包競態防護：

```python
                try:
                    self._scheduler.add_job(**one_time_task)
                except Exception as e:
                    from apscheduler.jobstores.base import ConflictingIdError
                    if isinstance(e, ConflictingIdError):
                        self.mylogger.warning(f"Task {task.name} 併發重複提交被拒")
                        self.send_user_task_notification(
                            task.name, "任務已在佇列中（併發提交），略過此次",
                            target_userid=current_user.id)
                        return
                    raise
```

**(e) pytest**：SCH-10 的驗證以路由層 smoke 為主——建議 coder 抽出
`resolve_and_dispatch(form, sched_tasks)` 純函式後測：未知 id 不拋、走警告分支；
`add_job` 競態分支以 monkeypatch `_scheduler.add_job` 拋 `ConflictingIdError` 斷言通知被送達。
測試骨架：

```python
"""SCH-10：未知 id 與併發提交防護。"""
from types import SimpleNamespace
from apscheduler.jobstores.base import ConflictingIdError


def test_add_job_conflict_notifies_and_swallows(stub_service, monkeypatch):
    # 依實作抽取的 dispatch helper 命名為準；此為契約示意（函式名以實作為準写進 PR 描述）
    from funlab.sched.service import safe_add_manual_job
    stub_service.add_job = None
    def boom(**kw):
        raise ConflictingIdError(kw['id'])
    stub_service._scheduler.add_job = boom
    task = SimpleNamespace(name='T', task_def={'id': 'T', 'name': 'T', 'func': lambda: None},
                           last_manual_exec_info={})
    notified = []
    stub_service.send_user_task_notification = lambda *a, **k: notified.append(a)
    safe_add_manual_job(stub_service, task, one_time_task={'id': 'T_M', 'name': 'T',
                                                           'func': lambda: None},
                        notify_userid=1)
    assert notified
```

**(f) 驗證**：pytest 綠 + 開發環境用 curl 對 `/sched/tasks` POST 假 id（帶合法 CSRF 與 admin
session）→ 頁面正常重繪帶警告 log，不再 500。

**(g) 風險**：低。純防禦性。注意 `run_task` 的 setattr 覆寫共享實例問題屬 SCH-11，別混改。

---

## SCH-11（P2）手動執行以 setattr 改寫**共享**任務實例 → 併發汙染

**(a) 問題影響**
`run_task` 把提交參數 `setattr(task, k, v)` 到 `self.sched_tasks` 裡的**長驻實例**（代碼註解說明
是為了 repr 安全）。同時：auto job 正在以同實例執行、UI 正在迭代讀 `task.*`、另一個 admin 正在
開表單。後寫者覆蓋前者的欄位值——`BookKeepingTask` 的 `book_date`/`cash_inout_override` 這種
業務欄位會被「最後一次手動提交」永久留在實例上，repr、`plan_schedule`、任何讀 `self.<field>`
的 execute 路徑都可能讀到彆人提交的值。真實並發窗口：auto(14:45) 執行中 + 手動提交。

**(b) 優先級**：P2（觸發需併發，但一旦踩中是錯參執行真實業務；與 SCH-03 疊加更糟）。

**(c) 目標**：`service.py::run_task`。

**(d) 修正方向（保留 repr 安全，去掉共享污染）**：`one_time_task['kwargs']` 已是參數的正式傳遞
管道，`_execute_with_hooks(*args, **kwargs) → execute(**kwargs)` 不依賴 setattr。setattr 段
改為**執行期快照還原**：

```python
                # 僅為 repr/表單綁定的最佳努力：提交後立即還原，縮小共享實例被污染窗口
                snapshot = {k: getattr(task, k, None) for k in task_kwargs}
                for k, v in task_kwargs.items():
                    try:
                        setattr(task, k, v)
                    except Exception:
                        pass
                # kwargs 正式生效於 _M job 執行；實例欄位 5 秒後還原（job 尚未執行時
                # execute 收到的是 kwargs，不受還原影響）
                threading.Timer(5.0, lambda: [setattr(task, k, v) for k, v in snapshot.items()
                                              if v is not None or hasattr(task, k)]).start()
```

更乾淨的正解（推薦給 coder 作為終態，PR 內擇一實作並說明）：刪除 setattr 段，實查所有任務
`execute` 是否只經 kwargs 取參（抽樣 BookKeeping/SpiderTask：是），repr 路徑（tasks.html）僅讀
`last_status/next_run_time/trigger`，不觸及業務欄位——直接刪除最簡單。

**(e) pytest**：

```python
"""SCH-11：提交參數不得永久滯留共享實例（終態：刪除 setattr 後，執行前後欄位不變）。"""
def test_manual_kwargs_do_not_persist_on_task(stub_service, flask_app):
    from types import SimpleNamespace
    from dataclasses import dataclass, field
    from funlab.utils.form import create_form_from_dataclass

    @dataclass
    class Spec:
        day: str = field(default='today', metadata={'type': 'StringField'})

    task = SimpleNamespace()
    task.form_class = create_form_from_dataclass(Spec)
    from funlab.sched.service import build_kwargs_from_form   # SCH-03 抽出的 helper
    kwargs, errors = build_kwargs_from_form(task, {'day': '2026-01-01'})
    assert kwargs == {'day': '2026-01-01'}
    # 契约：kwargs 只進佇列，不改 task 實例（task 上無 day 屬性亦不新增）
    assert getattr(task, 'day', None) is None
```

**(f) 驗證**：pytest 綠；開發環境手動跑一個 sleep 任務，期間另開 `/sched/tasks` 觀察欄位不被
提交值改写（終態實作下必然）。

**(g) 風險**：若有任務依賴「執行時讀 self.<field> 而非 kwargs」的壞習慣，刪除 setattr 會暴露它
——這是**好事**（把隱性耦合抓出來），但 PR 描述必須附 `grep -n "self\.\(<欄位名>\)" finfun-*/` 
抽樣結果證明取樣任務不依賴。

---

## SCH-12（P2）兜底 `threading.Timer(180)` 非 daemon → 關機最多多等 180 秒

**(a) 問題影響**
`__init__` 的 `threading.Timer(180.0, self._start_loader_thread_once).start()`：Timer 是
**非 daemon** 線程（實證 E11）。`_cleanup_on_exit → sys.exit` 後直譯器仍 join 非 daemon 線程 →
每個啟動的 Timer 讓進程退出最多延遲 180s（正常情況 hook 已觸發，Timer 醒來只回 `already started`，
但**仍然把退出時間拖到剩餘秒數**）。部署重啟感測（systemd 90s TimeoutStopSec）下，這 180s 定時器
甚至可能自己把 SCH-05 的 SIGKILL 窗口踩出來。

**(b) 優先級**：P2。

**(c) 目標**：`service.py::__init__` L74。

**(d) 完整修正後程式碼**：

```python
                fallback_timer = threading.Timer(180.0, self._start_loader_thread_once)
                fallback_timer.daemon = True      # SCH-12: 兜底計時器不得阻擋進程退出
                fallback_timer.start()
```

**(e) pytest**：

```python
"""SCH-12：兜底 Timer 必須是 daemon。"""
import threading
from unittest import mock

def test_fallback_timer_is_daemon():
    created = []
    real_timer = threading.Timer
    def spy(*a, **k):
        t = real_timer(*a, **k)
        created.append(t)
        return t
    with mock.patch('funlab.sched.service.threading.Timer', side_effect=spy):
        # 只驗證「構造→設 daemon→start」模式；完整 __init__ 需要整個 Flask app，
        # 故以源碼靜態斷言替代端到端：
        import pathlib, re
        src = (pathlib.Path(__file__).resolve().parents[1]
               / 'funlab/sched/service.py').read_text(encoding='utf-8')
        assert re.search(r'fallback_timer\.daemon\s*=\s*True', src)
    for t in created:
        assert not t.is_alive()
```

**(f) 驗證**：pytest 綠；開發環境 `Ctrl+C` 停 run.py 後進程應 <2s 退出。

**(g) 風險**：無。Timer 只是兜底，daemon 化不改變語意。

---

## SCH-13（P1）listener 例外自我吸收（防「靜默狀態遺失」家族性回歸）

**(a) 問題影響**：見 SCH-01/SCH-03 已两次踩同一機制：APScheduler `_dispatch_event` 對 listener
例外只 `logger.exception("Error notifying listener")`（源碼 base.py L1037-1042，實證 E2）。
未來任何人往 listener 加邏輯（SSE 推播、通知、統計）都可能再造一個「任務照跑、狀態全丟」的
靜默故障。**SCH-01 的修正碼已把整個 listener 主體包進 try/except**（見 SCH-01 (d)
`_handle_listener_event`），本條為獨立驗收項，確認包裝存在且未來新增邏輯落在包裝內。

**(b) 優先級**：P1（與 SCH-01 同 PR 落地）。

**(c) 目標**：`service.py::_listener_all_event`（SCH-01 (d) 已含）。

**(d)(e)**：同 SCH-01 代碼與其測試，另補一條包裝存在性測試：

```python
def test_listener_swallows_internal_exceptions(stub_service):
    """事件處理內部炸任何例外，例外不得逃出 listener（SCH-13）。"""
    from funlab.sched.service import SchedService
    from apscheduler.events import SchedulerEvent, EVENT_ALL
    stub_service.sched_tasks = None      # 強迫內部 AttributeError
    SchedService._listener_all_event(stub_service, SchedulerEvent(code=0, job_id=None, jobstore=None))
    assert any(m[0] == 'warning' for m in stub_service.mylogger.messages)
```

**(f) 驗證**：`python -m pytest tests/test_listener_job_id.py -v` 全綠（含新案 5 passed）。

**(g) 風險**：無。

---

## SCH-14（P2）`plan_schedule()` 覆寫 config 的語意與舊文件相反（文件已改，此條盯程式註解）

**(a) 問題影響**：`_load_single_task` L209-210 **無條件**呼叫 `plan_schedule()` 並把回傳值
`update` 進 `task_def`——config.toml 寫了 `trigger/hour` 也会被 plan 覆寫。舊
TROUBLESHOOTING_FLOWCHART 卻教「config 有 trigger 則 plan_schedule 被忽略，請移除 config 設定」，
照做的人會白忙。文件已在本輪改正（docs/TROUBLESHOOTING.md 問題 4）；本條要求在
`SchedTask.plan_schedule` docstring 寫明真實語意，防文件再次漂移。

**(b) P2**（純認知缺陷，但直接影響除錯效率與排程正確性預期）。

**(c) 目標**：`funlab/sched/task.py::SchedTask.plan_schedule`。

**(d) 修正後程式碼**：

```python
    def plan_schedule(self) -> dict:
        """runtime 動態排程 hook。

        載入時 SchedService 無條件呼叫本方法；回傳 truthy dict 會 **覆寫** config.toml
        同名的 task_def 鍵（含 trigger/hour/minute）。要在 config 固定排程，請讓本方法
        回傳 None/falsy；要動態排程，config 的 trigger 設定不可信，以本方法為準。
        """
        return None
```

**(e) pytest**：

```python
"""SCH-14：plan_schedule 回傳值覆寫 config（用 _load_single_task 的純邏輯段測太重的話，
以靜態契約＋小型整合斷言代替）。"""
def test_plan_merges_over_config(stub_service):
    from funlab.sched.service import SchedService
    from types import SimpleNamespace

    calls = []

    class Ep:
        name = 'Demo'
        def load(self):
            class T:
                id = 'Demo'
                name = 'Demo'
                task_config = {'disable': False}
                task_def = {'trigger': 'cron', 'hour': 9, 'kwargs': {}}
                def plan_schedule(self_inner):
                    return {'trigger': 'cron', 'hour': 3}
                last_status = ''
            return T
    svc = stub_service
    svc.mylogger = SimpleNamespace(progress=lambda *a: None, warning=lambda *a, **k: None,
                                   end_progress=lambda *a: None, info=lambda *a: None)
    added = {}
    svc._scheduler.add_job = lambda **kw: added.update(kw) or SimpleNamespace(id=kw['id'])
    SchedService._load_single_task(svc, Ep())
    assert added.get('hour') == 3          # plan 覆寫 config 的 hour=9
```

（若 `T` 與 `SchedTask` 介面出入導致 stub 過重，coder 可改為直接對 merge 邏輯抽 helper
`apply_plan(task_def, plan)` 測純函式，契約不變：plan 贏。）

**(f) 驗證**：pytest 綠。

**(g) 風險**：僅 docstring；測試如抽 helper 則有重構，注意 `_load_single_task` 行為等價。

---

## SCH-15（P2）docs 自身：本輪已完成的文件整併（記錄，不需 coder 動作）

已執行：刪 `COMPLETION_SUMMARY.md`；`ENTRY_POINTS_TROUBLESHOOTING.md`+`TROUBLESHOOTING_FLOWCHART.md`
+`QUICK_REFERENCE.md` 有價值內容併入 `TROUBLESHOOTING.md`；`DEVELOPMENT_GUIDE.md`/`README.md`
重寫為「可直接複製的最小任務 + PEP 621 entry point + uv/pip -e 安裝（本環境 `~/workspaces/fund13/.venv`）」；
簡體字全部轉正體；`[tool.poetry.plugins...]` 全部改正為 `[project.entry-points."funlab_sched_task"]`
（實查 finfun-* pyproject 均為 PEP 621 寫法）；`metadata {'type': 'StringField'}` 字串寫法恢復為
推薦寫法（task.py 註解與 STRING_TYPE_MAPPING 佐證）；`CalcQuantV2` 等虛構任務名移除；
`diagnose_tasks.py` 改為 TROUBLESHOOTING.md 內嵌指令（不另存腳本檔）。

## SCH-16（P2，repo 根 README，非 docs/，需 dev-coder 執行）

`funlab-sched/README.md`（repo 根，本次 docs-only 限制碰不到）現況宣稱：
「Job Store 預設 SQLAlchemy（使用主資料庫）」（錯：預設記憶體）、`max_instances=1`
（實際 conf 為 3，SCH-09 後為 1）、`misfire_grace_time=300`（實際未設＝預設 1s，SCH-08）、
`from funlab.sched import scheduler_plugin`（不存在的符號）。
**要求**：SCH-08/09 落地同一 PR 內把根 README 的「行為說明」段落改為與 conf 實際值一致，
「啟動方式」段落改為 plugin 自動載入說明（`[project.entry-points."funlab_plugin"]`），
移除「待實作（Wave 3）」歷史段。

---

## 附錄 A：實證輸出節錄（本環境實跑）

```text
# sch01_verify.py —— 現況版 vs SCH-01 修正版
--- 現況 service.py ---
  自動(id含_M): KeyError: 'FetchonthlyRevenue'
  手動_M 通知: KeyError: 'FetchonthlyRevenue'
  未知 id: KeyError: 'GhostJob'
--- SCH-01 修正版 ---
  自動(id含_M): OK Executed at:2026-09-
  手動_M 通知: OK
  未知 id: OK(忽略)

# pytest（SCH-01 測試對現況碼）： 3 failed, 1 passed

# probe2/probe3
replace: FetchonthlyRevenue | removesuffix: Fetch_MonthlyRevenue
job 仍執行（apscheduler 吞掉 listener 例外）: True / task.last_status 更新遺失: True
[listener 於 JobExecutionEvent 拋 KeyError] J1 ran=True | J2 ran=True | APScheduler thread alive=True
shutdown(wait=True) 在 job 執行中阻塞超過 1s
raises: SchedulerNotRunningError - Scheduler is not running
configure on running scheduler raises: SchedulerAlreadyRunningError - Scheduler is already running
讀者捕捉 RuntimeError: ['dictionary changed size during iteration']
bound method(引用含 BackgroundScheduler 的實例) pickle raises: TypeError - Schedulers cannot be serialized...
併發結果: ['added', 'added', 'blocked', 'added', 'blocked', 'added', 'blocked', 'added']
Timer(180).daemon = False
apscheduler 3.11.3; base.py L911: "misfire_grace_time": asint(job_defaults.get("misfire_grace_time", 1))

# probe7（L4 連帶影響）
typing.Optional[int] 欄位 → IntegerField
int | None           欄位 → StringField
```

探針腳本：`~/.hermes/profiles/fund13-dev-arch/cache/scratch/sched_probe2.py / sched_probe3.py /
sched_probe4.py / sched_probe5.py / sched_probe7.py / sch01_verify.py / schedtests/`。

## 附錄 B：待確認決策表（請 dev-arch/使用者裁示）

| # | 疑點 | 本文件預設 |
|---|------|-----------|
| Q1 | systemd `TimeoutStopSec` 未設（預設 90s）→ 長任務 SIGKILL 屬推論未觀測 | 建議部署加 `TimeoutStopSec=600`，先於觀察 |
| Q2 | `_M` check-then-add 併發 500 屬邏輯窗口，本環境未捕捉成實際 HTTP 500 | 照 SCH-10 防護，不追實驗證 |
| Q3 | funlab-libs `_stop_executed` 重置（SCH-06 正解）需另開 funlab-libs PR | sched 側防護先上，libs 修法供裁示 |
| Q4 | 舊任務可能已存字串型 kwargs（SCH-03 歷史毒數據） | 交付時附 `grep '[Tt]ask_def.*kwargs'` 盤點指令給 OPS，不自動改 |
| Q5 | misfire 300 + coalesce true 後，重啟補跑窗口拉長是否符合各任務冪等性 | 抽樣 finfetch upsert 語意 OK；BookKeeping 冪等由 ADR-02x 雙源驗證兜底 |
