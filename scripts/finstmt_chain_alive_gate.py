#!/usr/bin/env python3
"""[t_2a2c557f] 收斂側心跳閘：FetchFinstmt 鏈存活查詢（AC-5 接線，ADR-101 §5.2）。

薄殼定位（卡面 PM 裁決逐字）：讀庫＋算窗＋轉譯退出碼，**零判定邏輯、零複製常數**。
- 判定語意全部在 finfun-finfetch `finstmt_wake_policy.chain_alive_from_rows`
  （單一定義源；本檔只 import，禁複製規則/門檻——口徑漂移＝AC-5 作廢）。
- reason／輔助欄＝診斷性轉譯（僅用 policy 公開 helpers 分類），不承載判定位。

輸出契約：stdout 恰一行 JSON {alive, last_beat, window_start, t, reason, 輔助欄}；
退出碼 0=鏈存活／2=鏈不明（拒判收斂）／3=查詢失敗或缺件（fail-closed）。
紅線：零正式庫寫入（僅 SELECT＋engine readonly）；零告警投遞——處置權在消費端。

用法：python scripts/finstmt_chain_alive_gate.py [--t ISO8601]（--t 預設 now；
naive 輸入視同 Asia/Taipei＝policy §3.3 規約）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

EXIT_ALIVE = 0      # 鏈存活（收斂判定可續行）
EXIT_UNKNOWN = 2    # 鏈不明（拒判收斂：不得回「零新增收斂」）
EXIT_FAIL = 3       # 查詢失敗／缺件／日曆不可達（fail-closed）

CST = ZoneInfo('Asia/Taipei')
WINDOW_TRADING_DAYS = 5   # 卡面「判定日前 5 個營業日」（ADR-101 §5.2 收斂窗 N=5）
DEPLOY_DIR = Path.home() / 'workspaces/fund13/.deploy'   # 連線唯一源（卡面指定路徑）


class GateError(Exception):
    """薄殼環境件不可達（缺件／日曆／連線），一律轉退出碼 3 fail-closed。"""


try:  # 缺件＝fail-closed（退出碼 3），不在 import 期崩棧
    from finfun.finfetch import finstmt_wake_policy as _policy
    _POLICY_IMPORT_ERR: str | None = None
except Exception as _e:  # noqa: BLE001 — 缺件形態不限，一律轉 rc 3
    _policy = None
    _POLICY_IMPORT_ERR = f'{type(_e).__name__}: {_e}'


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec='seconds') if dt else None


def parse_t(raw: str) -> datetime:
    """ISO 字串 → aware(CST)。naive 一律視同 Asia/Taipei（policy §3.3）。"""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise GateError(f'bad_t: {e}') from e
    return dt.replace(tzinfo=CST) if dt.tzinfo is None else dt.astimezone(CST)


def compute_window_start(t: datetime) -> datetime:
    """視窗起點＝判定日（CST）往前嚴格第 5 個營業日 00:00 CST。

    fin_cale（環境正本，MyXTAI＋supplement）不可達／越界 → GateError
    （卡面：日曆不可達＝fail-closed 退出碼 3 並給 reason）。
    """
    try:
        from finfun.utils import fin_cale
    except Exception as e:  # noqa: BLE001
        raise GateError(f'calendar_unreachable: fin_cale import {type(e).__name__}: {e}') from e
    cur = t.astimezone(CST).date()
    try:
        for _ in range(WINDOW_TRADING_DAYS):
            cur = fin_cale.prev_trading_day(cur - timedelta(days=1))
    except Exception as e:  # noqa: BLE001 — CalendarError/BoundsExceeded 一律轉 rc 3
        raise GateError(f'calendar_unreachable: {type(e).__name__}: {e}') from e
    return datetime.combine(cur, time.min, tzinfo=CST)


def resolve_production_url() -> str:
    """正式庫連線唯一源＝.deploy/resolve_dburl.py（卡面契約；值不入 stdout）。"""
    path = DEPLOY_DIR / 'resolve_dburl.py'
    if not path.is_file():
        raise GateError(f'missing_component: {path} 不存在')
    import importlib.util
    spec = importlib.util.spec_from_file_location('_finstmt_gate_dburl', path)
    if spec is None or spec.loader is None:
        raise GateError(f'missing_component: 無法載入 {path}')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        env = mod.load_env()
        url = mod.ENV_VAR_RE.sub(lambda m: env[m.group(1)], mod.active_production_url())
    except GateError:
        raise
    except Exception as e:  # noqa: BLE001
        raise GateError(f'missing_component: resolve_dburl {type(e).__name__}: {e}') from e
    if 'ENV_VAR' in url:
        raise GateError('missing_component: DB url 展開後仍有佔位符')
    return url


def fetch_rows(engine, lower_bound: datetime) -> list[dict]:
    """sched_run 唯讀取數（卡面 SQL 契約逐字：僅四欄 SELECT，時間下界＝下推參數）。"""
    from sqlalchemy import text
    stmt = text('select task_id, status, trigger_kind, finished_at '
                'from sched_run where finished_at >= :lb order by finished_at')
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt, {'lb': lower_bound}).mappings().all()]


def default_query(lower_bound: datetime) -> list[dict]:
    """正式庫唯讀預設取數路徑：唯一源連線＋engine readonly 雙保險；
    engine 用完即 dispose，全程零寫入、零投遞。"""
    from sqlalchemy import create_engine
    url = resolve_production_url()   # GateError → run_gate 呼叫端轉 rc 3
    eng = create_engine(url, execution_options={'readonly': True})
    try:
        return fetch_rows(eng, lower_bound)
    finally:
        eng.dispose()


def _localize_rows(rows: list[dict]) -> list[dict]:
    """時間域歸一：全部轉 aware(CST) 再餵 policy（tier 邊界以 CST 日為準；
    PG timestamptz 回 UTC aware；naive 依 policy 規約視同 CST）。"""
    out = []
    for r in rows:
        dt = r.get('finished_at')
        if isinstance(dt, datetime):
            r = {**r, 'finished_at': dt.replace(tzinfo=CST) if dt.tzinfo is None
                 else dt.astimezone(CST)}
        out.append(r)
    return out


def judge(rows: list[dict], window_start: datetime, t: datetime) -> dict:
    """alive 判定＝轉呼 policy.chain_alive_from_rows（零複製）；reason＝診斷分類。"""
    loc = _localize_rows(rows)
    alive = _policy.chain_alive_from_rows(loc, window_start, t)
    beats = sorted(
        r['finished_at'] for r in loc
        if _policy.heartbeat_row_ok(r) and r.get('finished_at') is not None
        and r['finished_at'] <= t)
    in_win = [h for h in beats if h >= window_start]
    last_beat = beats[-1] if beats else None
    if alive:
        reason = 'chain_alive'
    elif not in_win:
        reason = 'no_heartbeat_in_window'
    elif (t - in_win[-1]) > timedelta(
            hours=_policy.max_silence_hours_for(t.astimezone(CST).date())):
        reason = 'last_beat_stale'
    else:
        reason = 'interval_exceeded_or_cross_tier'
    return {
        'alive': alive,
        'last_beat': _iso(last_beat),
        'window_start': _iso(window_start),
        't': _iso(t),
        'reason': reason,
        # 判讀輔助欄（policy 公開 helpers 現算，非判定源）
        'tier_t': _policy.tier_of(t.astimezone(CST).date()),
        'max_silence_hours': _policy.max_silence_hours_for(t.astimezone(CST).date()),
        'beats_in_window': len(in_win),
    }


def _fail(t: datetime | None, window_start: datetime | None, reason: str) -> dict:
    return {'alive': None, 'last_beat': None, 'window_start': _iso(window_start),
            't': _iso(t), 'reason': reason}


def run_gate(t: datetime, *, window_calc, query) -> tuple[int, dict]:
    """主 seam：日曆→視窗→取數→policy 判定→退出碼轉譯。"""
    if _policy is None:
        return EXIT_FAIL, _fail(t, None, f'missing_component: finstmt_wake_policy ({_POLICY_IMPORT_ERR})')
    try:
        window_start = window_calc(t)
    except Exception as e:  # noqa: BLE001 — 日曆類一律 fail-closed rc 3
        reason = str(e) if isinstance(e, GateError) else f'calendar_unreachable: {type(e).__name__}: {e}'
        return EXIT_FAIL, _fail(t, None, reason)
    lower = window_start - timedelta(hours=_policy.DEADMAN_MAX_SILENCE_HOURS)
    try:
        rows = query(lower)
    except Exception as e:  # noqa: BLE001 — 查詢例外 fail-closed（ADR §5.2 規則 4）
        return EXIT_FAIL, _fail(t, window_start, f'query_failed: {type(e).__name__}: {e}')
    if rows is None:
        return EXIT_FAIL, _fail(t, window_start, 'query_failed: rows=None（sched_run 不可達）')
    payload = judge(rows, window_start, t)
    return (EXIT_ALIVE if payload['alive'] else EXIT_UNKNOWN), payload


def main(argv: list[str] | None = None, *, query=None, window_calc=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--t', default=None, help='判定時刻 ISO8601（預設 now；naive 視同 Asia/Taipei）')
    args = ap.parse_args(argv)
    try:
        t = parse_t(args.t) if args.t else datetime.now(CST)
    except GateError as e:
        print(json.dumps(_fail(None, None, str(e)), ensure_ascii=False))
        return EXIT_FAIL
    if window_calc is None:
        window_calc = compute_window_start
    if query is None:
        query = default_query
    # GateError（resolve_production_url／日曆類）均在 run_gate 內轉 rc 3，此處不再兜
    rc, payload = run_gate(t, window_calc=window_calc, query=query)
    print(json.dumps(payload, ensure_ascii=False))
    return rc


if __name__ == '__main__':
    sys.exit(main())
