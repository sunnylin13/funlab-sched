"""[t_2a2c557f] 收斂側心跳閘腳本測案（ADR-101 §6：消費測案落 funlab-sched 倉）。

口徑正本（期望值獨立來源）：
- 退出碼 0/2/3 ＝卡面【腳本契約】PM 裁決條文逐字。
- 心跳字面 task_id×status×trigger_kind 三欄＋1.5×P 檔位上限＝ADR-101 §5.1/§5.2；
  檔位判定由 policy 模組自證（finfun-finfetch tests/unit/test_finstmt_wake_policy.py），
  本檔只釘薄殼轉譯，不重複測 policy 內部。
- 檔位邊界手算依據（policy T1_SPANS=(11,1)-(11,20)／T3_SPANS=(11,21)-(12,5)，
  PERIOD_H T1=14h／T3=24h → 1.5×P：T1=21h、T3=36h）。

測架紀律：fake rows 以 dict 形態直接餵薄殼函式（卡面【測案】）；
勿用 MagicMock 充外部回應（REV 閘門紅線）。
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy

_REAL_CREATE_ENGINE = sqlalchemy.create_engine   # patch 前原函式（鏡像 builder 用，防 spy 遞迴）

CST = ZoneInfo('Asia/Taipei')
SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'finstmt_chain_alive_gate.py'


def _load_gate():
    spec = importlib.util.spec_from_file_location('finstmt_chain_alive_gate', SCRIPT)
    assert spec and spec.loader, f'script missing: {SCRIPT}'
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()

# 判定日 t＝2026-11-24（T3 檔：11/21–12/5，1.5×P=36h）；窗起＝2026-11-17 00:00 CST。
# 注入固定 window_calc，單元測不觸 fin_cale（日曆連動另有 test_compute_window_start_* 釘選）。
T = datetime(2026, 11, 24, 12, 0, tzinfo=CST)
W = datetime(2026, 11, 17, 0, 0, tzinfo=CST)


def _beat(at: datetime, *, status='ok', kind='cron', task='FetchFinstmt'):
    """sched_run 查詢列的 dict 形態（欄集合 policy heartbeat_row_ok 字面）。"""
    return {'task_id': task, 'status': status, 'trigger_kind': kind,
            'finished_at': at}


def _run(rows=None, *, query=None, window_calc=None):
    q = query if query is not None else (lambda lb: rows)
    return gate.run_gate(T, window_calc=window_calc or (lambda t: W), query=q)


# --- 六態（卡面【測案】逐條） -------------------------------------------------

def test_no_heartbeat_exit2():
    rc, payload = _run(rows=[])
    assert rc == 2
    assert payload['alive'] is False
    assert payload['reason'] == 'no_heartbeat_in_window'


def test_interval_exceeded_exit2():
    # 相鄰間隔 11/18 00:00→11/24 10:00＝154h > max(1.5×P(T1),1.5×P(T3))=36h → 規則 2 死
    # （手算：6 日 10 時＝154h；末筆距 t 2h 合規則 3 → 判死必來自規則 2）
    rows = [_beat(datetime(2026, 11, 18, 0, 0, tzinfo=CST)),
            _beat(datetime(2026, 11, 24, 10, 0, tzinfo=CST))]
    rc, payload = _run(rows=rows)
    assert rc == 2
    assert payload['alive'] is False
    assert payload['reason'] == 'interval_exceeded_or_cross_tier'


def test_last_beat_stale_exit2():
    # 單筆 11/20 08:00 在窗內（規則 1 過、規則 2 無相鄰對）；距 t＝100h > 36h → 規則 3 死
    # （手算：4 日 4 時＝100h）
    rows = [_beat(datetime(2026, 11, 20, 8, 0, tzinfo=CST))]
    rc, payload = _run(rows=rows)
    assert rc == 2
    assert payload['alive'] is False
    assert payload['reason'] == 'last_beat_stale'
    assert payload['last_beat'] == '2026-11-20T08:00:00+08:00'


def test_normal_exit0():
    rows = [_beat(datetime(2026, 11, 23, 12, 0, tzinfo=CST)),
            _beat(datetime(2026, 11, 24, 6, 0, tzinfo=CST))]
    rc, payload = _run(rows=rows)
    assert rc == 0
    assert payload['alive'] is True
    assert payload['last_beat'] == '2026-11-24T06:00:00+08:00'
    assert payload['window_start'] == '2026-11-17T00:00:00+08:00'
    assert payload['t'] == '2026-11-24T12:00:00+08:00'


def test_query_raises_exit3():
    def boom(lb):
        raise RuntimeError('db down')
    rc, payload = _run(query=boom)
    assert rc == 3
    assert payload['alive'] is None
    assert payload['reason'].startswith('query_failed')


def test_manual_only_exit2():
    # 僅 manual 列（手動觸發不證排程鏈存活，ADR §5.2/§6.1 H-6）
    rows = [_beat(datetime(2026, 11, 24, 9, 0, tzinfo=CST), kind='manual')]
    rc, payload = _run(rows=rows)
    assert rc == 2
    assert payload['alive'] is False
    assert payload['reason'] == 'no_heartbeat_in_window'


# --- fail-closed 加護面（卡面退出碼契約＋§5.2 規則 4） -------------------------

def test_rows_none_exit3():
    rc, payload = _run(query=lambda lb: None)
    assert rc == 3
    assert payload['alive'] is None
    assert payload['reason'].startswith('query_failed')


def test_calendar_unreachable_exit3():
    def broken_calendar(t):
        raise gate.GateError('calendar unreachable: CAL_OOB')
    rc, payload = _run(rows=[], window_calc=broken_calendar)
    assert rc == 3
    assert payload['alive'] is None
    assert 'calendar' in payload['reason']


# --- 取數下界（回看餘裕 ≥252h，卡面【腳本契約】；252 值出處＝卡面＋policy 常數） --

def test_query_lower_bound_lookback_252h():
    seen = {}

    def spy(lb):
        seen['lb'] = lb
        return []

    _run(query=spy)
    assert seen['lb'] == W - timedelta(hours=252)


# --- --t 解析（預設 now；naive 一律視同 Asia/Taipei＝policy 規約 §3.3） ---------

def test_parse_t_naive_is_cst():
    assert gate.parse_t('2026-11-24T12:00:00') == T


def test_parse_t_aware_converted_to_cst():
    got = gate.parse_t('2026-11-24T04:00:00+00:00')
    assert got == T
    assert got.utcoffset() == timedelta(hours=8)


def test_parse_t_bad_input_raises_gate_error():
    with pytest.raises(gate.GateError):
        gate.parse_t('not-a-date')


# --- main()：stdout 恰一行 JSON＋退出碼（消費端可機器查詢契約） ------------------

def test_main_single_line_json_and_exit_code(capsys):
    rows = [_beat(datetime(2026, 11, 24, 6, 0, tzinfo=CST)),
            _beat(datetime(2026, 11, 23, 12, 0, tzinfo=CST))]
    rc = gate.main(['--t', '2026-11-24T12:00:00+08:00'],
                   query=lambda lb: rows, window_calc=lambda t: W)
    out = capsys.readouterr().out
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload['alive'] is True
    assert payload['reason'] == 'chain_alive'


def test_main_bad_t_exit3(capsys):
    rc = gate.main(['--t', 'garbage'], query=lambda lb: [],
                   window_calc=lambda t: W)
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload['alive'] is None
    assert 'bad_t' in payload['reason']


# --- 預設 window_calc＝fin_cale 真日曆釘選（2026-11 無假日，僅週五/週六休市） -----
# 錨點自查：若 fin_cale supplement 改版翻案 2026-11 任何日，此測紅＝意圖訊號（比照
# finfun-factor 錨點自查慣例），須重推期望日而非放寬斷言。

def test_compute_window_start_default_fin_cale():
    got = gate.compute_window_start(T)
    assert got == datetime(2026, 11, 17, 0, 0, tzinfo=CST)


# --- 缺件／日曆異常 face（退出碼 3 契約＝卡面＋ADR §5.2 規則 4） -----------------

_STUB_DBURL = '''
import re
ENV_VAR_RE = re.compile(r"\\{\\{ENV_VAR:(\\w+)\\}\\}")


def load_env():
    return {"U": "uu", "P": "pp"}


def active_production_url():
    return "postgresql://{{ENV_VAR:U}}:{{ENV_VAR:P}}@h:5432/db"
'''


def test_resolve_production_url_expands_placeholders(tmp_path, monkeypatch):
    (tmp_path / 'resolve_dburl.py').write_text(_STUB_DBURL, encoding='utf-8')
    monkeypatch.setattr(gate, 'DEPLOY_DIR', tmp_path)
    assert gate.resolve_production_url() == 'postgresql://uu:pp@h:5432/db'


def test_resolve_production_url_file_missing_exit3(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, 'DEPLOY_DIR', tmp_path / 'nope')
    with pytest.raises(gate.GateError, match='missing_component'):
        gate.resolve_production_url()


def test_resolve_production_url_bad_stub_exit3(tmp_path, monkeypatch):
    (tmp_path / 'resolve_dburl.py').write_text('raise RuntimeError("broken stub")',
                                               encoding='utf-8')
    monkeypatch.setattr(gate, 'DEPLOY_DIR', tmp_path)
    with pytest.raises(gate.GateError, match='missing_component'):
        gate.resolve_production_url()


def test_calendar_import_failure_exit3(monkeypatch):
    monkeypatch.setitem(sys.modules, 'finfun.utils', None)
    rc, payload = _run(rows=[], window_calc=gate.compute_window_start)
    assert rc == 3
    assert 'calendar_unreachable' in payload['reason']


def test_calendar_bounds_exceeded_exit3(monkeypatch):
    from finfun.utils import fin_cale

    def boom(d):
        raise fin_cale.CalendarBoundsExceeded('CAL_OOB 越界')
    monkeypatch.setattr(fin_cale, 'prev_trading_day', boom)
    rc, payload = _run(rows=[], window_calc=gate.compute_window_start)
    assert rc == 3
    assert 'calendar_unreachable' in payload['reason']


# --- 取數路徑釘選（sqlite 隔離，零正式庫連線） -----------------------------------

def _sqlite_sched_run(rows=()):
    """同名表 sqlite 模擬：回 (engine, text helper)。欄名與卡面 SQL 契約對齊。"""
    from sqlalchemy import text
    eng = _REAL_CREATE_ENGINE('sqlite://')
    with eng.begin() as c:
        c.execute(text('create table sched_run (task_id text, status text, '
                       'trigger_kind text, finished_at text)'))
        if rows:   # 空種子＝純空表（execute 空參列表會炸綁參required）
            c.execute(text("insert into sched_run values ('FetchFinstmt', :s, :k, :f)"),
                      [{'s': st, 'k': k, 'f': f} for st, k, f in rows])
    return eng, text


def test_fetch_rows_four_columns_time_filtered():
    # 期望值獨立來源＝卡面 SQL 契約（僅四欄）＋下界過濾語意；時間以 ISO 字串
    # 字典序比較（sqlite text 欄模擬，僅釘選 SQL 形態非型別鏈）
    eng, _ = _sqlite_sched_run(
        rows=[('ok', 'cron', '2026-11-10 00:00:00'),
              ('ok', 'cron', '2026-11-20 08:00:00')])
    rows = gate.fetch_rows(eng, '2026-11-15 00:00:00')
    assert [r['finished_at'] for r in rows] == ['2026-11-20 08:00:00']
    assert set(rows[0]) == {'task_id', 'status', 'trigger_kind', 'finished_at'}
    eng.dispose()


def test_default_query_pins_readonly_engine(monkeypatch):
    # 雙保險紅線釘選：engine 必須帶 execution_options(readonly=True)；
    # url 經 resolve_production_url（此測以 stub 頂替，真值不入斷言）。
    import sqlalchemy
    captured = {}

    def spy(url, **kw):
        captured['url'] = url
        captured['execution_options'] = kw.get('execution_options')
        eng, _ = _sqlite_sched_run()
        return eng
    monkeypatch.setattr(sqlalchemy, 'create_engine', spy)
    monkeypatch.setattr(gate, 'resolve_production_url',
                        lambda: 'postgresql://stub-url/db')
    rows = gate.default_query('2026-11-15 00:00:00')
    assert captured['execution_options'] == {'readonly': True}
    assert captured['url'] == 'postgresql://stub-url/db'
    assert rows == []


def test_main_default_wiring_no_injection(capsys, monkeypatch):
    """main 預設接線（window_calc＝真 fin_cale＋query＝default_query）全鏈路，
    engine 以 sqlite 鏡像頂替＝零正式庫連線。空表→rc 2（鏈不明，非崩潰）。"""
    import sqlalchemy

    def spy(url, **kw):
        assert kw.get('execution_options') == {'readonly': True}
        eng, _ = _sqlite_sched_run()
        return eng
    monkeypatch.setattr(sqlalchemy, 'create_engine', spy)
    monkeypatch.setattr(gate, 'resolve_production_url',
                        lambda: 'postgresql://stub-url/db')
    rc = gate.main([])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload['alive'] is False
    assert payload['window_start'] is not None


def test_main_guard_system_exit_code(capsys, monkeypatch):
    """`python scripts/...py` 直跑路徑：__main__ 護欄把 rc 轉成 SystemExit。

    REV 修正（Round 1 F1）：釘死 sys.argv＝僅腳本名，argparse 不得吞 pytest argv
    （舊版 argv=None 走 argparse SystemExit(2) 假綠，護欄未被執行）。
    空鏡像表＋真日曆 → 鏈不明，斷言收緊為恰 2（不再放寬 0/2 併容）。
    """
    import runpy

    import sqlalchemy

    monkeypatch.setattr(sys, 'argv', ['finstmt_chain_alive_gate.py'])

    def spy(url, **kw):
        eng, _ = _sqlite_sched_run()
        return eng
    monkeypatch.setattr(sqlalchemy, 'create_engine', spy)
    monkeypatch.setattr(gate, 'resolve_production_url',
                        lambda: 'postgresql://stub-url/db')
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(SCRIPT), run_name='__main__')
    assert e.value.code == 2   # 空鏡像表＝鏈不明（runpy 新模組自行走 default_query→spy）
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload['alive'] is False
    assert payload['reason'] == 'no_heartbeat_in_window'
