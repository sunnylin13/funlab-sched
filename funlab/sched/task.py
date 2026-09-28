from __future__ import annotations

from abc import ABC, abstractmethod
import datetime
import logging
from time import sleep
from typing import TYPE_CHECKING, Any
from dataclasses import dataclass, field, fields
# wtforms 欄位類別移至 TYPE_CHECKING，避免啟動時載入 wtforms 完整模組。
# field metadata 中改用字串（如 'HiddenField'），
# create_form_from_dataclass 的 STRING_TYPE_MAPPING 會在建立 form 時解析。
# DataRequired 為 validator 實例，必須在類別定義時建立，故保留直接 import。
from wtforms.validators import DataRequired
from funlab.core import _Configuable
from funlab.core.config import Config
from funlab.utils import log

if TYPE_CHECKING:
    from wtforms import HiddenField, StringField
    from funlab.flaskr.app import FunlabFlask
    from funlab.sched.service import SchedService

@dataclass
class SchedTask(_Configuable, ABC):
    id:str = field(init=False, metadata={'type': 'HiddenField'
                                         ,'default':lambda dataclass_type: dataclass_type.__name__.removesuffix('Task')})
    name:str = field(metadata={'type': 'HiddenField'
                               ,'default':lambda dataclass_type: dataclass_type.__name__.removesuffix('Task')})

    @staticmethod
    def form_javascript():
        return ''

    def __init__(self, sched:SchedService, name=None) -> None:
        from funlab.utils.form import create_form_from_dataclass
        self.mylogger = log.get_logger(self.__class__.__name__, level=logging.INFO)
        self.sched = sched
        self.id = self.__class__.__name__.removesuffix('Task') #.lower()
        if name:
            self.name = name
        else:
            self.name = self.__class__.__name__.removesuffix('Task')
        self.last_status=''
        self.last_manual_exec_info = {}  # there are manual and auto execution shared same task. Here to record manual execution info.
        self.start_time = None
        ext_task_config = sched.app.get_section_config(section=self.__class__.__name__,
                                                                default=Config({self.__class__.__name__:{}}),
                                                                keep_section=True)
        self._task_config = self.get_config(file_name='task.toml', ext_config=ext_task_config)  # group_session=sched.__class__.__name__)
        if 'task_def' in self.task_config:
            self._task_def = self.task_config.get('task_def', {})
        else:
            self._task_def = {key:val for key, val in vars(self.task_config).items()
                                if(not key.startswith('_') and not key[0].isupper()) }

        # Store original execute method before wrapping
        self._original_execute = self.execute

        # Update task_def with wrapped execute method
        self._task_def.update(dict(id=self.id, name=self.name, func=self._execute_with_hooks, replace_existing=True))  ## replace_existing 必需一定為true, 當使用jobstroe時

        func_default_kwargs  = self.task_def.get('kwargs', {})
        for key, value in func_default_kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
                setattr(self.__dataclass_fields__[key], 'default', value)

        self.form_class = create_form_from_dataclass(self.__class__)
        if self.task_config.get('HOOK_EXAMPLES', False):
            self._register_hook_examples()

    def _register_hook_examples(self):
        if not hasattr(self.sched.app, 'hook_manager'):
            return

        task_name = self.name

        def _before(context):
            if context.get('task_name') == task_name:
                self.mylogger.debug(f"Hook example: task_before_execute {task_name}")

        def _after(context):
            if context.get('task_name') == task_name:
                self.mylogger.debug(f"Hook example: task_after_execute {task_name}")

        def _error(context):
            if context.get('task_name') == task_name:
                error = context.get('error')
                self.mylogger.info(f"Hook example: task_error {task_name}: {error}")

        self.sched.app.hook_manager.register_hook(
            'task_before_execute',
            _before,
            priority=50,
            plugin_name=task_name,
        )
        self.sched.app.hook_manager.register_hook(
            'task_after_execute',
            _after,
            priority=50,
            plugin_name=task_name,
        )
        self.sched.app.hook_manager.register_hook(
            'task_error',
            _error,
            priority=50,
            plugin_name=task_name,
        )

    def _execute_with_hooks(self, *args, **kwargs):
        """Wrapper around execute() that triggers lifecycle hooks."""
        self.mylogger.info(f"Task {self.name} execution started: args={args}, kwargs={kwargs}")
        self.prepare_runtime()


        # Trigger before_task_execute hook
        if hasattr(self.sched.app, 'hook_manager'):
            self.sched.app.hook_manager.call_hook(
                'task_before_execute',
                task=self,
                task_name=self.name,
                args=args,
                kwargs=kwargs
            )

        try:
            result = self._original_execute(*args, **kwargs)

            # Trigger after_task_execute hook
            if hasattr(self.sched.app, 'hook_manager'):
                self.sched.app.hook_manager.call_hook(
                    'task_after_execute',
                    task=self,
                    task_name=self.name,
                    result=result,
                    args=args,
                    kwargs=kwargs
                )

            self.mylogger.info(f"Task {self.name} execution completed")
            return result
        except Exception as e:
            self.mylogger.error(f"Task {self.name} execution failed: {e}", exc_info=True)
            # Trigger task_error hook
            if hasattr(self.sched.app, 'hook_manager'):
                self.sched.app.hook_manager.call_hook(
                    'task_error',
                    task=self,
                    task_name=self.name,
                    error=e,
                    args=args,
                    kwargs=kwargs
                )
            raise

    def _validate_runtime_executable(self):
        """SCH-08：非 default 執行器（processpool）對綁定方法任務必然 pickle 失敗。

        排入的 func 是 ``task._execute_with_hooks`` 綁定方法（持 task→sched 引用），
        processpool 需序列化 → ``TypeError: Schedulers cannot be serialized``（實證 E8）。
        於每次執行前做一次字串判斷，設定漂移時日誌留下明確警告而非 executor 棧。
        """
        executor = self.task_def.get('executor', 'default')
        if executor and str(executor) != 'default':
            self.mylogger.warning(
                f"[{self.name}] executor='{executor}' 非 threadpool default；"
                "processpool 不支援綁定方法任務（pickle 必掛），請改回 default")

    def prepare_runtime(self):
        """Optional runtime warmup hook; override in subclasses for deferred init."""
        self._validate_runtime_executable()
        return None

    def __getattr__(self, name):
        # delegate apscheduler's Job attribute
        if name in ('trigger', 'executor', 'func', 'func_ref',
                    'args', 'kwargs', 'misfire_grace_time',
                    'coalesce', 'max_instances', 'next_run_time',):
            # Avoid triggering __getattr__/__getattribute__ recursion by
            # accessing the underlying attribute storage directly.
            try:
                job = object.__getattribute__(self, 'job')
            except AttributeError:
                job = None
            return getattr(job, name, None)

        # If this is a dataclass-declared field, return its value/default
        try:
            dataclass_fields = object.__getattribute__(self, '__dataclass_fields__')
        except Exception:
            dataclass_fields = {}

        if name in dataclass_fields:
            # if instance already has the attribute, return it
            try:
                inst_dict = object.__getattribute__(self, '__dict__')
            except Exception:
                inst_dict = {}
            if name in inst_dict:
                return inst_dict[name]

            # return dataclass field default if provided
            fld = dataclass_fields[name]
            from dataclasses import MISSING
            if fld.default is not MISSING:
                return fld.default

            # fallback to metadata 'default' (may be callable or value)
            md_def = fld.metadata.get('default', MISSING)
            if md_def is not MISSING:
                return md_def(self.__class__) if callable(md_def) else md_def

        # Not found: raise AttributeError so normal lookup semantics apply
        raise AttributeError(f"{self.__class__.__name__!r} object has no attribute {name!r}")

    @property
    def task_config(self):
        return self._task_config

    @property
    def task_def(self):
        if self._task_def:
            return self._task_def
        else:
            raise Exception('No configuration set, need to provide as property here.')

    @property
    def kwargs(self):
        return self.task_def.get('kwargs', {})

    def plan_schedule(self)->dict:
        """runtime 動態排程 hook。

        載入時 SchedService **無條件**呼叫本方法（SCH-14，經 service.apply_plan）；
        回傳 truthy dict 會**覆寫** config.toml 同名的 task_def 鍵（含 trigger/hour/minute）。
        要在 config 固定排程，請讓本方法回傳 None/falsy；要動態排程，config 的 trigger
        設定不可信，以本方法為準。
        """
        return None

    @abstractmethod
    def execute(self, *args:Any, **kwargs: Any) -> Any:
        raise NotImplementedError
@dataclass
class SayHelloTask(SchedTask):
    msg: str = field(default='bravo!!!', metadata={'type': 'StringField', 'label': 'Message', 'validators': [DataRequired()]})

    def __init__(self, app:FunlabFlask) -> None:
        super().__init__(app)

    @property
    def task_def(self):
        if self._task_def:
            return self._task_def
        else:
            raise Exception('No configuration set, need to provide as property here.')

    def execute(self, *args:Any, **kwargs: Any) -> Any:
        if (not args) and (not kwargs):
            print(f'Hello self msg: {self.msg}')
        else:
            if args:
                msg = args[0]
                print(f'Hello args, {msg}')
            else:
                msg = kwargs.get('msg', 'NA')
                print(f'Hello kwargs, {msg}')
        sleep(1)


