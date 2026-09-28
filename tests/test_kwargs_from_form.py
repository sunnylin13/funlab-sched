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
    # 真 SchedTask 是 dataclass；以 ArgsSpec 實例擔任，令 fields(task) 語意與生產一致
    t = ArgsSpec()
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
    t = ReqSpec()
    t.form_class = create_form_from_dataclass(ReqSpec)
    kwargs, errors = build_kwargs_from_form(t, {'sym': ''})
    assert kwargs is None
    assert 'sym' in errors
