"""宣告檔 schema＋載入器（卡 t_bdf4eb9c 工項④）。

規格正本：KB `dev/reports/Report-fund13-schedule-task統一排程層規劃評估-2026-11-15.md`
§3.3（`schedule/tasks.d/<id>.toml`）。設計要點：

- 白名單鍵驗證（含巢狀層）：未知鍵一律拒載並在 errors 回報（呼叫端負責告警）。
  動機＝SCH-14／小寫裸鍵事故（config 髒鍵流入 add_job 靜默 disabled）：載入器
  是排程鍵進入 APScheduler 前唯一強制閘。
- 一檔一任務；檔名不要求等於 id，但 id 全目录唯一（duplicate 拒載）。
- `kind` 擴充 `hermes`／`os`：B0 盤點登記用（納管不執行）；本層只執行 exec
  （見 cli_run），inproc 由 SchedService 執行。
- 本模組不告警、不印誌：純函式回傳 (specs, errors)，告警是呼叫端 seam。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_KINDS = {'exec', 'inproc', 'hermes', 'os'}
VALID_TRIGGER_TYPES = {'cron', 'interval', 'date', 'plan'}
VALID_MISFIRE_POLICIES = {'run_once', 'skip'}
VALID_LIFECYCLE_KINDS = {'recurring', 'once', 'converge'}
VALID_RETRY_ON = {'exit_nonzero', 'timeout', 'exception'}
VALID_ALERT_ON = {'failed', 'missed', 'timeout', 'retry_exhausted', 'silence'}
VALID_WITHIN = {'same_day'}
VALID_ON_FAIL = {'skip_and_alert'}

# 頂層白名單（§3.3 全欄 ＋ hermes_job＝盤點防漂移對账鍵［本卡擴充，PR 揭露］）
TOP_KEYS = {'id', 'owner', 'kind', 'entry', 'trigger', 'timeout_sec', 'retry',
            'misfire', 'concurrency', 'depends_on', 'safety', 'alert',
            'heartbeat', 'lifecycle', 'enabled', 'legacy_ref', 'hermes_job'}
TRIGGER_KEYS = {'type', 'expr', 'tz', 'calendar', 'days', 'hours', 'minutes',
                'run_date'}
RETRY_KEYS = {'max', 'backoff_sec', 'on'}
MISFIRE_KEYS = {'grace_sec', 'policy'}
CONCURRENCY_KEYS = {'key', 'max'}
DEPENDS_ON_KEYS = {'task', 'within', 'on_fail'}
SAFETY_KEYS = {'writes_db', 'idempotent'}
ALERT_KEYS = {'channel', 'on', 'dedupe_hours'}
HEARTBEAT_KEYS = {'max_silence_hours'}
LIFECYCLE_KEYS = {'kind', 'expires_at', 'stop_when'}

# 可選 dict 欄：欄名 → (白名單, 子鍵→合法值 枚舉表)
_DICT_FIELDS: dict[str, tuple[set, dict[str, set]]] = {
    'trigger': (TRIGGER_KEYS, {'type': VALID_TRIGGER_TYPES}),
    'retry': (RETRY_KEYS, {'on': VALID_RETRY_ON}),
    'misfire': (MISFIRE_KEYS, {'policy': VALID_MISFIRE_POLICIES}),
    'concurrency': (CONCURRENCY_KEYS, {}),
    'safety': (SAFETY_KEYS, {}),
    'alert': (ALERT_KEYS, {'on': VALID_ALERT_ON}),
    'heartbeat': (HEARTBEAT_KEYS, {}),
    'lifecycle': (LIFECYCLE_KEYS, {'kind': VALID_LIFECYCLE_KINDS}),
}


@dataclass
class TaskSpec:
    """一份宣告檔的載入結果（欄位語義見 §3.3 註解）。"""
    id: str
    owner: str
    kind: str
    entry: str = ''
    trigger: dict | None = None
    timeout_sec: int | None = None
    retry: dict | None = None
    misfire: dict | None = None
    concurrency: dict | None = None
    depends_on: list = field(default_factory=list)
    safety: dict | None = None
    alert: dict | None = None
    heartbeat: dict | None = None
    lifecycle: dict | None = None
    enabled: bool = True
    legacy_ref: str = ''
    hermes_job: str = ''
    source_path: str = ''

    @property
    def max_silence_hours(self) -> float | None:
        if self.heartbeat:
            v = self.heartbeat.get('max_silence_hours')
            if isinstance(v, (int, float)):
                return float(v)
        return None

    @property
    def lifecycle_kind(self) -> str:
        return (self.lifecycle or {}).get('kind', 'recurring')


def _check_dict(field_name: str, value: Any, errors: list[str]) -> None:
    """巢狀 dict 的白名單＋枚舉驗證。"""
    allow, enums = _DICT_FIELDS[field_name]
    if not isinstance(value, dict):
        errors.append(f"{field_name}: 需為 table/dict，得到 {type(value).__name__}")
        return
    for k in value:
        if k not in allow:
            errors.append(f"{field_name}.{k}: 未知鍵（白名單 {sorted(allow)}），拒載")
    for k, allowed in enums.items():
        if k in value:
            vals = value[k] if isinstance(value[k], (list, tuple)) else [value[k]]
            for v in vals:
                if v not in allowed:
                    errors.append(
                        f"{field_name}.{k}={v!r}: 非法值（allowed={sorted(allowed)}）")


def load_file(path: Path) -> tuple[TaskSpec | None, list[str]]:
    """載入單一宣告檔。回傳 (TaskSpec|None, errors)；errors 非空＝拒載。"""
    errors: list[str] = []
    try:
        raw = tomllib.loads(Path(path).read_text(encoding='utf-8'))
    except Exception as e:  # tomllib 解析錯亦屬載入失敗（呼叫端告警）
        return None, [f"{Path(path).name}: TOML 解析失敗: {e}"]

    for k in raw:
        if k not in TOP_KEYS:
            errors.append(f"{k}: 未知鍵（白名單見 manifest.TOP_KEYS），拒載")

    for req in ('id', 'owner', 'kind'):
        if not raw.get(req):
            errors.append(f"{req}: 必填欄缺失")

    kind = raw.get('kind')
    if kind and kind not in VALID_KINDS:
        errors.append(f"kind={kind!r}: 非法值（allowed={sorted(VALID_KINDS)}）")

    if kind in ('exec', 'inproc') and not raw.get('entry'):
        errors.append("entry: kind=exec/inproc 必填（exec＝命令；inproc＝entry-point 名）")
    if kind in ('exec', 'inproc') and not isinstance(raw.get('safety'), dict):
        errors.append("safety: kind=exec/inproc 必填（writes_db/idempotent 不得有預設值）")

    for fname in _DICT_FIELDS:
        if fname in raw:
            _check_dict(fname, raw[fname], errors)

    if 'depends_on' in raw:
        deps = raw['depends_on']
        if not isinstance(deps, list):
            errors.append("depends_on: 需為清單")
        else:
            for i, dep in enumerate(deps):
                if not isinstance(dep, dict):
                    errors.append(f"depends_on[{i}]: 需為 table")
                    continue
                for k in dep:
                    if k not in DEPENDS_ON_KEYS:
                        errors.append(f"depends_on[{i}].{k}: 未知鍵，拒載")
                if dep.get('within') not in VALID_WITHIN:
                    errors.append(
                        f"depends_on[{i}].within={dep.get('within')!r}: 非法值"
                        f"（allowed={sorted(VALID_WITHIN)}）")
                if dep.get('on_fail') not in VALID_ON_FAIL:
                    errors.append(
                        f"depends_on[{i}].on_fail={dep.get('on_fail')!r}: 非法值"
                        f"（allowed={sorted(VALID_ON_FAIL)}）")

    lc = raw.get('lifecycle') or {}
    if isinstance(lc, dict):
        if lc.get('kind') == 'once' and not lc.get('expires_at'):
            errors.append("lifecycle.kind=once: 必填 expires_at")
        if lc.get('kind') == 'converge' and not lc.get('stop_when'):
            errors.append("lifecycle.kind=converge: 必填 stop_when")

    if errors:
        return None, [f"{Path(path).name}: {e}" for e in errors]

    spec = TaskSpec(
        id=raw['id'], owner=raw['owner'], kind=str(kind),
        entry=raw.get('entry', ''), trigger=raw.get('trigger'),
        timeout_sec=raw.get('timeout_sec'), retry=raw.get('retry'),
        misfire=raw.get('misfire'), concurrency=raw.get('concurrency'),
        depends_on=list(raw.get('depends_on') or []),
        safety=raw.get('safety'), alert=raw.get('alert'),
        heartbeat=raw.get('heartbeat'), lifecycle=raw.get('lifecycle'),
        enabled=bool(raw.get('enabled', True)),
        legacy_ref=raw.get('legacy_ref', ''),
        hermes_job=raw.get('hermes_job', ''),
        source_path=str(path),
    )
    return spec, []


def load_dir(directory: Path) -> tuple[dict[str, TaskSpec], list[str]]:
    """載入宣告目錄全量。回傳 ({id: TaskSpec}, errors)；任一檔錯誤→該檔拒載，
    其餘檔照常載入（單檔故障隔離）；重複 id → 兩者皆拒。"""
    directory = Path(directory)
    specs: dict[str, TaskSpec] = {}
    errors: list[str] = []
    if not directory.is_dir():
        return {}, [f"宣告目錄不存在: {directory}"]
    seen: dict[str, str] = {}
    for path in sorted(directory.glob('*.toml')):
        spec, errs = load_file(path)
        errors.extend(errs)
        if spec is None:
            continue
        if spec.id in seen:
            errors.append(f"{path.name}: id '{spec.id}' 與 {seen[spec.id]} 重複（duplicate id），拒載")
            specs.pop(spec.id, None)
            continue
        seen[spec.id] = path.name
        specs[spec.id] = spec
    return specs, errors
