from __future__ import annotations

import logging
import asyncio
import threading
import time
from dataclasses import fields
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from funlab.core.plugin import ServicePlugin
from apscheduler.events import (EVENT_ALL, EVENT_JOB_ADDED, EVENT_JOB_MODIFIED,
                                EVENT_JOB_EXECUTED, EVENT_JOB_ERROR,
                                EVENT_JOB_MISSED, EVENT_JOB_REMOVED, EVENT_SCHEDULER_PAUSED,
                                EVENT_SCHEDULER_RESUMED,
                                EVENT_SCHEDULER_SHUTDOWN, JobEvent,
                                JobExecutionEvent, JobSubmissionEvent,
                                SchedulerEvent)
from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from importlib.metadata import entry_points as _task_entry_points
from funlab.core.menu import MenuItem
from funlab.core.auth import policy_required
from funlab.core.policy import is_admin
from funlab.utils import log

if TYPE_CHECKING:
    from funlab.flaskr.app import FunlabFlask
    from funlab.sched.task import SchedTask


def build_kwargs_from_form(task: 'SchedTask', formdata) -> tuple[dict | None, dict]:
    """SCH-03：以任務自己的 form_class 驗證 formdata 並轉出正確型別的 kwargs。

    回傳 (kwargs, errors)：驗證失敗時 kwargs 為 None。
    跳過非資料欄位（CSRF/id/name 由 task_def 自行管理）。
    formdata 允許傳純 dict（測試用）：正規化為 MultiDict 以滿足 wtforms 介面。
    """
    if formdata is not None and not hasattr(formdata, 'getlist'):
        from werkzeug.datastructures import MultiDict
        formdata = MultiDict(formdata)
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


def plain_exception_for(task, exception) -> str:
    """t_b6d1f78d P3：任務若有 humanize_exception 鉤子，取其白話一句。

    契約（fund13/handoff）：任務端 ``humanize_exception(exc) -> str | None``；
    回 None／無鉤子／鉤子自身拋例外 → 空字串（呈現層回落原始 exception 文本，
    既有行為零變動；SCH-13 自我吸收紀律）。
    """
    if exception is None:
        return ''
    try:
        fn = getattr(task, 'humanize_exception', None)
        if fn is None:
            return ''
        return str(fn(exception) or '')
    except Exception:
        return ''


# SCH-07: 多進程 WSGI 部署器名單——每個 worker 各起一份排程器，任務會被重複執行。
_MULTI_PROCESS_WSGI = {'gunicorn', 'uwsgi'}


def apply_plan(task_def: dict, plan: dict | None) -> dict:
    """SCH-14：plan_schedule() 回傳值**覆寫** config 的 task_def 同名鍵（就地 update）。

    語意契約：plan 贏——config.toml 寫了 trigger/hour 也会被 plan 覆寫；
    要在 config 固定排程，plan_schedule 必須回傳 falsy。
    """
    if plan:
        task_def.update(plan)
    return task_def


def safe_add_manual_job(service, task, one_time_task: dict, notify_userid):
    """SCH-10：手動執行 add_job 的競態防護。

    兩個請求可同時通過 get_job 檢查再同時 add_job → ConflictingIdError → 500。
    此處捕獲衝突：記警告＋通知提交者「已在佇列中」，不讓 500 炸到頁面。
    """
    from apscheduler.jobstores.base import ConflictingIdError
    try:
        service._scheduler.add_job(**one_time_task)
        return True
    except ConflictingIdError:
        service.mylogger.warning(f"Task {task.name} 併發重複提交被拒")
        service.send_user_task_notification(
            task.name, "任務已在佇列中（併發提交），略過此次",
            target_userid=notify_userid)
        return False


