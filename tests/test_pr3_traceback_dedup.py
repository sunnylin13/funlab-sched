"""ARCH-LOGGING PR-3 §4 N4：任務失敗 traceback 去重（同任務同日僅首筆全量）。

規格來源（KB dev/designs/ARCH-LOGGING §4 N4、卡 t_242ca7a5 §B-8）：
- 保留 ERROR（任務失敗＝需人看），但 traceback 去重：同任務同日僅首筆全量，
  其後單行（ERROR 照記、不含 traceback）。
seam：SchedTask._execute_with_hooks（卡 body 指定「任務包裝層」）。
期望值獨立來源：設計 §4 表格 N4 行「同任務同日僅首筆全量」。
"""
from __future__ import annotations

import logging

import pytest

from funlab.sched.task import SchedTask


class RecordingLogger:
    """mylogger 替身：記錄 (level, msg, exc_info)。"""

    def __init__(self):
        self.calls: list[tuple[str, str, bool]] = []

    def info(self, msg='', *a, **k):
        self.calls.append(('info', str(msg), False))

    def warning(self, msg='', *a, exc_info=False, **k):
        self.calls.append(('warning', str(msg), bool(exc_info)))

    def error(self, msg='', *a, exc_info=False, **k):
        self.calls.append(('error', str(msg), bool(exc_info)))

    def debug(self, msg='', *a, **k):
        self.calls.append(('debug', str(msg), False))

    def progress(self, msg='', *a, **k):
        pass

    def end_progress(self, msg='', *a, **k):
        pass

    def errors(self):
        return [c for c in self.calls if c[0] == 'error']


class BoomTask(SchedTask):
    def execute(self, *args, **kwargs):
        raise RuntimeError("boom")


@pytest.fixture()
def boom_task(stub_service):
    t = BoomTask(stub_service)
    t.mylogger = RecordingLogger()
    return t


def test_first_failure_keeps_full_traceback(boom_task):
    """規格：當日首筆失敗＝ERROR＋exc_info 全量 traceback。"""
    with pytest.raises(RuntimeError):
        boom_task._execute_with_hooks()
    errs = boom_task.mylogger.errors()
    assert len(errs) == 1
    assert errs[0][2] is True, "首筆需含 traceback（exc_info=True）"


def test_second_failure_same_day_single_line(boom_task):
    """規格：同任務同日第二筆＝ERROR 單行（不含 traceback），仍照記不吞。"""
    with pytest.raises(RuntimeError):
        boom_task._execute_with_hooks()
    with pytest.raises(RuntimeError):
        boom_task._execute_with_hooks()
    errs = boom_task.mylogger.errors()
    assert len(errs) == 2, "第二筆仍必須記 ERROR（ERROR＝需人看語意不減）"
    assert errs[1][2] is False, "第二筆不得再含 traceback"


def test_dedup_state_resets_next_day(boom_task, monkeypatch):
    """規格：去重以「日」為界——跨日後再次全量。
    時鐘注入：monkeypatch 模組級 _today_str()（任務包裝層的日期來源）。"""
    import funlab.sched.task as tm

    with pytest.raises(RuntimeError):
        boom_task._execute_with_hooks()
    monkeypatch.setattr(tm, "_today_str", lambda: "20990101")
    boom_task.mylogger.calls.clear()
    with pytest.raises(RuntimeError):
        boom_task._execute_with_hooks()
    errs = boom_task.mylogger.errors()
    assert len(errs) == 1
    assert errs[0][2] is True, "跨日後應重新全量 traceback"
