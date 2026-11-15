"""B0 service 接線 Seam 測試（卡 t_bdf4eb9c；SCH-13/14 紀律）。

邊界＝funlab.sched.service.setup_run_record(service, ...)：
module 層 helper（本倉 testable-helper 範式）＋假 service（SimpleNamespace）。
期望值出處：卡面工項①＋紅線「紀錄寫入失敗不得讓任務失敗」→ 接線本身
亦不得把服務拖死（全自吸）；報告 §4-B0① listener 覆蓋全任務。
"""
from __future__ import annotations

import types

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore

from funlab.sched.service import setup_run_record
from tests.conftest import FakeLogger


class _Cfg(dict):
    def as_dict(self):
        return dict(self)


def _svc(**cfg):
    sch = BackgroundScheduler(jobstores={'default': MemoryJobStore()})
    s = types.SimpleNamespace()
    s._scheduler = sch
    s.mylogger = FakeLogger()
    s.plugin_config = _Cfg(cfg)
    return s


def _listener_count(sch) -> int:
    return len(getattr(sch, '_listeners', []))


class TestSetup:
    def test_default_on_attaches_listener_and_state(self, tmp_path):
        from funlab.sched.runrec import RunRecorder
        from funlab.sched.alerting import Alerter
        s = _svc()
        before = _listener_count(s._scheduler)
        ok = setup_run_record(s, decl_dir=tmp_path / 'none.d',
                              recorder=RunRecorder(spool_dir=tmp_path / 'spool'),
                              alerter=Alerter(channel='test',
                                              state_dir=tmp_path),
                              start_watchdog=False)
        assert ok is True
        assert _listener_count(s._scheduler) > before   # listener 已掛上
        assert s._run_recorder is not None and s._run_watchdog is not None

    def test_flag_off_no_wiring(self, tmp_path):
        s = _svc(RUN_RECORD=False)
        ok = setup_run_record(s, decl_dir=tmp_path, start_watchdog=False)
        assert ok is False
        assert not hasattr(s, '_run_recorder')

    def test_wiring_failure_swallowed(self, tmp_path):
        # 紅線延伸：接線故障不得把服務炸掉→False＋日誌＋不拋
        s = _svc()

        class _Boom:
            def add_listener(self, *a, **k):
                raise RuntimeError('scheduler gone')
        s._scheduler = _Boom()
        ok = setup_run_record(s, decl_dir=tmp_path, start_watchdog=False)
        assert ok is False
        assert any('RUN_RECORD' in m for lvl, m in s.mylogger.messages)

    def test_listener_records_spool_row_after_attach(self, tmp_path):
        # 端到端最小鏈：attach 後事件 → spool 有一筆終態
        import json
        from apscheduler.events import EVENT_JOB_EXECUTED, JobExecutionEvent
        from funlab.sched.runrec import RunRecorder
        from funlab.sched.alerting import Alerter
        s = _svc()
        setup_run_record(s, decl_dir=tmp_path / 'none.d',
                         recorder=RunRecorder(spool_dir=tmp_path / 'spool'),
                         alerter=Alerter(channel='test', state_dir=tmp_path),
                         start_watchdog=False)
        s._run_recorder  # 接線成功
        # 直接餵事件線（事件分派由 APScheduler 負責，此處驗 listener 已掛）
        ev = JobExecutionEvent(EVENT_JOB_EXECUTED, 'FetchFinstmt', 'default',
                               scheduled_run_time=None)
        s._run_watchdog  # watchdog 未 start（執行緒不起）
        from funlab.sched.runrec import SchedRunRecorder
        # 找到掛上的 listener 並餵事件
        cbs = [cb for cb, _m in getattr(s._scheduler, '_listeners', [])] \
            if s._scheduler._listeners else []
        for cb in cbs:
            cb(ev)
        rows = []
        for f in (tmp_path / 'spool').glob('*.jsonl'):
            rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        assert any(r['task_id'] == 'FetchFinstmt' and r['status'] == 'ok'
                   for r in rows)
