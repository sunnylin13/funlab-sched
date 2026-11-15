"""漏跑偵測＋靜默（dead-man）＋排程層心跳（卡 t_bdf4eb9c 工項⑤；報告 §3.5-4/§3.5-5）。

形制：
- 漏跑（§3.5-4）：比對宣告檔 cron 應跑時刻與紀錄庫（spool/PG），
  「應跑已過＋寬限期外＋無任何終態紀錄」→ 寫 missed 紀錄＋告警。
  自己寫的 missed 單即成下一輪的抑制紀錄（免洗版）。
- 靜默：宣告 `heartbeat.max_silence_hours` 逾期無成功紀錄 → silence 告警
  （G3 晨巡 dead-man 的站內第一線；Hermes 第二線讀 v_sched_last_run）。
- 心跳：排程層活着就定期寫 `task_id=sched_daemon_heartbeat` 的 ok 紀錄；
  排程層死亡 → 心跳停 → 外部 dead-man 可見。
- plan 動態排程任務不在靜態推算範圍（由 APScheduler EVENT_JOB_MISSED 覆蓋）。
- Watchdog.tick 全自吸例外：監督者不得把被監督者進程拖死。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from funlab.sched.manifest import TaskSpec
from funlab.sched.runrec import RunRecord, RunRecorder, SpoolSink, SPOOL_SUBDIR, \
    default_state_dir

HEARTBEAT_TASK_ID = 'sched_daemon_heartbeat'
DEFAULT_GRACE_SEC = 900.0        # §3.3 misfire.grace_sec 範例值
DEFAULT_HEARTBEAT_SEC = 900.0
DEFAULT_INTERVAL_SEC = 60.0


def _as_utc(dt_: datetime) -> datetime:
    if dt_.tzinfo is None:
        return dt_.replace(tzinfo=timezone.utc)
    return dt_.astimezone(timezone.utc)


def expected_runs(spec: TaskSpec, window_start: datetime,
                  window_end: datetime) -> list[datetime]:
    """宣告檔 cron trigger 在窗口內的應跑時刻（含頭不含尾）。

    plan／無 trigger／非法式＝回空清單（靜態不可判，非錯誤）。
    """
    trig = spec.trigger or {}
    if trig.get('type') != 'cron' or not trig.get('expr'):
        return []
    try:
        from apscheduler.triggers.cron import CronTrigger
        tz = trig.get('tz', 'Asia/Taipei')
        trigger = CronTrigger.from_crontab(trig['expr'], timezone=tz)
        out: list[datetime] = []
        prev = None
        cursor = _as_utc(window_start)
        # get_next_fire_time(previous_fire_time, now)：從 window_start 起逐次推進
        nxt = trigger.get_next_fire_time(None, cursor)
        while nxt is not None and _as_utc(nxt) < _as_utc(window_end):
            out.append(nxt)
            prev, cursor = nxt, nxt + timedelta(microseconds=1)
            nxt = trigger.get_next_fire_time(prev, cursor)
        return out
    except Exception:
        return []


class SpoolStore:
    """JSONL spool ＝紀錄倉（降級期間的事實來源）。

    覆蓋口徑（裁決 #5633）：漏跑判定以「scheduled_at 已過＋寬限窗內該 task_id
    無任何列」為準——列的 scheduled_at 即該次 cron fire 的標記。
    """

    def __init__(self, spool_dir: Path | str):
        self.dir = Path(spool_dir)

    def _iter_rows(self):
        sink = SpoolSink(self.dir)
        import json
        for f in sink.files():
            for line in f.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue

    def _row_time(self, row, keys=('scheduled_at', 'finished_at')):
        for k in keys:
            v = row.get(k)
            if v:
                try:
                    return _as_utc(datetime.fromisoformat(v))
                except Exception:
                    continue
        return None

    def last_ok(self, task_id: str):
        """該 task 最近一次 status=ok 列的 finished_at（UTC）| None。"""
        best = None
        for row in self._iter_rows():
            if row.get('task_id') != task_id or row.get('status') != 'ok':
                continue
            t = self._row_time(row, ('finished_at',))
            if t and (best is None or t > best):
                best = t
        return best

    def has_row_at_or_after(self, task_id: str, when: datetime) -> bool:
        """寬限窗覆蓋判定：存在 scheduled_at（缺欄回落 finished_at）≥ when 的列。"""
        w = _as_utc(when)
        for row in self._iter_rows():
            if row.get('task_id') != task_id:
                continue
            t = self._row_time(row)
            if t is not None and t >= w:
                return True
        return False


class PgStore:
    """表在位時的紀錄倉查詢路徑（唯讀；同 #5633 覆蓋口徑）。"""

    def __init__(self, engine):
        self._engine = engine

    def last_ok(self, task_id: str):
        from sqlalchemy import text
        stmt = text("select max(finished_at) from sched_run "
                    "where task_id = :t and status = 'ok'")
        try:
            with self._engine.connect() as c:
                v = c.execute(stmt, {'t': task_id}).scalar()
        except Exception:
            return None
        return _as_utc(v) if v is not None else None

    def has_row_at_or_after(self, task_id: str, when: datetime) -> bool:
        from sqlalchemy import text
        stmt = text("select 1 from sched_run where task_id = :t "
                    "and coalesce(scheduled_at, finished_at) >= :w limit 1")
        try:
            with self._engine.connect() as c:
                return c.execute(stmt, {'t': task_id, 'w': when}).first() is not None
        except Exception:
            return False


