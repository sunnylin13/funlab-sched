"""告警出口（卡 t_bdf4eb9c 工項⑤；報告 §3.5-3/§3.5-5）。

形制：
- 沿用既有告警鏈範式＝`fi_guards.send_alert`（finfun-finfetch
  finfun/finfetch/fi_guards.py:239）：不自建通道、不持 secrets，經
  `hermes -p <profile> send -t <target> -s <subject> <body>` 重用 gateway 憑證。
- 頻道（channel）字串語法：
    'log'／'test'                 → 測試頻道（默認，卡面紅線：告警先導向測試頻道，
                                     驗畢才由部署配置切正式出口）
    'hermes:<profile>:<target>'   → 正式出口（Telegram via Hermes gateway）
- 同任務同症狀 `dedupe_hours` 內只發一次（§3.5-3 不洗版）；去重狀態持久化
  （外殼每分鐘起新進程，記憶體去重無效）。
- 任何投遞失敗只記日誌回傳 False，不拋。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from funlab.sched.runrec import default_state_dir

TEST_ALERT_FILENAME = 'sched_alerts_test.log'
DEDUPE_STATE_FILENAME = 'sched_alert_state.json'
DEFAULT_DEDUPE_HOURS = 6.0     # §3.3 例 alert.dedupe_hours=6


class Alerter:
    def __init__(self, *, channel: str = 'log',
                 state_dir: Path | str | None = None,
                 dedupe_hours: float = DEFAULT_DEDUPE_HOURS,
                 clock: Callable[[], float] = time.time,
                 runner: Callable = subprocess.run,
                 logger=None):
        self.channel = (channel or 'log').strip()
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.dedupe_hours = float(dedupe_hours)
        self.clock = clock
        self.runner = runner
        self.logger = logger

    # -- 去重 ---------------------------------------------------------------
    @property
    def _state_path(self) -> Path:
        return self.state_dir / DEDUPE_STATE_FILENAME

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding='utf-8'))
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state), encoding='utf-8')
        except Exception as e:
            self._log('warning', f'[alert] 去重狀態寫入失敗: {e}')

    def _suppressed(self, key: str, now: float) -> bool:
        state = self._load_state()
        last = state.get(key)
        if isinstance(last, (int, float)) and now - last < self.dedupe_hours * 3600:
            return True
        state[key] = now
        self._save_state(state)
        return False

    # -- 投遞 ---------------------------------------------------------------
    def send(self, task_id: str, event: str, message: str, *,
             dedupe_hours: float | None = None) -> bool:
        """告警投遞。回傳 True=已投遞／False=壓制或失敗。永不拋。"""
        try:
            if dedupe_hours is not None:
                saved, self.dedupe_hours = self.dedupe_hours, float(dedupe_hours)
            try:
                if self._suppressed(f'{task_id}|{event}', self.clock()):
                    return False
            finally:
                if dedupe_hours is not None:
                    self.dedupe_hours = saved
            subject = f'[sched] {task_id} {event}'
            if self.channel.startswith('hermes:'):
                return self._send_hermes(subject, message)
            return self._send_test_channel(subject, message)
        except Exception as e:
            self._log('error', f'[alert] 投遞例外 task={task_id}: {e}')
            return False

    def _send_test_channel(self, subject: str, message: str) -> bool:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.clock()))
            with (self.state_dir / TEST_ALERT_FILENAME).open('a', encoding='utf-8') as f:
                f.write(f'{ts} {subject}\n{message}\n--\n')
            self._log('info', f'[alert](test) {subject}')
            return True
        except Exception as e:
            self._log('error', f'[alert] 測試頻道寫入失敗: {e}')
            return False

    def _send_hermes(self, subject: str, message: str) -> bool:
        parts = self.channel.split(':')
        if len(parts) < 3 or not parts[1] or not parts[2]:
            self._log('error', f'[alert] 頻道格式須 hermes:<profile>:<target>，得 {self.channel!r}')
            return False
        _, profile, target = parts[0], parts[1], ':'.join(parts[2:])
        cmd = ['hermes', '-p', profile, 'send', '-t', target, '-s', subject, message]
        try:
            res = self.runner(cmd, capture_output=True, text=True, timeout=60)
        except Exception as exc:  # 告警鏈失敗不得炸排程（fi_guards 同規）
            self._log('error', f'[alert] 投遞例外：{exc}')
            return False
        ok = getattr(res, 'returncode', 1) == 0
        if not ok:
            self._log('error', f'[alert] 投遞失敗 rc={getattr(res, "returncode", "?")} '
                               f'stderr={getattr(res, "stderr", "")}')
        return ok

    def _log(self, level: str, msg: str) -> None:
        if self.logger is not None:
            try:
                getattr(self.logger, level)(msg)
            except Exception:
                pass
