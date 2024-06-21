from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
import logging
from time import sleep
from typing import Any

from flask_wtf import FlaskForm
from funlab.core import _Configuable
from funlab.core.config import Config
from funlab.utils import log
from wtforms import HiddenField, StringField
from wtforms.validators import DataRequired
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from funlab.sched.service import SchedService
    from funlab.core.appbase import _FlaskBase

@dataclass
class SchedTask(_Configuable, ABC):
    id:str = field(init=False, metadata={'type': HiddenField})
    name:str = field(metadata={'type': HiddenField})

    @staticmethod
    def form_javascript():
        return ''

    def __init__(self, sched:SchedService, name=None) -> None:
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
        self.task_config = self.get_config(file_name='task.toml', ext_config=ext_task_config)  # group_session=sched.__class__.__name__)

        log_type = self.task_config.get("log_type")
        log_level = self.task_config.get("log_level")
        # Check if log type and level are valid
        if log_type and log_type not in log.LogType.__members__.keys():
            raise ValueError(f"{self.__class__.__name__} has invalid log_type '{log_type}' in config")
        if log_level and log_level not in logging.getLevelNamesMapping():
            raise ValueError(f"{self.__class__.__name__} has invalid log_level '{log_level}' in config. Should be {tuple(logging.getLevelNamesMapping())}")
        # Create logger according specified log type and level
        if log_type and log_level:
            self.mylogger = log.get_logger(self.__class__.__name__, logtype=log.LogType[log_type], level=logging.getLevelName(log_level))
        elif log_type:
            self.mylogger = log.get_logger(self.__class__.__name__, logtype=log.LogType[log_type], level=logging.INFO)
        elif log_level:
            self.mylogger = log.get_logger(self.__class__.__name__, level=logging.getLevelName(log_level))
        else:
            self.mylogger = log.get_logger(self.__class__.__name__, level=logging.INFO)

        if 'task_def' in self.task_config:
            self._task_def = self.task_config.get('task_def', {})
        else:
            self._task_def = {key:val for key, val in vars(self.task_config).items()
                                if(not key.startswith('_') and not key[0].isupper()) and key not in ('log_type', 'log_level')}
        self._task_def.update(dict(id=self.id, name=self.name, func=self.execute, replace_existing=True))  ## replace_existing 必需一定為true, 當使用jobstroe時

        func_default_kwargs  = self.task_def.get('kwargs', {})
        for key, value in func_default_kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
                setattr(self.__dataclass_fields__[key], 'default', value)

    def __getattr__(self, name):
        # delegate apscheduler's Job attribute
        if name in ('trigger', 'executor', 'func', 'func_ref',
                    'args', 'kwargs', 'misfire_grace_time',
                    'coalesce', 'max_instances', 'next_run_time',):
            return getattr(self.job, name, None)
        # Default behaviour
        return self.__getattribute__(name) # super().__getattr__(name)

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
        """subclass provided so SchedSevice will call when config no 'trigger' defined,
           to let 'Task' provide 'trigger shchedule' at runtime according to current datetime."""
        return None

    @abstractmethod
    def execute(self, *args:Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def generate_params_formclass(self):
        form_fields = {}
        for field in fields(self):
            field_metadata = field.metadata
            if 'type' in field_metadata:
                field_type = field_metadata['type']
                kwargs = {}
                for key, value in field_metadata.items():
                    if key!='type':
                        kwargs.update({key:value})
                kwargs.update({'default':field_metadata.get('default', getattr(self, field.name, None))})
                for key, value in kwargs.copy().items():
                    if value is None:
                        kwargs.pop(key)
                form_fields[field.name] = field_type(**kwargs)
        form_fields['javascript'] = self.form_javascript()
        form_class = type(self.__class__.__name__+ 'ParamsForm', (FlaskForm,), form_fields)
        return form_class

@dataclass
class SayHelloTask(SchedTask):
    msg: str = field(default='bravo!!!', metadata={'type': StringField, 'label': 'Message', 'validators': [DataRequired()]})

    def __init__(self, app:_FlaskBase):
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
