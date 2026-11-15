"""`fund13-sched-run` 外殼命令（卡 t_bdf4eb9c 工項②；報告 §4-B0③）。

定位：crontab 行「未來」改調本命令（B1a 才搬家，本卡零搬遷）。
職責：讀宣告檔 → 跑 exec 任務 → 每次嘗試寫一列 sched_run（終態單筆口徑，
裁決 #5633）→ 重試閘 → 失敗告警。

紅線落地：
- 紀錄寫入失敗不得讓任務失敗（_safe_write 全覆蓋）。
- `writes_db=true ∧ idempotent=false` 禁自動重試，失敗直接告警（卡面驗收3）。
- 非 exec kind 一律拒跑（exit 3）：hermes/inproc/os 屬 B1 搬家範圍。

退出碼：0 成功／child rc 失敗／2 任務未登記／3 kind 不支援／124 逾時。

B1a 接線（卡 t_786e5277，欠債清單 #5624/#5645）：
- `--replay-spool`：把 JSONL spool 暫存補入 PG（實現在 RunRecorder.replay_spool）。
- 預設紀錄鏈接上 PGSink：DBURL 一律經環境注入——`FUND13_SCHED_DBURL` 直接給
  url，或 `FUND13_DBURL_RESOLVER` 給解析腳本路徑（本機慣例＝.deploy/
  resolve_dburl.py，KB system-paths §資料庫）。兩者皆缺＝spool 降級，零崩潰。
- 告警出口吃宣告檔 `alert.channel`／`alert.dedupe_hours`／`alert.on`
  （§3.3/§3.5-3）；未宣告＝'log' 測試頻道（B0 卡面紅線沿用）。
- 重試吃 `retry.on` 白名單：僅 'exit_nonzero' 觸發自動重試；'timeout' 結果
  不可知一律不重試（§3.5-2 金融寫入紀律延伸）。未宣告 on＝['exit_nonzero']。
- 宣告目錄可用 `FUND13_SCHED_DECL_DIR` 覆寫（測試注入口，預設倉內 tasks.d）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from funlab.sched.manifest import TaskSpec, load_dir
from funlab.sched.runrec import RunRecord, RunRecorder

DEFAULT_TIMEOUT_SEC = 3600.0
DEFAULT_ALERT_EVENTS = frozenset({'failed', 'timeout'})
DEFAULT_RETRY_ON = frozenset({'exit_nonzero'})
# 宣告檔目錄（PM 逕裁項：funlab-sched 倉內 schedule/tasks.d/）
DEFAULT_DECL_DIR = Path(__file__).resolve().parents[2] / 'schedule' / 'tasks.d'


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _db_url_from_env() -> str | None:
    """外殼 DBURL：env 直接值＞解析腳本＞None（spool 降級）。拒硬編碼。"""
    url = (os.environ.get('FUND13_SCHED_DBURL') or '').strip()
    if url:
        return url
    resolver = (os.environ.get('FUND13_DBURL_RESOLVER') or '').strip()
    if not resolver:
        return None
    try:
        proc = subprocess.run([sys.executable, resolver],
                              capture_output=True, text=True, timeout=15,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = [l.strip() for l in (proc.stdout or '').splitlines() if l.strip()]
    return lines[-1] if lines else None


def _safe_write(recorder, record: RunRecord) -> None:
    """紅線：紀錄鏈故障不得影响被紀錄的任務。"""
    try:
        recorder.write(record)
    except Exception:
        pass


def _retry_cfg(spec: TaskSpec) -> tuple[int, float, frozenset]:
    cfg = spec.retry or {}
    max_attempts = int(cfg.get('max', 1) or 1)
    backoff = float(cfg.get('backoff_sec', 0) or 0)
    on = cfg.get('on')
    retry_on = frozenset(on) if on else DEFAULT_RETRY_ON
    return max(1, max_attempts), backoff, retry_on


def _alert_events(spec: TaskSpec) -> frozenset:
    on = (spec.alert or {}).get('on')
    return frozenset(on) if on else DEFAULT_ALERT_EVENTS


def _auto_retry_forbidden(spec: TaskSpec) -> bool:
    safety = spec.safety or {}
    return bool(safety.get('writes_db')) and not bool(safety.get('idempotent'))


def _alerter_for_spec(spec: TaskSpec):
    """外殼預設告警器：出口／去重窗／事件白名單全吃宣告檔（§3.3/§3.5-3）。"""
    from funlab.sched.alerting import Alerter, DEFAULT_DEDUPE_HOURS
    cfg = spec.alert or {}
    channel = str(cfg.get('channel') or 'log')
    dedupe_raw = cfg.get('dedupe_hours')
    dedupe = float(dedupe_raw) if dedupe_raw is not None else DEFAULT_DEDUPE_HOURS
    return Alerter(channel=channel, dedupe_hours=dedupe)


def main(argv=None, *, declarations: str | Path | None = None,
         recorder=None, alerter=None,
         runner: Callable = subprocess.run,
         sleeper: Callable[[float], None] = time.sleep,
         clock: Callable[[], datetime] = _default_clock,
         timeout_sec: float | None = None) -> int:
    ap = argparse.ArgumentParser(prog='fund13-sched-run')
    ap.add_argument('task_id', nargs='?')
    ap.add_argument('--manual', action='store_true',
                    help='人工補跑（trigger_kind=manual）')
    ap.add_argument('--check-missed', action='store_true',
                    help='跑一輪漏跑偵測＋心跳（無 daemon 環境的排程入口）')
    ap.add_argument('--replay-spool', action='store_true',
                    help='把 JSONL spool 暫存補入 sched_run 表（RunRecorder.replay_spool）')
    args = ap.parse_args(argv)

    if declarations is None:
        declarations = (os.environ.get('FUND13_SCHED_DECL_DIR') or '').strip() or None
    decl_dir = Path(declarations) if declarations else DEFAULT_DECL_DIR
    specs, errors = load_dir(decl_dir)
    for err in errors:
        print(f'[sched-run] 宣告檔載入錯誤: {err}', file=sys.stderr)

    if recorder is None:
        from funlab.sched.runrec import PGSink
        recorder = RunRecorder(db_sink=PGSink(_db_url_from_env))

    if args.replay_spool:
        stats = recorder.replay_spool()
        print(f'[sched-run] replay_spool {stats}')
        return 0

    if args.check_missed:
        if alerter is None:
            from funlab.sched.alerting import Alerter
            alerter = Alerter()
        from funlab.sched.missed import Watchdog
        wd = Watchdog(specs_provider=lambda: specs, recorder=recorder,
                      alerter=alerter, clock=clock)
        wd.tick()
        return 0

    if not args.task_id:
        ap.error('需要 task_id 或 --check-missed')
    spec = specs.get(args.task_id)
    if spec is None:
        print(f'[sched-run] 未登記任務: {args.task_id}', file=sys.stderr)
        return 2
    if spec.kind != 'exec':
        # 零搬家紅線：本外殼只代跑 exec；其餘 kind 等 B1
        print(f'[sched-run] kind={spec.kind} 不经外殼執行（B0 零搬遷）',
              file=sys.stderr)
        return 3

    if alerter is None:
        alerter = _alerter_for_spec(spec)
    alert_events = _alert_events(spec)

    def _alert(event: str, message: str) -> None:
        if event not in alert_events:
            return
        try:
            alerter.send(spec.id, event, message)
        except Exception:
            pass

    trigger_kind = 'manual' if args.manual else 'cron'
    # F2（reviewer R1 必修·manager 背書）：宣告欄 timeout_sec 生效（§3.3 每任務
    # 宣告逾時）。優先序＝呼叫端顯式覆寫 > spec.timeout_sec > 全域預設。
    tmo = float(timeout_sec) if timeout_sec is not None \
        else float(spec.timeout_sec or DEFAULT_TIMEOUT_SEC)
    max_attempts, backoff, retry_on = _retry_cfg(spec)
    no_retry = _auto_retry_forbidden(spec)
    scheduled = clock()

    def mk(status, attempt, started, finished, exit_code=None, err=None):
        return RunRecord(task_id=spec.id, status=status, scheduled_at=scheduled,
                         started_at=started, finished_at=finished,
                         attempt=attempt, trigger_kind=trigger_kind,
                         exit_code=exit_code, error_summary=err)

    for attempt in range(1, max_attempts + 1):
        started = clock()
        try:
            proc = runner(spec.entry, shell=True, capture_output=True,
                          timeout=tmo)
            rc = int(proc.returncode)
        except subprocess.TimeoutExpired:
            # 結果不可知（子程序可能仍在寫庫）→ 不重試，直接 timeout 終態
            # （retry.on 含 'timeout' 亦不重試：白名單只放寬 exit_nonzero 域）
            now = clock()
            _safe_write(recorder, mk('timeout', attempt, started, now,
                                     err=f'外殼逾時 {tmo}s'))
            _alert('timeout', f'{spec.id} 執行逾時 {tmo}s（attempt {attempt}）。'
                              f'owner={spec.owner}')
            return 124
        finished = clock()
        if rc == 0:
            _safe_write(recorder, mk('ok', attempt, started, finished, rc))
            return 0
        err = f'外殼退出碼 {rc}'
        can_retry = ((not no_retry) and attempt < max_attempts
                     and 'exit_nonzero' in retry_on)
        if can_retry:
            _safe_write(recorder, mk('retrying', attempt, started, finished, rc, err))
            sleeper(backoff)
            continue
        _safe_write(recorder, mk('failed', attempt, started, finished, rc, err))
        why = ('（writes_db∧¬idempotent 禁自動重試）' if no_retry else
               f'（已重試 {attempt - 1} 次）' if 'exit_nonzero' in retry_on else
               '（retry.on 白名單未含 exit_nonzero）')
        _alert('failed', f'{spec.id} 執行失敗 exit={rc} {why}。owner={spec.owner}')
        return rc if rc != 0 else 1
    return 1   # 理論不可達（迴圈必 return）；防线迴圈重構回歸
