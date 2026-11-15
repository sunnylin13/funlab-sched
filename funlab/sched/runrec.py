"""sched_run 執行紀錄鏈（卡 t_bdf4eb9c 工項②；報告 §3.4/§3.5）。

形制（釘選）：
- **一執行一終態紀錄**：ok/failed/timeout/missed/skipped/retrying 單筆追加
  （append-only，不改寫）。EXECUTED/ERROR/MISSED 事件直接落行；SUBMITTED 只在
  記憶體記 started_at 供配對，不落行（卡面驗收1：`sched_run` 筆數＝journal
  `Executed` 筆數，容差 0＝終態單筆口徑）。
- **碼零依賴表存在**（實查閘 2026-11-15：正式庫 sched_run ABSENT）：PGSink 探測
  缺席→快取（TTL 600s，C2 DDL t_79671c59 落地後自動回升）→ RunRecorder 降級
  JSONL spool（`<state>/sched_spool/YYYY-MM-DD.jsonl`）；`replay_spool()` 供
  落地後補入。
- **紀錄寫入失敗不得讓任務失敗**（卡面紅線）：本模組一切公開寫入路徑自吸例外，
  最壞回傳 'dropped'。
- 欄位名逐字＝報告 §3.4 欄位表（C2 DDL 正本依同表，兩側走樣＝部署閘抓）。
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from apscheduler.events import (EVENT_JOB_ERROR, EVENT_JOB_EXECUTED,
                                EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED,
                                JobExecutionEvent, JobSubmissionEvent)

# status 六值＝正式庫 CHECK 枚舉（覆核 #5636；裁決 #5633：不含 running）
TERMINAL_STATUSES = {'ok', 'failed', 'timeout', 'missed', 'skipped', 'retrying'}
ERROR_SUMMARY_LIMIT = 500   # §3.4：錯誤摘要限 500 字，不含 secrets
SPOOL_SUBDIR = 'sched_spool'
SPOOL_DONE_SUBDIR = 'sched_spool_done'
PG_PROBE_TTL_SEC = 600.0

COLUMNS = ('run_id', 'task_id', 'scheduled_at', 'started_at', 'finished_at',
           'status', 'attempt', 'trigger_kind', 'exit_code', 'error_summary',
           'git_sha', 'host', 'log_ref')


def default_state_dir() -> Path:
    return Path(os.environ.get('FUND13_STATE_DIR',
                               '~/.local/state/fund13')).expanduser()


def truncate_error(text_, limit: int = ERROR_SUMMARY_LIMIT) -> str | None:
    if text_ is None:
        return None
    s = str(text_)
    return s if len(s) <= limit else s[:limit]


def _iso(dt_) -> str | None:
    return dt_.isoformat(timespec='seconds') if dt_ else None


def _parse_iso(s: str | None):
    return datetime.fromisoformat(s) if s else None


@dataclass
class RunRecord:
    """§3.4 欄位表的載體。時間一律 tz-aware datetime（序列化＝isoformat）。"""
    task_id: str
    status: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempt: int = 1
    trigger_kind: str = 'cron'     # cron/manual/retry/dependency/heartbeat
    exit_code: int | None = None
    error_summary: str | None = None
    git_sha: str | None = None
    host: str | None = None
    log_ref: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ('scheduled_at', 'started_at', 'finished_at'):
            d[k] = _iso(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'RunRecord':
        d = dict(d)
        for k in ('scheduled_at', 'started_at', 'finished_at'):
            d[k] = _parse_iso(d.get(k))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class SpoolSink:
    """JSONL 降級暫存（§3.4：`~/.local/state/fund13/sched_spool/`）。"""

    def __init__(self, directory: Path | str):
        self.dir = Path(directory)

    def write(self, record: RunRecord) -> None:
        day = (record.finished_at or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / f'{day}.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + '\n')

    def files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(self.dir.glob('*.jsonl'))


class PGSink:
    """正式庫 sched_run INSERT。表缺席/DB 不可用＝拋 RunRecordUnavailable，
    由 RunRecorder 降級 spool；缺席判定快取 TTL 600s（C2 落地自動回升）。

    url_provider 延遲取用（web 進程＝dbmgr；外殼＝env）；engine_factory 注入口
    （測試以 sqlite 建同名表模擬 DDL 落地態）。
    """

    def __init__(self, url_provider: Callable[[], str | None], *,
                 table: str = 'sched_run',
                 engine_factory: Callable[[str], object] | None = None,
                 probe_ttl: float = PG_PROBE_TTL_SEC):
        self._url_provider = url_provider
        self._table = table
        self._engine_factory = engine_factory
        self._probe_ttl = probe_ttl
        self._engine = None
        self._lock = threading.Lock()
        self._available = False
        self._probed_at = 0.0

    # -- 可用性探測 -------------------------------------------------------
    @property
    def available(self) -> bool:
        with self._lock:
            if time.monotonic() - self._probed_at > self._probe_ttl:
                self._probe_locked()
            return self._available

    def reset_availability(self) -> None:
        """强制重探（replay 前／測試模擬 DDL 落地）。"""
        with self._lock:
            self._probe_locked()

    def _engine_locked(self):
        if self._engine is None:
            url = self._url_provider()
            if not url:
                return None
            if self._engine_factory is not None:
                self._engine = self._engine_factory(url)
            else:
                from sqlalchemy import create_engine
                self._engine = create_engine(url, pool_pre_ping=True,
                                             execution_options={'readonly': False})
        return self._engine

    def _probe_locked(self) -> None:
        self._probed_at = time.monotonic()
        try:
            eng = self._engine_locked()
            if eng is None:
                self._available = False
                return
            from sqlalchemy import text
            with eng.connect() as c:
                c.execute(text(f'select 1 from {self._table} limit 1'))
            self._available = True
        except Exception:
            self._available = False

    # -- 寫入 -------------------------------------------------------------
    def write(self, record: RunRecord) -> None:
        with self._lock:
            eng = self._engine_locked()
            if eng is None:
                raise RunRecordUnavailable('db url unavailable')
        from sqlalchemy import text
        payload = {k: getattr(record, k) for k in COLUMNS}
        stmt = text(f"insert into {self._table} "
                    f"({', '.join(COLUMNS)}) values "
                    f"({', '.join(':' + c for c in COLUMNS)})")
        try:
            with eng.begin() as c:
                c.execute(stmt, payload)
        except Exception as e:
            # 表缺席/連線斷＝降級信號；快取重探時點交還 TTL
            with self._lock:
                self._available = False
                self._probed_at = time.monotonic()
            raise RunRecordUnavailable(str(e)) from e


class RunRecordUnavailable(Exception):
    """PG 路徑不可用（降級信號，非任務錯誤）。"""


class RunRecorder:
    """終態紀錄唯一出口：PG 在位走表，否則（或寫入失敗）走 JSONL spool。

    write() 永不拋——最壞回傳 'dropped'（卡面紅線：紀錄不拖垮任務）。
    """

    def __init__(self, *, spool_dir: Path | str | None = None,
                 db_sink: PGSink | None = None,
                 logger=None, git_sha: str | None = None, host: str | None = None):
        self.spool = SpoolSink(spool_dir if spool_dir is not None
                               else default_state_dir() / SPOOL_SUBDIR)
        self.db_sink = db_sink
        self.logger = logger
        self.git_sha = git_sha
        self.host = host or socket.gethostname()

    def _log(self, level: str, msg: str) -> None:
        if self.logger is not None:
            try:
                getattr(self.logger, level)(msg)
            except Exception:
                pass

    def write(self, record: RunRecord) -> str:
        try:
            if record.git_sha is None:
                record.git_sha = self.git_sha
            if record.host is None:
                record.host = self.host
            record.error_summary = truncate_error(record.error_summary)
            if self.db_sink is not None and self.db_sink.available:
                try:
                    self.db_sink.write(record)
                    return 'pg'
                except RunRecordUnavailable as e:
                    self._log('warning', f'[runrec] PG 寫入降級 spool: {e}')
                except Exception as e:
                    self._log('warning', f'[runrec] PG 寫入例外降級 spool: {e}')
            self.spool.write(record)
            return 'spool'
        except Exception as e:  # spool 也壞＝棄單不拋（紅線）
            self._log('error', f'[runrec] 紀錄降級失敗，已棄單: {e}')
            return 'dropped'

    def replay_spool(self) -> dict:
        """把 spool 暫存補入 PG（C2 落地後的匯入路徑）。全檔成功→移入 done 目錄。"""
        stats = {'replayed': 0, 'failed': 0}
        if self.db_sink is None:
            return stats
        self.db_sink.reset_availability()
        if not self.db_sink.available:
            return stats
        for f in self.spool.files():
            ok_lines, bad_lines = [], []
            for line in f.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                try:
                    self.db_sink.write(RunRecord.from_dict(json.loads(line)))
                    stats['replayed'] += 1
                    ok_lines.append(line)
                except Exception:
                    stats['failed'] += 1
                    bad_lines.append(line)
            try:
                if not bad_lines:
                    done = self.spool.dir / SPOOL_DONE_SUBDIR
                    done.mkdir(parents=True, exist_ok=True)
                    f.rename(done / f.name)
                else:
                    f.write_text('\n'.join(bad_lines) + '\n', encoding='utf-8')
            except Exception as e:
                self._log('error', f'[runrec] spool 檔收尾失敗 {f}: {e}')
        return stats


class SchedRunRecorder:
    """APScheduler 事件監聽器：覆蓋掛上排程器的全部任務（inproc 全 22 啟用）。

    事件映射（§2.3 journal `Executed` 同源＝同一事件流）：
      SUBMITTED → 記憶體記 started_at（不落行）
      EXECUTED  → status ok      ERROR → failed（error_summary＝例外摘要）
      MISSED    → missed         `_M` 後綴＝手動（trigger_kind=manual，去後綴還原 task_id）
    自我吸收（SCH-13）：本監聽器在 APScheduler 事件線執行，任何例外只記日誌。
    """

    MASK = EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED

    def __init__(self, recorder: RunRecorder, *, logger=None,
                 clock: Callable[[], datetime] | None = None,
                 alert_fn: Callable[[RunRecord], None] | None = None):
        self.recorder = recorder
        self.logger = logger
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.alert_fn = alert_fn
        self._starts: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def attach(self, scheduler) -> None:
        scheduler.add_listener(self.on_event, self.MASK)

    def on_event(self, event) -> None:
        try:
            self._on_event(event)
        except Exception as e:  # SCH-13：事件線禁物——只記日誌
            if self.logger is not None:
                try:
                    self.logger.warning(f'[runrec] listener error on {event!r}: '
                                        f'{type(e).__name__}: {e}')
                except Exception:
                    pass

    def _on_event(self, event) -> None:
        if isinstance(event, JobSubmissionEvent):
            if event.code == EVENT_JOB_SUBMITTED:
                with self._lock:
                    self._starts[event.job_id] = self.clock()
            return
        if not isinstance(event, JobExecutionEvent):
            return
        is_manual = event.job_id.endswith('_M')
        task_id = event.job_id.removesuffix('_M')
        if event.code == EVENT_JOB_MISSED:
            status, err = 'missed', None
        elif getattr(event, 'exception', None):
            status = 'failed'
            err = f"{type(event.exception).__name__}: {event.exception}"
        else:
            status, err = 'ok', None
        with self._lock:
            started = self._starts.pop(event.job_id, None)
        record = RunRecord(
            task_id=task_id, status=status,
            scheduled_at=getattr(event, 'scheduled_run_time', None),
            started_at=started, finished_at=self.clock(),
            trigger_kind='manual' if is_manual else 'cron',
            error_summary=err)
        self.recorder.write(record)
        if self.alert_fn is not None and status in ('failed', 'missed'):
            try:
                self.alert_fn(record)
            except Exception:
                pass  # 告警失敗不連累紀錄/任務（出口自有降級）
