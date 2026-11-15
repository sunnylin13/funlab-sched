"""`fund13-sched-run` 外殼命令（卡 t_bdf4eb9c 工項②；報告 §4-B0③）。

定位：crontab 行「未來」改調本命令（B1a 才搬家，本卡零搬遷）。
職責：讀宣告檔 → 跑 exec 任務 → 每次嘗試寫一列 sched_run（終態單筆口徑，
裁決 #5633）→ 重試閘 → 失敗告警。

紅線落地：
- 紀錄寫入失敗不得讓任務失敗（_safe_write 全覆蓋）。
- `writes_db=true ∧ idempotent=false` 禁自動重試，失敗直接告警（卡面驗收3）。
- 非 exec kind 一律拒跑（exit 3）：hermes/inproc/os 屬 B1 搬家範圍。

退出碼：0 成功／child rc 失敗／2 任務未登記／3 kind 不支援／124 逾時。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from funlab.sched.manifest import TaskSpec, load_dir
from funlab.sched.runrec import RunRecord, RunRecorder

DEFAULT_TIMEOUT_SEC = 3600.0
# 宣告檔目錄（PM 逕裁項：funlab-sched 倉內 schedule/tasks.d/）
DEFAULT_DECL_DIR = Path(__file__).resolve().parents[2] / 'schedule' / 'tasks.d'


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _safe_write(recorder, record: RunRecord) -> None:
    """紅線：紀錄鏈故障不得影响被紀錄的任務。"""
    try:
        recorder.write(record)
    except Exception:
        pass


def _retry_cfg(spec: TaskSpec) -> tuple[int, float]:
    cfg = spec.retry or {}
    max_attempts = int(cfg.get('max', 1) or 1)
    backoff = float(cfg.get('backoff_sec', 0) or 0)
    return max(1, max_attempts), backoff


def _auto_retry_forbidden(spec: TaskSpec) -> bool:
    safety = spec.safety or {}
    return bool(safety.get('writes_db')) and not bool(safety.get('idempotent'))


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
    args = ap.parse_args(argv)

    decl_dir = Path(declarations) if declarations else DEFAULT_DECL_DIR
    specs, errors = load_dir(decl_dir)
    for err in errors:
        print(f'[sched-run] 宣告檔載入錯誤: {err}', file=sys.stderr)

    if recorder is None:
        recorder = RunRecorder()
    if alerter is None:
        from funlab.sched.alerting import Alerter
        alerter = Alerter()

    if args.check_missed:
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

    trigger_kind = 'manual' if args.manual else 'cron'
    # F2（reviewer R1 必修·manager 背書）：宣告欄 timeout_sec 生效（§3.3 每任務
    # 宣告逾時）。優先序＝呼叫端顯式覆寫 > spec.timeout_sec > 全域預設。
    tmo = float(timeout_sec) if timeout_sec is not None \
        else float(spec.timeout_sec or DEFAULT_TIMEOUT_SEC)
    max_attempts, backoff = _retry_cfg(spec)
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
            now = clock()
            _safe_write(recorder, mk('timeout', attempt, started, now,
                                     err=f'外殼逾時 {tmo}s'))
            try:
                alerter.send(spec.id, 'timeout',
                             f'{spec.id} 執行逾時 {tmo}s（attempt {attempt}）。'
                             f'owner={spec.owner}')
            except Exception:
                pass
            return 124
        finished = clock()
        if rc == 0:
            _safe_write(recorder, mk('ok', attempt, started, finished, rc))
            return 0
        err = f'外殼退出碼 {rc}'
        can_retry = (not no_retry) and attempt < max_attempts
        if can_retry:
            _safe_write(recorder, mk('retrying', attempt, started, finished, rc, err))
            sleeper(backoff)
            continue
        _safe_write(recorder, mk('failed', attempt, started, finished, rc, err))
        try:
            why = ('（writes_db∧¬idempotent 禁自動重試）' if no_retry else
                   f'（已重試 {attempt - 1} 次）')
            alerter.send(spec.id, 'failed',
                         f'{spec.id} 執行失敗 exit={rc} {why}。owner={spec.owner}')
        except Exception:
            pass
        return rc if rc != 0 else 1
    return 1   # 理論不可達（迴圈必 return）；防线迴圈重構回歸
