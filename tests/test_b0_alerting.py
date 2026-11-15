"""alerting 測試（卡 t_bdf4eb9c 工項⑤；報告 §3.5-3＋紅線「告警先導向測試頻道」）。

hermes argv 期望逐字＝fi_guards.send_alert 範式（finfun-finfetch
finfun/finfetch/fi_guards.py:248 `["hermes","-p",profile,"send","-t",target,
"-s",subject, body]`，獨立來源）。
"""
import json

from funlab.sched.alerting import Alerter


class _Res:
    def __init__(self, rc=0, err=''):
        self.returncode, self.stderr = rc, err


class _RunRecorder:
    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return _Res(self.rc)


class TestLogChannel:
    """紅線：告警先導向測試頻道——默認 log channel 只落測試檔，不外送。"""

    def test_default_channel_appends_test_log_and_no_subprocess(self, tmp_path):
        runner = _RunRecorder()
        a = Alerter(channel='log', state_dir=tmp_path, runner=runner)
        assert a.send('gate1', 'failed', '閘1 缺口 3 筆') is True
        assert runner.calls == []  # 零外呼
        log = (tmp_path / 'sched_alerts_test.log').read_text(encoding='utf-8')
        assert 'gate1' in log and 'failed' in log and '閘1 缺口 3 筆' in log

    def test_test_named_channel_also_local(self, tmp_path):
        a = Alerter(channel='test', state_dir=tmp_path, runner=_RunRecorder())
        assert a.send('t', 'missed', 'x') is True


class TestHermesChannel:
    def test_hermes_argv_matches_fi_guards_paradigm(self, tmp_path):
        runner = _RunRecorder()
        a = Alerter(channel='hermes:fund13-dev-ops:telegram:-1004368128611',
                    state_dir=tmp_path, runner=runner)
        ok = a.send('fi_guards.gate1', 'failed', '正文內容')
        assert ok is True
        (cmd, kw), = runner.calls
        assert cmd == ['hermes', '-p', 'fund13-dev-ops', 'send',
                       '-t', 'telegram:-1004368128611',
                       '-s', '[sched] fi_guards.gate1 failed', '正文內容']
        assert kw.get('timeout') == 60

    def test_delivery_failure_returns_false_never_raises(self, tmp_path):
        a = Alerter(channel='hermes:p:t', state_dir=tmp_path, runner=_RunRecorder(rc=1))
        assert a.send('t', 'failed', 'x') is False

    def test_runner_exception_swallowed(self, tmp_path):
        def boom(cmd, **kw):
            raise OSError('hermes not found')
        a = Alerter(channel='hermes:p:t', state_dir=tmp_path, runner=boom)
        assert a.send('t', 'failed', 'x') is False


class TestDedupe:
    def _clock(self):
        t = {'now': 1_000_000.0}
        return t, (lambda: t['now'])

    def test_same_task_same_event_suppressed_within_window(self, tmp_path):
        t, clock = self._clock()
        a = Alerter(channel='log', state_dir=tmp_path, clock=clock,
                    dedupe_hours=6.0)
        assert a.send('x', 'failed', 'e1') is True
        t['now'] += 5 * 3600
        assert a.send('x', 'failed', 'e1-重複') is False   # 6h 內同症狀＝壓制
        t['now'] += 2 * 3600
        assert a.send('x', 'failed', 'e1-過窗') is True    # 8h 後可再發

    def test_different_event_not_suppressed(self, tmp_path):
        _, clock = self._clock()
        a = Alerter(channel='log', state_dir=tmp_path, clock=clock)
        assert a.send('x', 'failed', 'a') is True
        assert a.send('x', 'missed', 'b') is True

    def test_dedupe_state_persists_across_processes(self, tmp_path):
        t, clock = self._clock()
        Alerter(channel='log', state_dir=tmp_path, clock=clock).send('x', 'failed', 'a')
        # 外殼每分鐘起新進程：去重必須跨進程
        a2 = Alerter(channel='log', state_dir=tmp_path, clock=clock)
        assert a2.send('x', 'failed', 'dup') is False
