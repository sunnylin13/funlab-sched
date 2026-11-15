"""B0 覆蓋補強切片（同 Seam，非新 Seam；卡 t_bdf4eb9c 覆蓋率≥80% 門檻）。

只補既有 Seam 的邊角路徑：inventory CLI 抓取端、missed 的 spool/PG 倉與
巡邏緒、cli_run 邊角分支、service 預設構造路徑。
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore

from funlab.sched import inventory
from funlab.sched.alerting import Alerter
from funlab.sched.cli_run import main as cli_main
from funlab.sched.manifest import TaskSpec, load_dir
from funlab.sched.missed import HEARTBEAT_TASK_ID, SpoolStore, Watchdog
from funlab.sched.runrec import RunRecord, RunRecorder, SpoolSink
from funlab.sched.service import setup_run_record
from tests.conftest import FakeLogger


def _spec(**kw):
    base = dict(id='x', owner='dev-ops', kind='exec', entry='true')
    base.update(kw)
    return TaskSpec(**base)


class TestInventoryCollectors:
    def test_collectors_return_lists_on_real_host(self):
        # 真機呼叫僅驗證形制（防崩潰路徑本身要進覆蓋）
        assert isinstance(inventory._collect_crontab(), list)
        jobs = inventory._collect_hermes_jobs()
        assert isinstance(jobs, list) and all(isinstance(j, dict) for j in jobs)
        eps = inventory._collect_entry_points()
        assert isinstance(eps, list) and all(isinstance(e, str) for e in eps)


class TestInventoryMain:
    def test_drift_exit1_alerts_and_prints(self, tmp_path, monkeypatch, capsys):
        d = tmp_path / 'tasks.d'
        d.mkdir()
        (d / 'good.toml').write_text(
            'id = "a"\nowner = "o"\nkind = "exec"\nentry = "t"\n'
            'safety = { writes_db = false, idempotent = true }\n'
            'legacy_ref = "/bin/true"\n', encoding='utf-8')
        (d / 'bad.toml').write_text('id = 只壞在未知鍵\nzzz = 1\n', encoding='utf-8')
        monkeypatch.setattr(inventory, '_collect_crontab',
                            lambda: ['* * * * * /bin/true',
                                     '* * * * * /bin/ghost'])
        monkeypatch.setattr(inventory, '_collect_hermes_jobs', lambda: [])
        monkeypatch.setattr(inventory, '_collect_entry_points', lambda: [])
        al = types.SimpleNamespace(sent=[], send=lambda *a, **k: al.sent.append(a))
        rc = inventory.main(declarations=d, alerter=al)
        assert rc == 1                       # ghost 未登記→漂移
        out = capsys.readouterr().out
        assert 'ghost' in out and 'ok=' in out
        assert al.sent and al.sent[0][1] == 'inventory_drift'

    def test_clean_exit0_no_alert(self, tmp_path, monkeypatch):
        d = tmp_path / 'tasks.d'
        d.mkdir()
        (d / 'good.toml').write_text(
            'id = "a"\nowner = "o"\nkind = "exec"\nentry = "t"\n'
            'safety = { writes_db = false, idempotent = true }\n'
            'legacy_ref = "/bin/true"\n', encoding='utf-8')
        monkeypatch.setattr(inventory, '_collect_crontab',
                            lambda: ['* * * * * /bin/true'])
        monkeypatch.setattr(inventory, '_collect_hermes_jobs', lambda: [])
        monkeypatch.setattr(inventory, '_collect_entry_points', lambda: [])
        rc = inventory.main(declarations=d, alerter=None)
        assert rc == 0


class TestSpoolStoreRealFiles:
    def _row(self, **kw):
        base = dict(task_id='a', status='ok', trigger_kind='cron')
        base.update(kw)
        return RunRecord(**base)

    def test_last_ok_and_coverage_from_jsonl(self, tmp_path):
        sink = SpoolSink(tmp_path)
        t0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
        sink.write(self._row(scheduled_at=t0, finished_at=t0, status='failed'))
        t1 = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
        sink.write(self._row(scheduled_at=t1, finished_at=t1))
        st = SpoolStore(tmp_path)
        assert st.last_ok('a') == t1
        assert st.last_ok('zzz') is None
        assert st.has_row_at_or_after('a', t1)
        assert not st.has_row_at_or_after('a',
                                          datetime(2026, 11, 16, tzinfo=timezone.utc))
        # 壞行跳過不炸
        (tmp_path / 'junk.jsonl').write_text('{oops\n', encoding='utf-8')
        assert st.last_ok('a') == t1

    def test_broken_timestamp_row_skipped(self, tmp_path):
        (tmp_path / 'x.jsonl').write_text(
            json.dumps({'task_id': 'a', 'status': 'ok',
                        'finished_at': '不是時間'}) + '\n', encoding='utf-8')
        st = SpoolStore(tmp_path)
        assert st.last_ok('a') is None
        assert not st.has_row_at_or_after('a', datetime.now(timezone.utc))


class _FakeSink:
    available = True
    _engine = None


class _FakeRes:
    def __init__(self, v):
        self._v = v

    def scalar(self):
        return self._v

    def first(self):
        return self._v


class _FakeConn:
    def __init__(self, v):
        self.v = v

    def execute(self, stmt, params):
        return _FakeRes(self.v)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeEngine:
    def __init__(self, v):
        self.v = v

    def connect(self):
        return _FakeConn(self.v)


class TestPgStoreQueries:
    def test_last_ok_and_coverage_hit(self):
        from funlab.sched.missed import PgStore
        dt = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
        st = PgStore(_FakeEngine(dt))
        assert st.last_ok('a') == dt
        assert st.has_row_at_or_after('a', dt) is True

    def test_no_row_paths(self):
        from funlab.sched.missed import PgStore
        st = PgStore(_FakeEngine(None))
        assert st.last_ok('a') is None
        assert st.has_row_at_or_after('a', datetime.now(timezone.utc)) is False

    def test_engine_error_swallowed(self):
        from funlab.sched.missed import PgStore

        class _Boom:
            def connect(self):
                raise RuntimeError('db down')
        st = PgStore(_Boom())
        assert st.last_ok('a') is None
        assert st.has_row_at_or_after('a', datetime.now(timezone.utc)) is False


class TestWatchdogInternals:
    def test_store_falls_back_to_spool_without_engine(self, tmp_path):
        rec = RunRecorder(spool_dir=tmp_path / 'spool', db_sink=_FakeSink())
        wd = Watchdog(specs_provider=lambda: {}, recorder=rec,
                      spool_dir=tmp_path / 'spool',
                      clock=lambda: datetime(2026, 11, 15, tzinfo=timezone.utc))
        assert isinstance(wd._store(), SpoolStore)   # engine None→spool

    def test_alert_exception_swallowed(self, tmp_path):
        rec = RunRecorder(spool_dir=tmp_path / 'spool')

        class _Boom:
            def send(self, *a, **k):
                raise RuntimeError('alert channel down')
        lg = FakeLogger()
        wd = Watchdog(specs_provider=lambda: {}, recorder=rec, alerter=_Boom(),
                      logger=lg,
                      clock=lambda: datetime(2026, 11, 15, tzinfo=timezone.utc))
        wd._alert('a', 'failed', 'msg')   # 不拋＝過

    def test_thread_start_stop(self, tmp_path):
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        wd = Watchdog(specs_provider=lambda: {}, recorder=rec,
                      spool_dir=tmp_path / 'spool', interval_sec=0.05,
                      clock=lambda: datetime(2026, 11, 15, tzinfo=timezone.utc))
        wd.start()
        wd.start()   # 冪等
        import time
        time.sleep(0.2)
        wd.stop()
        time.sleep(0.1)
        rows = []
        for f in (tmp_path / 'spool').glob('*.jsonl'):
            rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        assert any(r['task_id'] == HEARTBEAT_TASK_ID for r in rows)


class TestCliRunEdges:
    def _decl(self, tmp_path):
        d = tmp_path / 'tasks.d'
        d.mkdir(exist_ok=True)
        return d

    def test_load_error_printed_but_valid_task_runs(self, tmp_path):
        d = self._decl(tmp_path)
        (d / 'good.toml').write_text(
            'id = "a"\nowner = "o"\nkind = "exec"\nentry = "true"\n'
            'safety = { writes_db = false, idempotent = true }\n', encoding='utf-8')
        (d / 'bad.toml').write_text('id = "b"\nzzz = 1\n', encoding='utf-8')
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        al = types.SimpleNamespace(send=lambda *a, **k: True)
        rc = cli_main(['a'], declarations=d, recorder=rec, alerter=al,
                      runner=lambda *a, **k: types.SimpleNamespace(returncode=0),
                      sleeper=lambda s: None)
        assert rc == 0   # 坏檔拒載但不連累好檔任務

    def test_no_args_exits(self, tmp_path):
        import pytest
        d = self._decl(tmp_path)
        with pytest.raises(SystemExit):
            cli_main([], declarations=d)

    def test_alert_failure_still_returns_rc(self, tmp_path):
        d = self._decl(tmp_path)
        (d / 'a.toml').write_text(
            'id = "a"\nowner = "o"\nkind = "exec"\nentry = "true"\n'
            'safety = { writes_db = true, idempotent = false }\n', encoding='utf-8')
        rec = RunRecorder(spool_dir=tmp_path / 'spool')

        class _Boom:
            def send(self, *a, **k):
                raise RuntimeError('channel down')
        rc = cli_main(['a'], declarations=d, recorder=rec, alerter=_Boom(),
                      runner=lambda *a, **k: types.SimpleNamespace(returncode=7),
                      sleeper=lambda s: None)
        assert rc == 7


class TestServiceDefaults:
    def _svc(self, db_url=None):
        sch = BackgroundScheduler(jobstores={'default': MemoryJobStore()})
        s = types.SimpleNamespace()
        s._scheduler = sch
        s.mylogger = FakeLogger()
        s.plugin_config = {}
        s.app = types.SimpleNamespace(
            dbmgr=types.SimpleNamespace(get_db_url=lambda: db_url))
        return s

    def test_default_recorder_uses_dbmgr_url(self, tmp_path):
        s = self._svc(db_url=None)   # url None→探測失敗→spool 降級路徑
        ok = setup_run_record(s, decl_dir=tmp_path, start_watchdog=False)
        assert ok is True
        assert s._run_recorder.db_sink is not None   # 預設構造有 PGSink

    def test_replay_called_and_provider_logs_load_errors(self, tmp_path):
        s = self._svc()
        ok = setup_run_record(s, decl_dir=tmp_path, start_watchdog=False)
        assert ok is True
        (tmp_path / 'bad.toml').write_text('zzz = 1\n', encoding='utf-8')
        s._run_watchdog.tick()   # provider 走 load_dir→錯誤進日誌不拋
        assert any('宣告檔' in m for lvl, m in s.mylogger.messages)
