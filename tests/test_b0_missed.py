"""missed／heartbeat／watchdog 測試（卡 t_bdf4eb9c 工項⑤）。

期望值獨立來源：§3.5-4「應跑時刻已過且無紀錄→missed 告警」＋§3.3 heartbeat 欄；
cron  firing 以手算（每日 10:00 Asia/Taipei）為準。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from funlab.sched.manifest import TaskSpec
from funlab.sched.missed import (HEARTBEAT_TASK_ID, SpoolStore, Watchdog,
                                 expected_runs, find_missed, find_silence)
from funlab.sched.runrec import RunRecord, RunRecorder

TPE = timezone(timedelta(hours=8))


def _dt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TPE)


def _spec(id='gate1', trigger=None, enabled=True, lifecycle=None, heartbeat=None,
          kind='inproc'):
    # kind 預設 inproc＝「終態會落 sched_run」的納管任務（F1(b) 裁決，
    # manager 2026-11-15 採 reviewer 路線 (b)）；非納管 kind 見 TestScopeCoverage。
    return TaskSpec(id=id, owner='dev-ops', kind=kind, entry='true',
                    trigger=trigger, enabled=enabled,
                    lifecycle=lifecycle, heartbeat=heartbeat,
                    safety={'writes_db': False, 'idempotent': True})


DAILY_10 = {'type': 'cron', 'expr': '0 10 * * *', 'tz': 'Asia/Taipei'}


class TestExpectedRuns:
    def test_cron_daily_hand_computed(self):
        # 手算：每日 10:00 → 11-15 10:00、11-16 10:00（窗端點含頭不含尾）
        runs = expected_runs(_spec(trigger=DAILY_10), _dt(2026, 11, 15), _dt(2026, 11, 17))
        assert [r.isoformat() for r in runs] == ['2026-11-15T10:00:00+08:00',
                                                 '2026-11-16T10:00:00+08:00']

    def test_plan_and_missing_trigger_yield_empty(self):
        # plan＝動態排程靜態不可判（由 EVENT_JOB_MISSED 覆蓋）；hermes 未宣告 trigger 同理
        assert expected_runs(_spec(trigger={'type': 'plan'}), _dt(2026, 11, 15), _dt(2026, 11, 16)) == []
        assert expected_runs(_spec(trigger=None), _dt(2026, 11, 15), _dt(2026, 11, 16)) == []

    def test_invalid_cron_expr_does_not_raise(self):
        assert expected_runs(_spec(trigger={'type': 'cron', 'expr': 'bogus'}),
                             _dt(2026, 11, 15), _dt(2026, 11, 16)) == []


class _DictStore:
    """#5633 口徑替身：rows=覆蓋列（scheduled_at）；oks=最近成功時刻。"""
    def __init__(self, oks=None, rows=None):
        self.oks = oks or {}
        self.rows = rows or {}

    def last_ok(self, task_id):
        return self.oks.get(task_id)

    def has_row_at_or_after(self, task_id, when):
        return any(t >= when for t in self.rows.get(task_id, []))


