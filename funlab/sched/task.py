from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
import datetime
import logging
from time import sleep
from typing import Any
from dataclasses import dataclass, field, fields
from wtforms import StringField, IntegerField, FloatField, BooleanField, DateField
from wtforms.validators import DataRequired, Optional as OptionalValidator
from typing import get_type_hints, Union
from flask_wtf import FlaskForm
from funlab.core import _Configuable
from funlab.core.config import Config
from funlab.flaskr.app import FunlabFlask
from funlab.utils import log
from wtforms import DateField, DateTimeField, FloatField, HiddenField, IntegerField, BooleanField, DecimalField, StringField
from wtforms.validators import DataRequired
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from funlab.sched.service import SchedService

@dataclass
class SchedTask(_Configuable, ABC):
    id:str = field(init=False, metadata={'type': HiddenField})
    name:str = field(metadata={'type': HiddenField})

    @staticmethod
    def form_javascript():
        return ''

    def __init__(self, sched:SchedService, name=None) -> None:
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
        self.task_config = self.get_config(file_name='task.toml', ext_config=ext_task_config)  # group_session=sched.__class__.__name__)
        if 'task_def' in self.task_config:
            self._task_def = self.task_config.get('task_def', {})
        else:
            self._task_def = {key:val for key, val in vars(self.task_config).items()
                                if(not key.startswith('_') and not key[0].isupper()) }
        self._task_def.update(dict(id=self.id, name=self.name, func=self.execute, replace_existing=True))  ## replace_existing 必需一定為true, 當使用jobstroe時

        func_default_kwargs  = self.task_def.get('kwargs', {})
        for key, value in func_default_kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
                setattr(self.__dataclass_fields__[key], 'default', value)

        self.form_class = self.generate_params_formclass()

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
        TYPE_MAPPING = {
            str: StringField,
            int: IntegerField,
            float: FloatField,
            bool: BooleanField,
            datetime.date: DateField,
            datetime.datetime: DateTimeField
        }
        form_fields = {}
        type_hints = get_type_hints(type(self))
        for field in fields(self):
            field_metadata = field.metadata
            field_name = field.name
            field_type = type_hints[field_name]
                        # 處理 Optional 類型
            is_optional = False
            if hasattr(field_type, "__origin__") and field_type.__origin__ is Union:
                args = field_type.__args__
                if type(None) in args:
                    is_optional = True
                    # 找出非 None 的類型
                    field_type = next(arg for arg in args if arg is not type(None))
            form_field_class = field_metadata.get('type', TYPE_MAPPING.get(field_type, StringField))

            field_kwargs = {}
            for key, value in field_metadata.items():
                if key!='type':
                    field_kwargs.update({key:value})
            field_kwargs.update({'default':field_metadata.get('default', getattr(self, field.name, None))})
            for key, value in field_kwargs.copy().items():
                if value is None:
                    field_kwargs.pop(key)

            # 如果是可選欄位且沒有明確設置驗證器，則不添加 DataRequired
            if is_optional and not field_kwargs.get('validators', None):
                field_kwargs['validators'].append(OptionalValidator())

            form_fields[field.name] = form_field_class(**field_kwargs)
        form_fields['javascript'] = self.form_javascript()
        form_class = type(self.__class__.__name__+ 'ParamsForm', (FlaskForm,), form_fields)
        return form_class
@dataclass
class SayHelloTask(SchedTask):
    msg: str = field(default='bravo!!!', metadata={'type': StringField, 'label': 'Message', 'validators': [DataRequired()]})

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


