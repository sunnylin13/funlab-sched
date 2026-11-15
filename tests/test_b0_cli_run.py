"""B0 工項② `fund13-sched-run` 外殼 Seam 測試（卡 t_bdf4eb9c）。

邊界＝funlab.sched.cli_run.main(argv)：只做「跑一個 exec 任務＋寫紀錄＋重試／告警」。
期望值出處：
- 卡面要求2：外殼供後續 crontab 改調用；本卡不改任何現行調用。
- 卡面驗收3：writes_db=true ∧ idempotent=false 禁自動重試直接告警。
- 報告 §3.4：trigger_kind ∈ {cron,manual,retry,dependency}；status 六值（#5633）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from funlab.sched.cli_run import main
from funlab.sched.runrec import RunRecorder

BASE = """
owner = "dev-ops"

[retry]
max = 3
backoff_sec = 1
"""


def _decl(tmp_path: Path, body: str) -> str:
    d = tmp_path / 'tasks.d'
    d.mkdir(exist_ok=True)
    (d / 'a.toml').write_text(body + BASE, encoding='utf-8')
    return str(d)


def _rows(spool_dir: Path):
    rows = []
    for f in sorted(spool_dir.glob('*.jsonl')):
        rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return rows


class _Proc:
    """subprocess.CompletedProcess 替身。"""
    def __init__(self, returncode):
        self.returncode = returncode


class _Runner:
    """依腳本回 rc 的 subprocess.run 替身（記 argv）。"""
    def __init__(self, rcs):
        self.rcs = list(rcs)
        self.calls = []
        self.kws = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kws.append(kw)
        rc = self.rcs.pop(0) if self.rcs else 0
        if rc == 'TIMEOUT':
            raise subprocess.TimeoutExpired(argv, kw.get('timeout', 0))
        return _Proc(rc)


class _Alerts:
    def __init__(self):
        self.sent = []

    def send(self, task_id, event, message, **kw):
        self.sent.append((task_id, event))
        return True


OK_SPEC = """
id = "a"
kind = "exec"
entry = "true"
trigger = { type = "cron", expr = "0 10 * * *", tz = "Asia/Taipei" }
safety = { writes_db = false, idempotent = true }
"""

NI_SPEC = """
id = "a"
kind = "exec"
entry = "true"
trigger = { type = "cron", expr = "0 10 * * *", tz = "Asia/Taipei" }
safety = { writes_db = true, idempotent = false }
"""


class TestRunSuccess:
    def test_ok_writes_one_cron_row(self, tmp_path):
        decl = _decl(tmp_path, OK_SPEC)
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        al = _Alerts()
        rc = main(['a'], declarations=decl, recorder=rec, alerter=al,
                  runner=_Runner([0]), sleeper=lambda s: None)
        assert rc == 0
        rows = _rows(tmp_path / 'spool')
        assert len(rows) == 1
        r = rows[0]
        assert r['task_id'] == 'a' and r['status'] == 'ok'
        # 外殼預設 cron 觸發（crontab 行調用）；§3.4 枚舉
        assert r['trigger_kind'] == 'cron'
        assert r['attempt'] == 1 and r['exit_code'] == 0
        assert r['started_at'] and r['finished_at'] and r['scheduled_at']

    def test_manual_flag(self, tmp_path):
        decl = _decl(tmp_path, OK_SPEC)
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        rc = main(['a', '--manual'], declarations=decl, recorder=rec,
                  alerter=_Alerts(), runner=_Runner([0]), sleeper=lambda s: None)
        assert rc == 0
        assert _rows(tmp_path / 'spool')[0]['trigger_kind'] == 'manual'


class TestRetryPolicy:
    def test_transient_failure_retries_then_ok(self, tmp_path):
        # §3.5 retry.max_attempts=3：失敗→retrying 列→重試→ok 終態列
        decl = _decl(tmp_path, OK_SPEC)
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        runner = _Runner([1, 0])
        slept = []
        rc = main(['a'], declarations=decl, recorder=rec, alerter=_Alerts(),
                  runner=runner, sleeper=slept.append)
        assert rc == 0
        assert len(runner.calls) == 2 and slept == [1]
        rows = _rows(tmp_path / 'spool')
        assert [(r['status'], r['attempt']) for r in rows] == \
            [('retrying', 1), ('ok', 2)]
        assert rows[1]['trigger_kind'] == 'cron'   # 同一次執行的 attempt 遞增

    def test_final_failure_writes_failed_and_alerts(self, tmp_path):
        decl = _decl(tmp_path, OK_SPEC)
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        al = _Alerts()
        rc = main(['a'], declarations=decl, recorder=rec, alerter=al,
                  runner=_Runner([1, 1, 1]), sleeper=lambda s: None)
        assert rc == 1
        rows = _rows(tmp_path / 'spool')
        assert [(r['status'], r['attempt']) for r in rows] == \
            [('retrying', 1), ('retrying', 2), ('failed', 3)]
        assert ('a', 'failed') in al.sent

    def test_non_idempotent_writes_db_no_retry_direct_alert(self, tmp_path):
        # 卡面驗收3：writes_db=true ∧ idempotent=false → 禁自動重試直接告警
        decl = _decl(tmp_path, NI_SPEC)
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        al = _Alerts()
        runner = _Runner([1, 1, 1])
        rc = main(['a'], declarations=decl, recorder=rec, alerter=al,
                  runner=runner, sleeper=lambda s: None)
        assert rc == 1
        assert len(runner.calls) == 1, '非冪等寫庫任務不准重試'
        rows = _rows(tmp_path / 'spool')
        assert [(r['status'], r['attempt']) for r in rows] == [('failed', 1)]
        assert ('a', 'failed') in al.sent

    def test_timeout_status(self, tmp_path):
        decl = _decl(tmp_path, NI_SPEC)   # timeout 不重試（結果不可知）
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        al = _Alerts()
        rc = main(['a'], declarations=decl, recorder=rec, alerter=al,
                  runner=_Runner(['TIMEOUT']), sleeper=lambda s: None)
        assert rc != 0
        assert _rows(tmp_path / 'spool')[0]['status'] == 'timeout'
        assert ('a', 'timeout') in al.sent


class TestDeclaredTimeout:
    """F2（reviewer R1 必修·manager 背書）：宣告欄 timeout_sec 生效（§3.3
    每任務宣告逾時）。優先序＝呼叫端顯式覆寫 > spec.timeout_sec > 全域預設。"""

    def _captured_timeout(self, tmp_path, spec_body, **kw):
        decl = _decl(tmp_path, spec_body)
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        runner = _Runner([0])
        rc = main(['a'], declarations=decl, recorder=rec, alerter=_Alerts(),
                  runner=runner, sleeper=lambda s: None, **kw)
        assert rc == 0
        return runner.kws[0].get('timeout')

    def test_spec_timeout_used_when_no_explicit_override(self, tmp_path):
        got = self._captured_timeout(tmp_path, OK_SPEC + 'timeout_sec = 7\n')
        assert got == 7.0

    def test_explicit_kwarg_overrides_spec(self, tmp_path):
        got = self._captured_timeout(tmp_path, OK_SPEC + 'timeout_sec = 7\n',
                                     timeout_sec=3)
        assert got == 3.0

    def test_global_default_when_spec_omits(self, tmp_path):
        assert self._captured_timeout(tmp_path, OK_SPEC) == 3600.0


class TestGuards:
    def test_unknown_task_returns_2_no_row(self, tmp_path):
        decl = _decl(tmp_path, OK_SPEC)
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        rc = main(['nope'], declarations=decl, recorder=rec, alerter=_Alerts(),
                  runner=_Runner([0]), sleeper=lambda s: None)
        assert rc == 2
        assert _rows(tmp_path / 'spool') == []

    def test_record_failure_does_not_fail_run(self, tmp_path, monkeypatch):
        # 紅線：紀錄寫入失敗不得讓任務失敗
        decl = _decl(tmp_path, OK_SPEC)
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        monkeypatch.setattr(rec, 'write',
                            lambda *a, **k: (_ for _ in ()).throw(OSError('db down')))
        rc = main(['a'], declarations=decl, recorder=rec, alerter=_Alerts(),
                  runner=_Runner([0]), sleeper=lambda s: None)
        assert rc == 0

    def test_hermes_kind_refused_in_shell(self, tmp_path):
        # 外殼只跑 exec；其他 kind 屬 B1 搬家範圍（紅線：零搬家）
        decl = _decl(tmp_path, """
id = "a"
kind = "hermes"
hermes_job = "abc123"
trigger = { type = "cron", expr = "0 10 * * *", tz = "Asia/Taipei" }
safety = { writes_db = false, idempotent = true }
""")
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        rc = main(['a'], declarations=decl, recorder=rec, alerter=_Alerts(),
                  runner=_Runner([0]), sleeper=lambda s: None)
        assert rc == 3
        assert _rows(tmp_path / 'spool') == []


class TestCheckMissed:
    def test_check_missed_tick_writes_heartbeat(self, tmp_path):
        decl = _decl(tmp_path, OK_SPEC)
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        rc = main(['--check-missed'], declarations=decl, recorder=rec,
                  alerter=_Alerts(), runner=_Runner([]), sleeper=lambda s: None)
        assert rc == 0
        from funlab.sched.missed import HEARTBEAT_TASK_ID
        assert any(r['task_id'] == HEARTBEAT_TASK_ID for r in _rows(tmp_path / 'spool'))