class SchedService(ServicePlugin):
    # Declare optional module-level dependencies so plugin_manager can warn
    # instead of crashing when these are missing.
    # __plugin_module_deps__: list[str] = []            # hard requirements (module must exist)
    # __plugin_optional_module_deps__: list[str] = [    # soft requirements (nice to have)
    #     'funlab.sse.model',       # from funlab-sse; enables SSE job notifications
    # ]
    # __plugin_dependencies__: list[str] = []           # plugin-name dependencies (loaded first)

    def __init__(self, app:FunlabFlask, trace_job_status=True):
        super().__init__(app)
        self._task_lock = threading.Lock()
        self._scheduler = BackgroundScheduler()
        self.sched_tasks: dict[str, SchedTask] = {}
        self._task_build: dict[str, 'SchedTask'] | None = None   # SCH-04: loader 專屬建構中副本
        self._loader_started = False
        self._background_task_loading = True
        # Set when all tasks are registered and the APScheduler thread is running.
        # Other code that needs tasks-ready can call self._tasks_loaded.wait().
        self._tasks_loaded = threading.Event()
        self._load_config()
        if trace_job_status:
            self._scheduler.add_listener(self._listener_all_event, EVENT_ALL)
            # self._scheduler.add_listener(self._listener_job_start, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
            # self._scheduler.add_listener(self._listener_job_removed, EVENT_JOB_REMOVED)
        self._loader_thread = None
        if self._background_task_loading:
            # Load tasks in a background thread so SchedService.__init__ returns quickly,
            # allowing other plugins to continue initialising while heavy imports happen
            # concurrently.
            self._loader_thread = threading.Thread(
                target=self._run_task_loading,
                name='sched-task-loader',
                daemon=True,
            )
            # Start task loader after plugin registration is confirmed complete
            # (R10: 明確 hook plugins_registration_complete，取代對
            # PluginManagerView plugin_name 的字串匹配脆弱握手；見
            # funlab-libs docs/PLUGIN_LIFECYCLE.md §2 矩陣)，to avoid import
            # races on finfun.core.entity.
            if hasattr(self.app, 'hook_manager'):
                self.app.hook_manager.register_hook(
                    'plugins_registration_complete',
                    self._hook_start_loader_after_fundmgr,
                    priority=5,
                    plugin_name=self.name,
                )
                # Fallback: if expected hooks are missed, still start much later.
                # SCH-12: Timer 必須 daemon——非 daemon Timer 讓進程退出最多延遲 180s
                # （實證 E11），甚至踩出 SCH-05 的 systemd SIGKILL 窗口。
                fallback_timer = threading.Timer(180.0, self._start_loader_thread_once)
                fallback_timer.daemon = True
                fallback_timer.start()
            else:
                self._start_loader_thread_once()
        else:
            # Synchronous mode for deterministic startup/diagnostics.
            self._run_task_loading()
        self.register_routes()
        if self.plugin_config.get('HOOK_EXAMPLES', False):
            self._register_hook_examples()

    def _start_loader_thread_once(self):
        with self._task_lock:
            if self._loader_started:
                return
            self._loader_started = True
            if self._loader_thread is None:
                self.mylogger.info('[SchedService] Task loading already completed synchronously')
                return
            self._loader_thread.start()

    def _hook_start_loader_after_fundmgr(self, context):
        # R10：plugins_registration_complete 為一次性全域 hook（全部 plugin
        # 註冊完成後由 FunlabFlask 恰觸發一次），不再比對 plugin_name。
        self._start_loader_thread_once()

    def _run_task_loading(self):
        """Discover + load tasks, then start APScheduler (sync or background thread)."""
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._load_tasks()
            self._publish_tasks()   # SCH-04: 全部載入完成後一次性發佈快照
            self.start()   # _on_start() starts APScheduler with paused=False.
        except Exception as e:
            self.mylogger.error(
                f"[SchedService] Fatal error during task loading: {e}",
                exc_info=True,
            )
        finally:
            if loop is not None:
                loop.close()
            self._tasks_loaded.set()

    def _register_hook_examples(self):
        if not hasattr(self.app, 'hook_manager'):
            return

        self.app.hook_manager.register_hook(
            'task_before_execute',
            self._hook_example_task_before,
            priority=50,
            plugin_name=self.name,
        )
        self.app.hook_manager.register_hook(
            'task_error',
            self._hook_example_task_error,
            priority=50,
            plugin_name=self.name,
        )

    def _hook_example_task_before(self, context):
        task_name = context.get('task_name')
        if task_name:
            self.mylogger.debug(f"Hook example: task_before_execute {task_name}")

    def _hook_example_task_error(self, context):
        task_name = context.get('task_name')
        error = context.get('error')
        if task_name and error:
            self.mylogger.info(f"Hook example: task_error {task_name}: {error}")

    def setup_menus(self):
        super().setup_menus()
        mi = MenuItem(title='Sched Tasks',
                icon='<svg xmlns="http://www.w3.org/2000/svg" class="icon icon-tabler icon-tabler-calendar-stats" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round">\
                        <path stroke="none" d="M0 0h24v24H0z" fill="none"></path>\
                        <path d="M11.795 21h-6.795a2 2 0 0 1 -2 -2v-12a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v4"></path>\
                        <path d="M18 14v4h4"></path>\
                        <path d="M18 18m-4 0a4 4 0 1 0 8 0a4 4 0 1 0 -8 0"></path>\
                        <path d="M15 3v4"></path>\
                        <path d="M7 3v4"></path>\
                        <path d="M3 11h16"></path>\
                        </svg>',
            href=f'/{self.name}/tasks', required_policy=is_admin)
        self.app.append_adminmenu(mi)


    def send_user_task_notification(self, task_name: str, message: str, target_userid: int=None):
        title = f"Task {task_name} 執行通知"
        self.app.send_user_notification(title, message, target_userid=target_userid)

    def _load_config(self):
        self._background_task_loading = self.plugin_config.get('BACKGROUND_TASK_LOADING', True)
        scheduler_config = self.plugin_config.as_dict()
        scheduler_config.pop('BACKGROUND_TASK_LOADING', None)
        self._scheduler.configure(**scheduler_config)

    def _load_tasks(self):
        """
        Discover and load task classes via the ``funlab_sched_task`` entry-point
        group, then register each task with APScheduler.

        Uses importlib.metadata.entry_points directly (no deprecated load_plugins
        wrapper) so each task failure is isolated and does not abort startup.
        Each phase is individually timed so import-chain bottlenecks are clearly
        visible in the log:
          ep.load        module import (first call per package triggers heavy imports)
          __init__       task instantiation (lazy imports often happen here)
          plan_schedule  next-run calculation
          add_job        APScheduler registration
        """
        task_eps = list(_task_entry_points(group="funlab_sched_task"))
        for ep in task_eps:
            self._load_single_task(ep)

    def _stage_task(self, task: 'SchedTask'):
        """SCH-04：loader 執行緒專用——寫入建構中副本，不直接動讀者可見的 dict。"""
        if self._task_build is None:
            self._task_build = dict(self.sched_tasks)
        self._task_build[task.id] = task

    def _unstage_task(self, task_id: str):
        """SCH-04：loader 執行緒專用——在建構中副本上移除。"""
        if self._task_build is None:
            self._task_build = dict(self.sched_tasks)
        self._task_build.pop(task_id, None)

    def _publish_tasks(self):
        """SCH-04：loader 執行緒專用——把建構中副本原子置換出去（dict 名稱綁定是原子操作）。"""
        if self._task_build is not None:
            self.sched_tasks = self._task_build   # 單一賦值，讀者永遠看到完整快照
            self._task_build = None

    def _snapshot_tasks(self) -> list:
        """SCH-04：Web/其他讀端專用——先捕獲引用再迭代，永不與寫者共享同一 dict。"""
        return list(self.sched_tasks.values())

    def _load_single_task(self, ep):
            # Phase 1: class loading (may trigger module-level imports on first call).
            self.mylogger.progress(f"Loading task {ep.name} ...")
            try:
                task_class = ep.load()
            except Exception as e:
                self.mylogger.warning("")  # add return line
                self.mylogger.warning(
                    f"[SchedService] Skipping task '{ep.name}': "
                    f"failed to load class ({type(e).__name__}: {e})"
                )
                self.mylogger.end_progress(f"Failed to load task {ep.name}")
                return
            try:
                # Instantiate task and compute schedule; only track total time.
                task: SchedTask = task_class(self)

                # If task config explicitly disables the task, skip registration.
                disabled = task.task_config.get('disable', False)

                # SCH-14: 經 apply_plan 明確表達「plan 覆寫 config」語意
                apply_plan(task.task_def, task.plan_schedule())

                if disabled:
                    self.mylogger.end_progress(f"Skipped task {ep.name}: disabled in config")
                    return

                # If no trigger is provided (from plan_schedule or config), do not
                # add an APScheduler job. Still keep the task object loaded so
                # it is available for manual execution via the UI/API.
                if not task.task_def.get('trigger'):
                    task.last_status = 'Loaded (manual-only)'
                    self._stage_task(task)   # SCH-04: 寫入建構中副本，不直接動讀者可見的 dict
                    self.mylogger.end_progress(f"Loaded task {ep.name}:(manual-only, no trigger)")
                    return

                # APScheduler registration
                job: Job = self._scheduler.get_job(task.id)
                if not job:
                    job = self._scheduler.add_job(**task.task_def)
                    self._align_task_job(None, task, job)
                else:
                    if task.task_def.get("replace_existing", False):
                        job.modify(**task.task_def)
            except Exception as e:
                self.mylogger.warning("")  # add return line
                self.mylogger.warning(
                    f"[SchedService] Task '{ep.name}' disabled: "
                    f"failed during initialisation: {e}"
                )
            finally:
                self.mylogger.end_progress(f"Task {ep.name} loaded.")

    def _align_task_job(self, old_task:SchedTask, new_task:SchedTask, new_job:Job):
        if old_task:
            self._unstage_task(old_task.id)   # SCH-04
        if new_task and new_job:
            new_task.id = new_job.id
            setattr(new_task, "job", new_job)
            self._stage_task(new_task)        # SCH-04

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
                # t_b6d1f78d P3：任務有 humanize_exception 鉤子且可映射 →
                # 通知以白話一句開頭、原始 exception 附後作明細；否則原樣。
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
                    # t_b6d1f78d P3：任務提供 humanize_exception 鉤子且可映射
                    # → 通知改「白話一句＋原始明細」；無法映射 → 原樣。
                    plain = plain_exception_for(base_task, exception)
                    if plain:
                        message = f"{plain}\n—— 原始明細 ——\n{exception}"
                    self.send_user_task_notification(base_task.name, message=message, target_userid=summit_userid)
                    base_task.last_manual_exec_info.update({
                        'result_status': event_type,
                        'result_time': datetime.now().isoformat(timespec='seconds'),
                        'exception': str(exception) if exception else '',
                        'exception_plain': plain,
                    })

        elif isinstance(event, SchedulerEvent):  # this is apscheduler service event, influence all tasks
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

            if (task:=self.sched_tasks.get(event.job_id, None)):
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
                        # t_b6d1f78d P3：白話一句（可映射時）；無→空字串回落原始
                        'exception_plain': plain_exception_for(task, exception),
                    })

                self.mylogger.info(f"Task {event.job_id} {task.last_status}")


    # The following monitoring helpers were kept for future investigation of
    # long-running jobs or dead worker threads.
    # def _listener_job_start(self, event):
    #     if event.code == EVENT_JOB_EXECUTED:
    #         self.sched_tasks[event.job_id].start_time = datetime.now()

    # def _listener_job_removed(self, event):
    #     if event.code == EVENT_JOB_REMOVED:
    #         self.sched_tasks.pop(event.job_id, None)

    # def monitor_jobs(self):
    #     for job_id, task in self.sched_tasks.items():
    #         start_time = task.start_time
    #         if start_time is None:
    #             runtime = timedelta(0)
    #         else:
    #             runtime = datetime.now() - start_time
    #         if runtime > timedelta(minutes=20):  # Replace with your threshold
    #             self.mylogger.warning(f"Job {task.name}:{job_id} has been running for {runtime} over 20 minutes, removing it.")
    #             self._scheduler.remove_job(job_id)

    def register_routes(self):
        from flask import render_template, request
        from flask_login import current_user
        from wtforms import HiddenField

        @self.blueprint.route("/tasks", methods=["GET", "POST"])
        @policy_required(is_admin)
        def tasks():
            def run_task(task:SchedTask):
                if not self.running:
                    self.mylogger.warning(
                        f"Task {task.name} manual run skipped: scheduler is not running (state={self.state})"
                    )
                    self.send_user_task_notification(
                        task.name,
                        f"排程器尚未啟動（state={self.state}），請稍後再試",
                        target_userid=current_user.id
                    )
                    return

                kwargs, errors = build_kwargs_from_form(task, request.form)
                if kwargs is None:
                    self.mylogger.warning(
                        f"Task {task.name} form validation failed: {errors}"
                    )
                    self.send_user_task_notification(
                        task.name,
                        f"任務參數驗證失敗: {errors}",
                        target_userid=current_user.id
                    )
                    return
                task_kwargs = kwargs   # SCH-03: 與 save 路徑共用同一份驗證+轉型契約

                # SCH-15（ADR-053 D1）：手動入口單點無條件注入 manually=True。
                # 契約（比照 SCH-01「_M 後綴＝手動」）：經 run_task 佇列 *_M
                # job 者即手動——入口語意在服務端釘死、不经表單、不可偽造。
                # 表單 HiddenField 往返鏈（G1 預設丟失→G2 渲染 value=""→
                # G3 原樣直傳）曾使手動跑以 manually=''（falsy＝自動場語意）
                # 執行（t_b378d78c 事故根因 P4）。無條件注入：所有任務
                # execute 簽名皆收 manually=False 或 **kwargs（Q2 已裁），
                # 免維護欄位清單；自動場（cron/plan/save_args）不走本漏斗。
                task_kwargs['manually'] = True

                # SCH-11（終態）: 不再 setattr 覆寫長驻共享實例——kwargs 經
                # one_time_task['kwargs'] → _execute_with_hooks(**kwargs) → execute(**kwargs)
                # 正式傳遞；實例欄位不被「最後一次手動提交」永久污染。
                # 抽樣佐證（PR 描述附 grep）：finfun-fundmgr BookKeeping/Reconcile、
                # finfun-finfetch 各 execute 皆經參數列取値，不讀 self.<業務欄位>。

                # Avoid queueing the same manual-run job more than once.
                manual_job_id = task.task_def['id'] + '_M'
                if self._scheduler.get_job(manual_job_id):
                    self.mylogger.warning(f"Task {task.name} 已在執行中，略過此次請求")
                    self.send_user_task_notification(
                        task.name,
                        "任務已在執行中，請稍後再試",
                        target_userid=current_user.id
                    )
                    return

                # run a one time task, with same name, but different id with '_M' suffix
                run_at = datetime.now(self._scheduler.timezone) + timedelta(seconds=1)
                one_time_task = {'id': manual_job_id, 'name': task.task_def['name'], 'func': task.task_def['func'],
                    "kwargs": task_kwargs,
                    'trigger':'date',
                    "run_date": run_at,
                    "misfire_grace_time": 300,
                    "coalesce": False,
                }
                # Compute a safe name for the queued function without forcing
                # a full stringification of a bound method (which may call
                # the task's __repr__ and access dataclass fields).
                funcobj = task.task_def.get('func')
                qname = None
                if funcobj is not None:
                    qname = getattr(funcobj, '__qualname__', None)
                    if qname is None:
                        # bound method objects expose the underlying function on __func__
                        qname = getattr(getattr(funcobj, '__func__', None), '__qualname__', None)
                    if qname is None:
                        # fallback to a safe repr that avoids calling object's __repr__
                        try:
                            qname = f"{type(funcobj).__name__}:{getattr(funcobj, '__name__', repr(funcobj))}"
                        except Exception:
                            qname = str(type(funcobj))

                task.last_manual_exec_info = one_time_task.copy()
                task.last_manual_exec_info.update({
                    'summit_userid': current_user.id,
                    'is_manual': True,
                    'queued_at': datetime.now(self._scheduler.timezone).isoformat(timespec='seconds'),
                    'queue_func': qname,
                    'result_status': 'Queued',
                    'result_time': '',
                    'exception': '',
                })
                # SCH-10: add_job 競態防護（兩請求同過 get_job 檢查 → ConflictingIdError 不再 500）
                if not safe_add_manual_job(self, task, one_time_task,
                                           notify_userid=current_user.id):
                    return
                self.mylogger.info(
                    f"Task {task.name} manually queued: id={manual_job_id}, run_at={run_at}, "
                    f"func={getattr(task.task_def.get('func'), '__qualname__', task.task_def.get('func'))}, kwargs={task_kwargs}"
                )

            def save_as_default_args(task:SchedTask):
                # SCH-03: 经由 form_class 驗證+轉型；髒字串（如 'false'）不再直達排程任務
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
                # SCH-15（ADR-053 D2）：save 路徑寫入的是**自動 job** 的預設
                # kwargs——框架恆 pop manually（自動場入口語意＝不帶該鍵，
                # 由 execute(manually=False) 簽名預設取得）。現況存 '' 屬髒值
                # （falsy 僥倖正確），且 last_status 顯示誤導（manually: ''）。
                kwargs.pop('manually', None)
                job = self._scheduler.get_job(task.id)
                if job:
                    job.modify(kwargs=kwargs)
                task.task_def.update({"kwargs": kwargs})

            submitted_task_id=request.form.get('id')
            if 'run_task' in request.form:
                target = self.sched_tasks.get(submitted_task_id)   # SCH-04 連帶 SCH-10：未知 id 不再 KeyError→500
                if target is None:
                    self.mylogger.warning(f"Unknown task id in POST: {submitted_task_id!r}")
                else:
                    run_task(target)
                # Originally this only closed the dialog without refreshing the page.
                # return make_response('', 204)  # No Content
            elif 'save_args' in request.form:
                target = self.sched_tasks.get(submitted_task_id)   # SCH-04 連帶 SCH-10
                if target is None:
                    self.mylogger.warning(f"Unknown task id in POST: {submitted_task_id!r}")
                else:
                    save_as_default_args(target)
                # Originally this only closed the dialog without refreshing the page.
                # return make_response('', 204)  # No Content
            tasks = []
            forms = {}
            for task in self._snapshot_tasks():   # SCH-04: 先捕獲引用再迭代
                tasks.append(task)
                form = request.form if (request.form and task.id == submitted_task_id) else None
                forms[task.id] = task.form_class(formdata=form)  # Bind form data only for the submitted task to avoid cross-form contamination.
            return render_template("tasks.html", tasks=tasks, forms=forms)

    @property
    def running(self):
        """Get true whether the scheduler is running."""
        return self._scheduler.running

    @property
    def metrics(self):
        base_metrics = super().metrics
        base_metrics.update({
            'scheduler_running': bool(self.running),
            'tasks_loaded': bool(self._tasks_loaded.is_set()),
            'registered_tasks': len(self.sched_tasks),
            'loader_started': bool(self._loader_started),
        })
        return base_metrics

    @property
    def state(self):
        """Get the state of the scheduler."""
        return self._scheduler.state

    @property
    def scheduler(self):
        return self._scheduler

    @property
    def task(self):
        """Get the base scheduler decorator"""
        return self._scheduler.scheduled_job

    def _on_start(self):
        """Start the APScheduler background scheduler.

        SCH-07: 每個 WSGI worker process 各有一份 SchedService；多進程部署器
        （gunicorn/uwsgi 多 worker）下同一 cron 任務會被重複執行（記憶體 jobstore
        無跨進程去重）。PM 裁示（2026-09-28）正式棧為 waitress 單進程，故多進程
        WSGI 預設 fail-closed 拒跑；明示 ALLOW_MULTI_WORKER_SCHEDULER=true 可放行。
        """
        wsgi = str(self.app.config.get('WSGI', 'flask')).lower()
        allow_multi = bool(self.plugin_config.get('ALLOW_MULTI_WORKER_SCHEDULER', False))
        if wsgi in _MULTI_PROCESS_WSGI and not allow_multi:
            self.mylogger.error(
                f"[SchedService] 偵測到 WSGI={wsgi}：多 worker 會使排程任務重複執行，"
                "排程器已拒絕啟動。請改用單進程 WSGI（waitress，現況正式配置）或設 "
                "[SchedService] ALLOW_MULTI_WORKER_SCHEDULER=true 並自行確保單 worker。"
            )
            return
        self._scheduler.start(paused=False)

    def _on_stop(self):
        """Shut down the APScheduler.

        SCH-05: wait=False——不在此線程無限等 job（plugin._run_stop_safely 只有 5s 額度，
        等待與否不改變「不殺任務」的事實，因為 apscheduler 線程由直譯器 join）。
        真正要「等任務跑完再退」靠關機序：先在 Web 停止提交、APScheduler 線程自然 join；
        並把 systemd TimeoutStopSec 設到大於最長任務時間（部署文件，另由 OPS 執行）。
        從未 start 過（載入失敗路徑）時 shutdown() 會拋 SchedulerNotRunningError（實證 E5），吞掉。
        """
        from apscheduler.schedulers.base import SchedulerNotRunningError
        try:
            self._scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            self.mylogger.info("[SchedService] scheduler was never started; nothing to shut down")

    def _on_reload(self):
        """Reload scheduler configuration and tasks.

        Called by Plugin.reload() after stop() and before start().
        """
        super()._on_reload()
        # Ensure any in-progress loader work is complete before rebuilding jobs.
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
        self._publish_tasks()   # SCH-04: 同步載入完成後一次性發佈快照
        self._tasks_loaded.set()

    def _perform_health_check(self) -> bool:
        return bool(self.running and self._tasks_loaded.is_set())

    def pause(self):
        """
        Pause job processing in the scheduler.
        This will prevent the scheduler from waking up to do job processing until :meth:`resume`
        is called. It will not however stop any already running job processing.
        """
        self._scheduler.pause()

    def resume(self):
        """
        Resume job processing in the scheduler.
        """
        self._scheduler.resume()

