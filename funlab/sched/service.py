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
            # Start task loader after plugin registration is effectively complete
            # (PluginManagerView hook), to avoid import races on finfun.core.entity.
            if hasattr(self.app, 'hook_manager'):
                self.app.hook_manager.register_hook(
                    'plugin_after_init',
                    self._hook_start_loader_after_fundmgr,
                    priority=5,
                    plugin_name=self.name,
                )
                # Fallback: if expected hooks are missed, still start much later.
                threading.Timer(180.0, self._start_loader_thread_once).start()
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
        plugin_name = context.get('plugin_name')
        if plugin_name in {'pluginmanager', 'PluginManagerView'}:
            self._start_loader_thread_once()

    def _run_task_loading(self):
        """Discover + load tasks, then start APScheduler (sync or background thread)."""
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._load_tasks()
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

                if (next_plan := task.plan_schedule()):
                    task.task_def.update(next_plan)

                if disabled:
                    self.mylogger.end_progress(f"Skipped task {ep.name}: disabled in config")
                    return

                # If no trigger is provided (from plan_schedule or config), do not
                # add an APScheduler job. Still keep the task object loaded so
                # it is available for manual execution via the UI/API.
                if not task.task_def.get('trigger'):
                    task.last_status = 'Loaded (manual-only)'
                    self.sched_tasks[task.id] = task
                    self.mylogger.end_progress(f"Loaded task {ep.name}: registered as manual-only (no trigger)")
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
            self.sched_tasks.pop(old_task.id, None)
        if new_task and new_job:
            new_task.id = new_job.id
            setattr(new_task, "job", new_job)
            self.sched_tasks[new_task.id] = new_task

    def _listener_all_event(self, event):
        """
        keep tracing of job execution
        """
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
            scheduled_run_time = datetime.now()  # log as completed time, not event.scheduled_run_time.strftime("%y-%m-%d %H:%M:%S")
            retval = event.retval
            task = self.sched_tasks[event.job_id.replace('_M', '')]
            summit_userid = task.last_manual_exec_info.get('summit_userid', None)
            is_manual = task.last_manual_exec_info.get('is_manual', False)
            if is_manual:
                self.send_user_task_notification(task.name, message=message, target_userid=summit_userid)
                task.last_manual_exec_info.update({
                    'result_status': event_type,
                    'result_time': datetime.now().isoformat(timespec='seconds'),
                    'exception': str(exception) if exception else '',
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
            elif task:=self.sched_tasks.get(event.job_id.replace('_M', ''), None):  # _M is run manually, one time task
                kwargs = task.last_manual_exec_info.get('kwargs', None)
                args = task.last_manual_exec_info.get('args', None)

            if task:  # Only update status when the task still exists.
                task.last_status = (f"{event_type} at:{scheduled_run_time}") \
                                    + (f", kwargs={kwargs}" if (kwargs) else "") \
                                    + (f", args={args}" if (args) else "") \
                                    + (f", ret={retval}" if retval is not None else "") \
                                    + (f", exception: {exception}" if exception else "")

                if event.job_id.endswith('_M'):
                    task.last_manual_exec_info.update({
                        'result_status': event_type,
                        'result_time': datetime.now().isoformat(timespec='seconds'),
                        'exception': str(exception) if exception else '',
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

                submitted_form = task.form_class(request.form)
                if not submitted_form.validate():
                    self.mylogger.warning(
                        f"Task {task.name} form validation failed: {submitted_form.errors}"
                    )
                    self.send_user_task_notification(
                        task.name,
                        f"任務參數驗證失敗: {submitted_form.errors}",
                        target_userid=current_user.id
                    )
                    return

                task_kwargs = {}
                for field in fields(task):
                    # Safely access the data attribute of bound fields
                    field_instance = getattr(submitted_form, field.name, None)
                    if field_instance and hasattr(field_instance, 'data'):
                        arg_value = field_instance.data
                    else:
                        arg_value = None
                    task_kwargs.update({field.name: arg_value})

                # Apply submitted args to the task instance so dataclass __repr__ and
                # bound-method representations won't fail when they access fields
                # (e.g. repr(bound_method) can include repr(self)).
                for k, v in task_kwargs.items():
                    try:
                        setattr(task, k, v)
                    except Exception:
                        # Ignore if attribute cannot be set; we only attempt best-effort
                        pass

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
                self._scheduler.add_job(**one_time_task)
                self.mylogger.info(
                    f"Task {task.name} manually queued: id={manual_job_id}, run_at={run_at}, "
                    f"func={getattr(task.task_def.get('func'), '__qualname__', task.task_def.get('func'))}, kwargs={task_kwargs}"
                )

            def save_as_default_args(task:SchedTask):
                task_kwargs = {}
                for field in fields(task):
                    if arg_value := request.form.get(field.name, None):
                        task_kwargs.update({field.name: arg_value})
                job = self._scheduler.get_job(task.id)
                if job:
                    job.modify(
                        kwargs=task_kwargs,
                    )
                task.task_def.update({"kwargs": task_kwargs})

            submitted_task_id=request.form.get('id')
            if 'run_task' in request.form:
                run_task(self.sched_tasks[submitted_task_id])
                # Originally this only closed the dialog without refreshing the page.
                # return make_response('', 204)  # No Content
            elif 'save_args' in request.form:
                save_as_default_args(self.sched_tasks[submitted_task_id])
                # Originally this only closed the dialog without refreshing the page.
                # return make_response('', 204)  # No Content
            tasks = []
            forms = {}
            for task in self.sched_tasks.values():
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
        """Start the APScheduler background scheduler."""
        self._scheduler.start(paused=False)

    def _on_stop(self):
        """Shut down the APScheduler (waits for running jobs to finish)."""
        self._scheduler.shutdown(wait=True)

    def _on_reload(self):
        """Reload scheduler configuration and tasks.

        Called by Plugin.reload() after stop() and before start().
        """
        super()._on_reload()
        # Ensure any in-progress loader work is complete before rebuilding jobs.
        self._tasks_loaded.wait()
        self._tasks_loaded.clear()
        self._load_config()
        self._load_tasks()   # synchronous during manual reload
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