class TestFindMissed:
    def test_due_with_no_record_flagged(self):
        # #5633：昨日已有列、今日 10:00 應跑逾期且窗內無列 → 逐 fire 列單只剩今日這張
        store = _DictStore(rows={'a': [_dt(2026, 11, 14, 10)]})
        got = find_missed({'a': _spec(id='a', trigger=DAILY_10)}, store,
                          now=_dt(2026, 11, 15, 12), grace_sec=900)
        assert len(got) == 1
        spec, expected = got[0]
        assert spec.id == 'a' and expected.isoformat() == '2026-11-15T10:00:00+08:00'

    def test_ok_record_after_due_not_flagged(self):
        store = _DictStore(rows={'a': [_dt(2026, 11, 15, 10, 1)]})
        assert find_missed({'a': _spec(id='a', trigger=DAILY_10)}, store,
                           now=_dt(2026, 11, 15, 12), grace_sec=900) == []

    def test_previously_written_missed_record_suppresses(self):
        # 本工具自己寫的 miss 回寫單（scheduled_at=應跑時刻）也是列 → 下輪不重複告警
        store = _DictStore(rows={'a': [_dt(2026, 11, 15, 10, 0)]})
        assert find_missed({'a': _spec(id='a', trigger=DAILY_10)}, store,
                           now=_dt(2026, 11, 15, 11), grace_sec=900) == []

    def test_within_grace_not_flagged(self):
        # 昨日列已在 ⇒ 唯一剩的今日 10:00 應跑落寬限 900s 內 → 不告警
        store = _DictStore(rows={'a': [_dt(2026, 11, 14, 10)]})
        assert find_missed({'a': _spec(id='a', trigger=DAILY_10)}, store,
                           now=_dt(2026, 11, 15, 10, 10), grace_sec=900) == []

    def test_stale_row_before_due_does_not_cover(self):
        # 前一日 09:50 的列（早於該日 10:00 fire）不得掩蓋任何應跑時刻
        store = _DictStore(rows={'a': [_dt(2026, 11, 14, 9, 50)]})
        got = find_missed({'a': _spec(id='a', trigger=DAILY_10)}, store,
                          now=_dt(2026, 11, 15, 12), grace_sec=900)
        assert [e.isoformat() for _, e in got] == [
            '2026-11-14T10:00:00+08:00', '2026-11-15T10:00:00+08:00']

    def test_disabled_and_expired_once_skipped(self):
        off = _spec(trigger=DAILY_10, enabled=False)
        once = _spec(trigger=DAILY_10, lifecycle={'kind': 'once',
                                                   'expires_at': '2026-11-14T00:00:00+08:00'})
        specs = {'off': off, 'once': once, 'plan': _spec(trigger={'type': 'plan'})}
        assert find_missed(specs, _DictStore(), now=_dt(2026, 11, 15, 12),
                           grace_sec=900) == []


class TestScopeCoverage:
    """F1(b) 裁決（manager 2026-11-15 採 reviewer 路線 (b)）：判定範圍＝終態會落
    sched_run 的任務（誰跑誰記錄、誰記錄誰受漏跑判定）。crontab 直跑的 exec／
    hermes／os 於 B0 無寫入路徑 → missed／silence 皆零誤報（覆核探針實據：
    2 日窗 26 張假單／13 任務，假單直進 append-only 表清不掉）。
    """

    def test_exec_hermes_os_never_missed(self):
        specs = {'e': _spec(id='e', kind='exec', trigger=DAILY_10),
                 'h': _spec(id='h', kind='hermes', trigger=DAILY_10),
                 'o': _spec(id='o', kind='os', trigger=DAILY_10)}
        got = find_missed(specs, _DictStore(), now=_dt(2026, 11, 15, 12),
                          grace_sec=900)
        assert got == []

    def test_exec_hermes_os_never_silence(self):
        hb = {'max_silence_hours': 26}
        specs = {'e': _spec(id='e', kind='exec', heartbeat=hb),
                 'h': _spec(id='h', kind='hermes', heartbeat=hb)}
        assert find_silence(specs, _DictStore(), now=_dt(2026, 11, 15, 12)) == []

    def test_inproc_still_in_scope(self):
        # 排程器實際承載的 inproc 照舊受漏跑判定（範圍縮小≠漏跑偵測失效）
        got = find_missed({'a': _spec(id='a', kind='inproc', trigger=DAILY_10)},
                          _DictStore(), now=_dt(2026, 11, 15, 12), grace_sec=900)
        assert got and all(s.id == 'a' for s, _ in got)

    def test_shipped_snapshot_zero_false_missed(self):
        # reviewer F1 釘選測：真實 schedule/tasks.d 快照＋空 store＋代表時刻
        # （週一 14:00 TPE）→ 全零誤報。修正前受審態＝2 日窗 26 張假單。
        from funlab.sched.manifest import load_dir
        snap = Path(__file__).resolve().parents[1] / 'schedule' / 'tasks.d'
        specs, errors = load_dir(snap)
        assert errors == []
        # 防空轉假綠：快照必須含足量「enabled＋cron trigger 的 exec 宣告」
        # （即覆核探針命中的假單來源族）。
        guards = [s for s in specs.values()
                  if s.kind == 'exec' and s.enabled
                  and (s.trigger or {}).get('type') == 'cron']
        assert len(guards) >= 10
        got = find_missed(specs, _DictStore(), now=_dt(2026, 11, 16, 14),
                          grace_sec=900)
        assert [(s.id, e.isoformat()) for s, e in got] == []


