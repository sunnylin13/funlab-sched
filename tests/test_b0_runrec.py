"""runrec 測試（卡 t_bdf4eb9c 工項②；報告 §3.4/§3.5 條文＝獨立期望來源）。

Seam：RunRecorder.write（PG→spool 降級、永不拋）、SchedRunRecorder.on_event
（事件→終態單筆映射）。期望欄位名逐字取自報告 §3.4 欄位表。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.events import (EVENT_JOB_ERROR, EVENT_JOB_EXECUTED,
                                EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED,
                                JobEvent, JobExecutionEvent, JobSubmissionEvent)
from sqlalchemy import create_engine, text

from funlab.sched.runrec import (RunRecord, RunRecorder, SchedRunRecorder,
                                 SpoolSink, truncate_error)

UTC = timezone.utc


def _rec(**kw):
    base = dict(task_id='FetchDailyPrice', status='ok',
                started_at=datetime(2026, 11, 15, 6, 40, tzinfo=UTC),
                finished_at=datetime(2026, 11, 15, 6, 41, tzinfo=UTC),
                scheduled_at=datetime(2026, 11, 15, 6, 40, tzinfo=UTC))
    base.update(kw)
    return RunRecord(**base)


class TestTruncateError:
    def test_500_char_limit(self):
        # §3.4：錯誤摘要限 500 字
        assert len(truncate_error('x' * 900)) == 500
        assert truncate_error('short') == 'short'
        assert truncate_error(None) is None


class TestSpoolSink:
    def test_appends_jsonl_one_line_per_run(self, tmp_path):
        sink = SpoolSink(tmp_path / 'spool')
        sink.write(_rec())
        sink.write(_rec(status='failed'))
        files = list((tmp_path / 'spool').glob('*.jsonl'))
        assert len(files) == 1  # 同日一檔
        lines = files[0].read_text(encoding='utf-8').strip().splitlines()
        assert len(lines) == 2
        row = json.loads(lines[1])
        assert row['task_id'] == 'FetchDailyPrice'
        assert row['status'] == 'failed'
        assert set(row) >= {'run_id', 'task_id', 'scheduled_at', 'started_at',
                            'finished_at', 'status', 'attempt', 'trigger_kind',
                            'exit_code', 'error_summary', 'git_sha', 'host',
                            'log_ref'}  # §3.4 欄位表逐字


class _BoomSink:
    def write(self, record):
        raise RuntimeError('pg down')

    available = True


class _CountingSink:
    def __init__(self):
        self.rows = []
        self.available = True

    def write(self, record):
        self.rows.append(record)


class TestRunRecorderFallback:
    def test_pg_failure_degrades_to_spool_never_raises(self, tmp_path):
        # 紅線：紀錄寫入失敗不得讓任務失敗（降級 JSONL spool）
        rec = RunRecorder(spool_dir=tmp_path / 'spool', db_sink=_BoomSink(),
                          logger=None)
        assert rec.write(_rec()) == 'spool'
        assert len(list((tmp_path / 'spool').glob('*.jsonl'))) == 1

    def test_no_db_sink_goes_straight_to_spool(self, tmp_path):
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        assert rec.write(_rec()) == 'spool'

    def test_spool_failure_drops_quietly_never_raises(self, tmp_path, monkeypatch):
        rec = RunRecorder(spool_dir=tmp_path / 'spool', db_sink=_BoomSink())
        monkeypatch.setattr('funlab.sched.runrec.SpoolSink.write',
                            lambda self, r: (_ for _ in ()).throw(OSError('disk full')))
        assert rec.write(_rec()) == 'dropped'  # 不拋＝任務不受累

    def test_error_summary_truncated_on_write(self, tmp_path):
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        rec.write(_rec(status='failed', error_summary='e' * 800))
        row = [json.loads(l) for l in
               next((tmp_path / 'spool').glob('*.jsonl')).read_text().splitlines()][-1]
        assert len(row['error_summary']) == 500

    def test_git_sha_host_defaults_filled(self, tmp_path):
        rec = RunRecorder(spool_dir=tmp_path / 'spool', git_sha='abc1234', host='spark')
        r = _rec()
        rec.write(r)
        assert r.git_sha == 'abc1234' and r.host == 'spark'


class TestPgSink:
    """PGSink 對「表在位/缺席」兩態（實查閘：碼不得依賴表存在）。

    測試以 sqlite 記憶體庫建同名表模擬 C2 DDL 落地後狀態。
    """

    DDL = """
    CREATE TABLE sched_run (
        run_id text primary key, task_id text not null,
        scheduled_at timestamp, started_at timestamp, finished_at timestamp,
        status text not null, attempt integer default 1, trigger_kind text,
        exit_code integer, error_summary text, git_sha text, host text, log_ref text)
    """

    def _engine_with_table(self):
        eng = create_engine('sqlite://')
        with eng.begin() as c:
            c.execute(text(self.DDL))
        return eng

    def test_inserts_all_section_34_columns(self):
        eng = self._engine_with_table()
        from funlab.sched.runrec import PGSink
        sink = PGSink(lambda: 'sqlite://', engine_factory=lambda url: eng)
        r = _rec(status='ok', trigger_kind='cron', attempt=1)
        sink.write(r)
        with eng.connect() as c:
            row = c.execute(text('select * from sched_run')).mappings().one()
        assert row['task_id'] == 'FetchDailyPrice' and row['status'] == 'ok'
        assert row['run_id'] == r.run_id

    def test_absent_table_marks_unavailable_and_recorder_falls_back(self, tmp_path):
        empty = create_engine('sqlite://')  # 無 sched_run 表＝現況正式庫
        from funlab.sched.runrec import PGSink
        sink = PGSink(lambda: 'sqlite://', engine_factory=lambda url: empty)
        rec = RunRecorder(spool_dir=tmp_path / 'spool', db_sink=sink)
        assert rec.write(_rec()) == 'spool'
        # 第二次不再撞 INSERT（缺席狀態已快取）：仍降級且不拋
        assert rec.write(_rec(task_id='B')) == 'spool'

    def test_replay_spool_backfills_pg_then_marks_done(self, tmp_path):
        eng = create_engine('sqlite://')  # 先缺席 → spool
        from funlab.sched.runrec import PGSink
        sink = PGSink(lambda: 'sqlite://', engine_factory=lambda url: eng)
        rec = RunRecorder(spool_dir=tmp_path / 'spool', db_sink=sink)
        rec.write(_rec())
        rec.write(_rec(task_id='C'))
        # 模擬 C2 落地：同名表建好＋sink 重探
        with eng.begin() as c:
            c.execute(text(self.DDL))
        sink.reset_availability()
        stats = rec.replay_spool()
        assert stats == {'replayed': 2, 'failed': 0}
        with eng.connect() as c:
            n = c.execute(text('select count(*) from sched_run')).scalar()
        assert n == 2
        assert not list((tmp_path / 'spool').glob('*.jsonl'))  # 補入後不再重複補


class _FakeLogger:
    def __init__(self):
        self.msgs = []

    def info(self, m, *a, **k): self.msgs.append(('info', m))
    def warning(self, m, *a, **k): self.msgs.append(('warning', m))
    def error(self, m, *a, **k): self.msgs.append(('error', m))


def _tzdt(y=2026, mo=11, d=15, h=6, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


class TestSchedRunRecorderListener:
    """事件映射（§3.5-1；一執行一終態單筆＝卡面驗收1 容差0 口徑）。"""

    def _mk(self, tmp_path):
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        listener = SchedRunRecorder(rec, logger=_FakeLogger())
        return rec, listener

    def _rows(self, tmp_path):
        rows = []
        for f in (tmp_path / 'spool').glob('*.jsonl'):
            rows += [json.loads(l) for l in f.read_text().splitlines()]
        return rows

    def test_executed_maps_to_single_ok_row(self, tmp_path):
        rec, l = self._mk(tmp_path)
        l.on_event(JobSubmissionEvent(EVENT_JOB_SUBMITTED, 'FetchFinstmt', 'default',
                                      scheduled_run_times=[_tzdt(h=1, mi=10)]))
        l.on_event(JobExecutionEvent(EVENT_JOB_EXECUTED, 'FetchFinstmt', 'default',
                                     scheduled_run_time=_tzdt(h=1, mi=10), retval='done'))
        rows = self._rows(tmp_path)
        assert len(rows) == 1  # SUBMITTED 不落行（記憶體配對 started_at）
        row = rows[0]
        assert row['status'] == 'ok' and row['task_id'] == 'FetchFinstmt'
        assert row['trigger_kind'] == 'cron'
        assert row['scheduled_at'].startswith('2026-11-15T01:10')
        assert row['started_at'] is not None  # 由 SUBMITTED 配對
        assert row['attempt'] == 1

    def test_error_maps_failed_with_error_summary(self, tmp_path):
        rec, l = self._mk(tmp_path)
        l.on_event(JobExecutionEvent(EVENT_JOB_ERROR, 'BookKeeping', 'default',
                                     scheduled_run_time=_tzdt(),
                                     exception=ValueError('boom'),
                                     traceback='TB'))
        row = self._rows(tmp_path)[-1]
        assert row['status'] == 'failed'
        assert 'boom' in row['error_summary']

    def test_missed_maps_missed(self, tmp_path):
        rec, l = self._mk(tmp_path)
        ev = JobExecutionEvent(EVENT_JOB_MISSED, 'SipWatch', 'default',
                               scheduled_run_time=_tzdt(h=10, mi=20))
        l.on_event(ev)
        row = self._rows(tmp_path)[-1]
        assert row['status'] == 'missed'
        assert row['task_id'] == 'SipWatch'

    def test_manual_suffix_stripped_and_kind_manual(self, tmp_path):
        rec, l = self._mk(tmp_path)
        l.on_event(JobExecutionEvent(EVENT_JOB_EXECUTED, 'FetchRevenue_M', 'default',
                                     scheduled_run_time=_tzdt()))
        row = self._rows(tmp_path)[-1]
        assert row['task_id'] == 'FetchRevenue'
        assert row['trigger_kind'] == 'manual'

    def test_listener_never_raises(self, tmp_path):
        # SCH-13 紀律：listener 自我吸收（正式庫事件線禁物＝二次炸排程器）
        rec, l = self._mk(tmp_path)
        monkey_bad = JobEvent(99999, 'j', 'X')  # 非 submission/execution
        l.on_event(monkey_bad)  # 不拋即過
        l.on_event(object())    # 垃圾物件也不拋