def _live_specs(specs: dict[str, TaskSpec], now: datetime) -> dict[str, TaskSpec]:
    out = {}
    for sid, s in specs.items():
        if not s.enabled:
            continue
        lc = s.lifecycle or {}
        if lc.get('kind') == 'once' and lc.get('expires_at'):
            try:
                if datetime.fromisoformat(str(lc['expires_at'])) < now:
                    continue   # 一次性已過期＝生命週期自然終止
            except Exception:
                pass
        out[sid] = s
    return out


def find_missed(specs: dict[str, TaskSpec], store, *, now: datetime,
                grace_sec: float = DEFAULT_GRACE_SEC) -> list[tuple[TaskSpec, datetime]]:
    """「scheduled_at 已過（含寬限）且窗內無任何列」（裁決 #5633 口徑）。

    回 [(spec, 應跑時刻)]。
    """
    out = []
    horizon = now - timedelta(seconds=grace_sec)
    for spec in _live_specs(specs, now).values():
        runs = expected_runs(spec, now - timedelta(days=2), now)
        due = [r for r in runs if _as_utc(r) <= _as_utc(horizon)]
        # 逐 fire 判定：每次逾期且窗內無列的應跑時刻各列一單
        for expected in due:
            if not store.has_row_at_or_after(spec.id, _as_utc(expected)):
                out.append((spec, expected))
    return out


def find_silence(specs: dict[str, TaskSpec], store, *,
                 now: datetime) -> list[TaskSpec]:
    """heartbeat.max_silence_hours 逾期無成功紀錄 → dead-man 靜默清單。"""
    out = []
    for spec in _live_specs(specs, now).values():
        limit_h = spec.max_silence_hours
        if limit_h is None:
            continue
        last_ok = store.last_ok(spec.id)
        if last_ok is not None and _as_utc(now) - last_ok <= timedelta(hours=limit_h):
            continue
        out.append(spec)
    return out


class Watchdog:
    """排程層內部巡邏：心跳＋漏跑＋靜默。SchedService 緒／外殼 --check-missed 共用 tick。"""

    def __init__(self, *, specs_provider: Callable[[], dict[str, TaskSpec]],
                 recorder: RunRecorder, alerter=None,
                 spool_dir: Path | str | None = None,
                 clock: Callable[[], datetime] | None = None,
                 heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC,
                 interval_sec: float = DEFAULT_INTERVAL_SEC,
                 grace_sec: float = DEFAULT_GRACE_SEC,
                 logger=None):
        self.specs_provider = specs_provider
        self.recorder = recorder
        self.alerter = alerter
        self.spool_dir = Path(spool_dir) if spool_dir is not None \
            else default_state_dir() / SPOOL_SUBDIR
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.heartbeat_sec = float(heartbeat_sec)
        self.interval_sec = float(interval_sec)
        self.grace_sec = float(grace_sec)
        self.logger = logger
        self._last_heartbeat: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- store：表在位走 PG，否則 spool ------------------------------------
    def _store(self):
        sink = getattr(self.recorder, 'db_sink', None)
        if sink is not None and sink.available:
            eng = getattr(sink, '_engine', None)
            if eng is not None:
                return PgStore(eng)
        return SpoolStore(self.spool_dir)

    def tick(self) -> None:
        """一輪巡邏（全自吸）。"""
        try:
            now = self.clock()
            if self._last_heartbeat is None or \
                    (now - self._last_heartbeat).total_seconds() >= self.heartbeat_sec:
                self.recorder.write(RunRecord(
                    task_id=HEARTBEAT_TASK_ID, status='ok', trigger_kind='cron',
                    scheduled_at=now, started_at=now, finished_at=now))
                self._last_heartbeat = now
            specs = self.specs_provider()
            store = self._store()
            for spec, expected in find_missed(specs, store, now=now,
                                              grace_sec=self.grace_sec):
                self.recorder.write(RunRecord(
                    task_id=spec.id, status='missed', trigger_kind='cron',
                    scheduled_at=expected, finished_at=now,
                    error_summary=f'漏跑偵測：應跑 {expected.isoformat()} 後無任何紀錄'))
                self._alert(spec.id, 'missed',
                            f'{spec.id} 漏跑：應跑 {expected.isoformat()}（tz 依宣告）無紀錄。'
                            f'owner={spec.owner} legacy_ref={spec.legacy_ref}')
            for spec in find_silence(specs, store, now=now):
                self._alert(spec.id, 'silence',
                            f'{spec.id} 超過 heartbeat.max_silence_hours='
                            f'{spec.max_silence_hours} 無成功紀錄。owner={spec.owner}')
        except Exception as e:
            self._log('warning', f'[watchdog] tick 例外（已自吸）: {e}')

    def _alert(self, task_id: str, event: str, message: str) -> None:
        if self.alerter is None:
            return
        try:
            self.alerter.send(task_id, event, message)
        except Exception as e:
            self._log('warning', f'[watchdog] 告警例外: {e}')

    # -- 緒管理 --------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name='sched-watchdog',
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            self.tick()

    def stop(self) -> None:
        self._stop.set()

    def _log(self, level: str, msg: str) -> None:
        if self.logger is not None:
            try:
                getattr(self.logger, level)(msg)
            except Exception:
                pass
