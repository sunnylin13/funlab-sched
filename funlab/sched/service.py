
import logging
import threading
import time
from dataclasses import fields
from datetime import datetime, timedelta
from funlab.core.enhanced_plugin import EnhancedServicePlugin
from funlab.flaskr.app import FunlabFlask
from wtforms import HiddenField
from apscheduler.events import (EVENT_ALL, EVENT_JOB_ADDED, EVENT_JOB_MODIFIED,
                                EVENT_JOB_EXECUTED, EVENT_JOB_ERROR,
                                EVENT_JOB_REMOVED, EVENT_SCHEDULER_PAUSED,
                                EVENT_SCHEDULER_RESUMED,
                                EVENT_SCHEDULER_SHUTDOWN, JobEvent,
                                JobExecutionEvent, JobSubmissionEvent,
                                SchedulerEvent)
from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from flask import make_response, render_template, request
from flask_login import login_required, current_user
from funlab.core.plugin_manager import load_plugins
from funlab.core.menu import MenuItem
from funlab.sched.task import SchedTask
from funlab.utils import log

class SchedService(EnhancedServicePlugin):
    # Declare optional module-level dependencies so plugin_manager can warn
    # instead of crashing when these are missing.
    __plugin_module_deps__: list[str] = []            # hard requirements (module must exist)
    __plugin_optional_module_deps__: list[str] = [    # soft requirements (nice to have)
        'funlab.sse.model',       # from funlab-sse; enables SSE job notifications
    ]
    __plugin_dependencies__: list[str] = []           # plugin-name dependencies (loaded first)

    def __init__(self, app:FunlabFlask, trace_job_status=True):
        super().__init__(app)
        self._task_lock = threading.Lock()
        self._scheduler = BackgroundScheduler()
        self.sched_tasks: dict[str, SchedTask] = {}
        self._load_config()
        self._load_tasks()
        self.start()
        self.register_routes()
        if trace_job_status:
            self._scheduler.add_listener(self._listener_all_event, EVENT_ALL)
            # self._scheduler.add_listener(self._listener_job_start, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)  # Add this line
            # self._scheduler.add_listener(self._listener_job_removed, EVENT_JOB_REMOVED)  # Add this line
        if self.plugin_config.get('HOOK_EXAMPLES', False):
            self._register_hook_examples()

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
                href=f'/{self.name}/tasks', admin_only=True)
        self.app.append_adminmenu(mi)


    def send_user_task_notification(self, task_name: str, message: str, target_userid: int=None):
        title = f"Task {task_name}執行通知"
        self.app.send_user_notification(title, message, target_userid=target_userid)

    def _load_config(self):
        self._scheduler.configure(**self.plugin_config.as_dict())

    def _load_tasks(self):
        """
        Load the job from the plugin class.
        """
        tasks = load_plugins(group="funlab_sched_task")
        no_leading_logger=log.get_logger('', fmt=log.LogFmtType.EMPTY, level=logging.INFO)
        no_leading_logger.info('')  # default plugin loading info without newline, so to let subtask log.info to start from new line
        for task in tasks.values():
            self.mylogger.info(f"Loading task {task} ...", end='')
            task: SchedTask = task(self)
            if (next_plan:=task.plan_schedule()):
                task.task_def.update(next_plan)
            job: Job = self._scheduler.get_job(task.id)
            if not job:
                job = self._scheduler.add_job(**task.task_def)
                self._align_task_job(None, task, job)
            else:
                if task.task_def.get("replace_existing", False):
                    job.modify(**task.task_def)
            no_leading_logger.info('Done')

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

            if event.exception:
                event_type = 'Failed'
                exception = event.exception
                message = f"失敗:{exception}"
            else:
                event_type = 'Executed'
                message = f"完成:{datetime.now().isoformat(timespec='seconds')}"
            scheduled_run_time = datetime.now()  # log as completed time, not event.scheduled_run_time.strftime("%y-%m-%d %H:%M:%S")
            retval = event.retval
            task = self.sched_tasks[event.job_id.replace('_M', '')]
            summit_userid = task.last_manual_exec_info.get('summit_userid', None)
            is_manual = task.last_manual_exec_info.get('is_manual', False)
            if is_manual:
                self.send_user_task_notification(task.name, message=message, target_userid=summit_userid)

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
                if job:  # ✅ 防止 job 為 None
                    kwargs = job.kwargs
                    args = job.args
            elif task:=self.sched_tasks.get(event.job_id.replace('_M', ''), None):  # _M is run manually, one time task
                kwargs = task.last_manual_exec_info.get('kwargs', None)
                args = task.last_manual_exec_info.get('args', None)

            if task:  # ✅ 確保 task 存在才更新狀態
                task.last_status = (f"{event_type} at:{scheduled_run_time}") \
                                    + (f", kwargs={kwargs}" if (kwargs) else "") \
                                    + (f", args={args}" if (args) else "") \
                                    + (f", ret={retval}" if retval is not None else "") \
                                    + (f", exception: {exception}" if exception else "")

                self.mylogger.info(f"Task {event.job_id} {task.last_status}")


    # 以下目的是為檢查job是否有長時間執行, thread dead的問題, 應將其stop thread, , 而不是remove job
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
        @self.blueprint.route("/tasks", methods=["GET", "POST"])
        @login_required
        def tasks():
            def run_task(task:SchedTask):
                submitted_form = task.form_class(request.form)
                task_kwargs = {}
                for field in fields(task):
                    if (field.metadata.get('type', None)!=HiddenField):
                        arg_value = getattr(submitted_form, field.name, None).data
                        task_kwargs.update({field.name: arg_value})

                # ✅ 檢查是否已有手動執行的 job 在進行中
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
                one_time_task = {'id': manual_job_id, 'name': task.task_def['name'], 'func': task.task_def['func'],
                    "kwargs": task_kwargs, 'trigger':'date', "run_date": datetime.now() + timedelta(seconds=2)}
                task.last_manual_exec_info = one_time_task.copy()
                task.last_manual_exec_info.update({'summit_userid': current_user.id, 'is_manual': True})
                self._scheduler.add_job(**one_time_task)

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
                # 原本用於只close dialog不更新網頁, 有其它問題先不這樣處理
                # return make_response('', 204)  # No Content
            elif 'save_args' in request.form:
                save_as_default_args(self.sched_tasks[submitted_task_id])
                # 原本用於只close dialog不更新網頁, 有其它問題先不這樣處理
                # return make_response('', 204)  # No Content
            tasks = []
            forms = {}
            for task in self.sched_tasks.values():
                tasks.append(task)
                form = request.form if (request.form and task.id in request.form) else None
                forms[task.id] = task.form_class(formdata=form)  # 這裡必需將同request.form時的值填入form, 其它則是None, 若全部都是None, 則會造成id, name都是相同request.form的值, 致submit時錯誤, 原因尚未找到
            return render_template("tasks.html", tasks=tasks, forms=forms)

    @property
    def running(self):
        """Get true whether the scheduler is running."""
        return self._scheduler.running

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

    def reload(self):
        """Reload scheduler configuration and tasks, then restart."""
        self.stop()
        self._load_config()
        self._load_tasks()
        self.start()

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