class TestFindSilence:
    def test_over_max_silence_flagged(self):
        spec = _spec(id='a', heartbeat={'max_silence_hours': 26})
        store = _DictStore(oks={'a': _dt(2026, 11, 13, 10)})   # 30h 前
        got = find_silence({'a': spec}, store, now=_dt(2026, 11, 14, 16))
        assert [s.id for s in got] == ['a']

    def test_within_silence_ok(self):
        spec = _spec(id='a', heartbeat={'max_silence_hours': 26})
        store = _DictStore(oks={'a': _dt(2026, 11, 14, 6)})    # 10h 前
        assert find_silence({'a': spec}, store, now=_dt(2026, 11, 14, 16)) == []

    def test_never_run_flagged_and_no_heartbeat_key_skipped(self):
        spec = _spec(id='a', heartbeat={'max_silence_hours': 26})
        plain = _spec(id='b')
        got = find_silence({'a': spec, 'b': plain}, _DictStore(),
                           now=_dt(2026, 11, 15, 12))
        assert [s.id for s in got] == ['a']


class TestWatchdog:
    def _mk(self, tmp_path, specs, now_box):
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        alerts = []
        class _A:
            def send(self, task_id, event, message, **kw):
                alerts.append((task_id, event))
                return True
        wd = Watchdog(specs_provider=lambda: specs, recorder=rec, alerter=_A(),
                      spool_dir=tmp_path / 'spool',
                      clock=lambda: now_box[0],
                      heartbeat_sec=900, interval_sec=60, grace_sec=900)
        return wd, alerts

    def _rows(self, tmp_path):
        rows = []
        import json
        for f in (tmp_path / 'spool').glob('*.jsonl'):
            rows += [json.loads(l) for l in f.read_text().splitlines()]
        return rows

    def test_tick_writes_heartbeat_then_rate_limited(self, tmp_path):
        now_box = [datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)]
        wd, alerts = self._mk(tmp_path, {}, now_box)
        wd.tick()
        assert any(r['task_id'] == HEARTBEAT_TASK_ID and r['status'] == 'ok'
                   for r in self._rows(tmp_path))
        n1 = len(self._rows(tmp_path))
        wd.tick()                      # 同窗再 tick＝心跳不重複
        assert len(self._rows(tmp_path)) == n1
        now_box[0] += timedelta(seconds=901)
        wd.tick()
        assert len(self._rows(tmp_path)) == n1 + 1

    def test_tick_missed_writes_record_and_alerts_once(self, tmp_path):
        now_box = [datetime(2026, 11, 15, 4, 0, tzinfo=timezone.utc)]  # 12:00 TPE
        specs = {'a': _spec(id='a', trigger=DAILY_10)}
        wd, alerts = self._mk(tmp_path, specs, now_box)
        wd.tick()
        assert ('a', 'missed') in alerts
        rows = self._rows(tmp_path)
        assert any(r['task_id'] == 'a' and r['status'] == 'missed' for r in rows)
        # 下一輪：spool 已有 miss 回寫單 → 不重複告警（心跳 alert 不計）
        alerts.clear()
        wd.tick()
        assert alerts == []

    def test_tick_never_raises(self, tmp_path):
        now_box = [datetime(2026, 11, 15, 4, 0, tzinfo=timezone.utc)]

        def boom():
            raise RuntimeError('spec dir gone')
        rec = RunRecorder(spool_dir=tmp_path / 'spool')
        wd = Watchdog(specs_provider=boom, recorder=rec, alerter=None,
                      clock=lambda: now_box[0])
        wd.tick()   # 不拋即過
