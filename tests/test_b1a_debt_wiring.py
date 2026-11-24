"""B1a 欠債接線測試（卡 t_786e5277）。

覆蓋：
- 外殼 DBURL 環境注入口（直接值＞解析腳本＞None）。
- `--replay-spool` CLI 接線（欠債#1：僅缺 CLI 接線之 Seam 認帳）。
- 告警出口吃宣告檔 alert.channel/dedupe_hours/on（欠債#3）。
- 重試吃 retry.on 白名單（欠債#3）；timeout 一律不重試。
- FUND13_SCHED_DECL_DIR 注入口。
- missed.py hosted-exec 納管判式（欠債#2 定案＝legacy_ref 外殼錨＋cron）。
"""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import funlab.sched.alerting as alerting_mod
from funlab.sched import missed
from funlab.sched.cli_run import _db_url_from_env, main
from funlab.sched.manifest import TaskSpec, load_dir

BASE_TAIL = """
[safety]
writes_db = false
idempotent = true
"""


def _decl(tmp_path, body, name='a.toml'):
    d = tmp_path / 'tasks.d'
    d.mkdir(exist_ok=True)
    (d / name).write_text(body + BASE_TAIL, encoding='utf-8')
    return str(d)


def _rows(spool_dir):
    rows = []
    for f in sorted(spool_dir.glob('*.jsonl')):
        rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return rows


class _Proc:
    def __init__(self, returncode, stdout=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ''


class _Runner:
    def __init__(self, rcs):
        self.rcs = list(rcs)

    def __call__(self, argv, **kw):
        rc = self.rcs.pop(0) if self.rcs else 0
        if rc == 'TIMEOUT':
            raise subprocess.TimeoutExpired(argv, kw.get('timeout', 0))
        return _Proc(rc)


class _Alerts:
    def __init__(self):
        self.sent = []

    def send(self, task_id, event, message, **kw):
        self.sent.append((task_id, event, message))
        return True


class _Rec:
    """RunRecorder 替身：記 write ＋ replay_spool 呼叫。"""

    def __init__(self, stats=None):
        self.rows = []
        self.stats = stats or {'replayed': 1, 'failed': 0}
        self.replay_calls = 0
        self.db_sink = None

    def write(self, record):
        self.rows.append(record)
        return 'spool'

    def replay_spool(self):
        self.replay_calls += 1
        return self.stats


def _spool_kwargs(tmp_path):
    from funlab.sched.runrec import RunRecorder
    spool = tmp_path / 'spool'
    return RunRecorder(spool_dir=spool, db_sink=None), spool


# ---------- 欠債#1：DBURL 環境注入＋--replay-spool ----------------------------

def test_db_url_env_direct(monkeypatch, tmp_path):
    monkeypatch.setenv('FUND13_SCHED_DBURL', 'postgresql://u:***@h/db')
    monkeypatch.delenv('FUND13_DBURL_RESOLVER', raising=False)
    assert _db_url_from_env() == 'postgresql://u:***@h/db'


def test_db_url_env_resolver_precedence_and_fail(monkeypatch, tmp_path):
    monkeypatch.delenv('FUND13_SCHED_DBURL', raising=False)
    fake = tmp_path / 'resolve.sh'
    fake.write_text('#!/bin/sh\necho junk\necho postgresql://r:v@h/db\n')
    monkeypatch.setenv('FUND13_DBURL_RESOLVER', str(fake))
    import funlab.sched.cli_run as cli
    monkeypatch.setattr(cli.subprocess, 'run',
                        lambda *a, **k: _Proc(0, stdout='junk\npostgresql://r:v@h/db\n'))
    assert _db_url_from_env() == 'postgresql://r:v@h/db'
    # 直接值優先於解析腳本
    monkeypatch.setenv('FUND13_SCHED_DBURL', 'postgresql://direct/db')
    assert _db_url_from_env() == 'postgresql://direct/db'
    # 解析器非零退出＝None（降級不崩）
    monkeypatch.delenv('FUND13_SCHED_DBURL')
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: _Proc(1))
    assert _db_url_from_env() is None


def test_db_url_none_without_env(monkeypatch):
    monkeypatch.delenv('FUND13_SCHED_DBURL', raising=False)
    monkeypatch.delenv('FUND13_DBURL_RESOLVER', raising=False)
    assert _db_url_from_env() is None


def test_replay_spool_cli(tmp_path):
    decl = _decl(tmp_path, 'id = "a"\nowner = "o"\nkind = "exec"\nentry = "true"\n')
    rec = _Rec()
    rc = main(['--replay-spool'], declarations=decl, recorder=rec)
    assert rc == 0
    assert rec.replay_calls == 1


# ---------- 欠債#3：alert.channel/dedupe/on＋retry.on 白名單 ------------------

def test_alerter_from_spec_channel_and_dedupe(tmp_path, monkeypatch):
    captured = {}

    class _SpyAlerter:
        def __init__(self, *, channel, dedupe_hours):
            captured['channel'] = channel
            captured['dedupe_hours'] = dedupe_hours

    monkeypatch.setattr(alerting_mod, 'Alerter', _SpyAlerter)
    monkeypatch.setenv('FUND13_STATE_DIR', str(tmp_path / 'state'))
    decl = _decl(tmp_path, '''id = "a"
owner = "o"
kind = "exec"
entry = "exit 5"
alert = { channel = "hermes:pm:chat1", dedupe_hours = 1 }
''')
    main(['a'], declarations=decl, recorder=_Rec(), runner=_Runner([5]),
         sleeper=lambda s: None)
    assert captured == {'channel': 'hermes:pm:chat1', 'dedupe_hours': 1.0}


def test_alert_on_list_suppresses_failed(tmp_path):
    decl = _decl(tmp_path, '''id = "a"
owner = "o"
kind = "exec"
entry = "exit 5"
alert = { on = ["missed"] }
''')
    al = _Alerts()
    rc = main(['a'], declarations=decl, recorder=_Rec(), alerter=al,
              runner=_Runner([5]))
    assert rc == 5 and al.sent == []


def test_retry_on_whitelist_blocks_retry(tmp_path):
    decl = _decl(tmp_path, '''id = "a"
owner = "o"
kind = "exec"
entry = "exit 5"
[retry]
max = 3
on = ["timeout"]
''')
    rec, _spool = _Rec(), None
    al = _Alerts()
    runner = _Runner([5])
    rc = main(['a'], declarations=decl, recorder=rec, alerter=al,
              runner=runner, sleeper=lambda s: None)
    assert rc == 5
    assert [r.status for r in rec.rows] == ['failed']   # 未重試
    assert len(runner.rcs) == 0                          # 只跑一次
    assert '白名單' in al.sent[0][2]


def test_default_retry_on_still_retries(tmp_path):
    decl = _decl(tmp_path, '''id = "a"
owner = "o"
kind = "exec"
entry = "exit 5"
[retry]
max = 2
''')
    rec = _Rec()
    rc = main(['a'], declarations=decl, recorder=rec, alerter=_Alerts(),
              runner=_Runner([5, 0]), sleeper=lambda s: None)
    assert rc == 0
    assert [r.status for r in rec.rows] == ['retrying', 'ok']


def test_timeout_never_retried_even_if_on_lists_timeout(tmp_path):
    decl = _decl(tmp_path, '''id = "a"
owner = "o"
kind = "exec"
entry = "sleep"
timeout_sec = 1
[retry]
max = 3
on = ["timeout"]
''')
    rec = _Rec()
    rc = main(['a'], declarations=decl, recorder=rec, alerter=_Alerts(),
              runner=_Runner(['TIMEOUT']), sleeper=lambda s: None)
    assert rc == 124
    assert [r.status for r in rec.rows] == ['timeout']


def test_decl_dir_env_override(tmp_path, monkeypatch):
    decl = _decl(tmp_path, 'id = "a"\nowner = "o"\nkind = "exec"\nentry = "true"\n')
    monkeypatch.setenv('FUND13_SCHED_DECL_DIR', decl)
    rec = _Rec()
    assert main(['a'], recorder=rec, runner=_Runner([0])) == 0
    assert rec.rows[-1].status == 'ok'


# ---------- 欠債#2：hosted-exec 納管判式 --------------------------------------

def _spec(tid, kind='exec', legacy_ref='', trigger=None, **kw):
    return TaskSpec(id=tid, owner='dev-ops', kind=kind,
                    entry=kw.get('entry', 'true'),
                    trigger=trigger if trigger is not None
                    else {'type': 'cron', 'expr': '0 10 * * *', 'tz': 'Asia/Taipei'},
                    safety={'writes_db': False, 'idempotent': True},
                    legacy_ref=legacy_ref)


def test_live_specs_hosted_exec_inclusion():
    now = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    specs = {
        'hosted': _spec('hosted', legacy_ref='fund13-sched-run hosted'),
        'raw_exec': _spec('raw_exec', legacy_ref='TZ=Asia/Taipei x.sh'),
        'anchor_no_cron': _spec('anchor_no_cron', legacy_ref='fund13-sched-run anchor_no_cron',
                                trigger={'type': 'plan'}),
        'inproc': _spec('inproc', kind='inproc', trigger=None),
        'hosted_off': _spec('hosted_off', legacy_ref='fund13-sched-run hosted_off'),
    }
    specs['hosted_off'].enabled = False
    live = missed._live_specs(specs, now)
    assert set(live) == {'hosted', 'inproc'}


def test_find_missed_covers_hosted_exec(tmp_path):
    from funlab.sched.missed import find_missed

    class _Store:
        def has_row_at_or_after(self, task_id, when):
            return False
        def last_ok(self, task_id):
            return None

    specs = {'h': _spec('h', legacy_ref='fund13-sched-run h')}
    now = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)  # 01:00 UTC＝09:00 CST
    out = find_missed(specs, _Store(), now=now, grace_sec=900)
    assert [sid for sid, _t in out]   # 10:00 CST 前一日應有漏單（2 日窗）


def test_find_missed_ignores_unhosted_exec():
    from funlab.sched.missed import find_missed

    class _Store:
        def has_row_at_or_after(self, task_id, when):
            return False
        def last_ok(self, task_id):
            return None

    specs = {'r': _spec('r', legacy_ref='TZ=Asia/Taipei x.sh')}
    now = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
    out = find_missed(specs, _Store(), now=now, grace_sec=900)
    assert out == []


# ---------- B1a 七份宣告檔本身的載入閘 ----------------------------------------

def test_repo_tasks_d_b1a_declarations_load():
    from funlab.sched.cli_run import DEFAULT_DECL_DIR
    specs, errors = load_dir(DEFAULT_DECL_DIR)
    assert errors == []
    ids = {'fi_guards.gate1', 'fi_guards.gate2_9h', 'fi_guards.gate2_16h',
           'fi_guards.gate2_22h', 'fi_guards.gate3', 'cf_gap_watch.quarterly',
           'finstmt_selfcheck.daily'}
    assert ids <= set(specs)
    for tid in ids:
        s = specs[tid]
        assert s.kind == 'exec' and s.safety == {'writes_db': False, 'idempotent': True}
        assert s.legacy_ref == f'fund13-sched-run {tid}'
        assert missed._is_hosted_exec(s)
        assert s.retry == {'max': 1}
    # 七行 entry 逐字含原命令與日誌重定向（行為零變化紅線的機械保證）
    assert specs['fi_guards.gate1'].entry.endswith(
        'fi_guards.sh gate1 >> /home/sunnylin/workspaces/fund13/.deploy/logs/cron_fi_guards.log 2>&1')


# ---------- ADR-101 §5.3 FetchFinstmt 死人開關釘值 -----------------------------

def test_fetchfinstmt_declaration_deadman_252h():
    """死人開關釘值（卡 t_46947671，manager 裁案 A #7246）。

    id 改釘 FetchFinstmt（sched_run 落表鍵，無 inproc. 前綴）＋
    heartbeat.max_silence_hours == 252.0（T4 週期 168h×1.5，ADR-101 §5.2/§5.3，不得改）。
    """
    from funlab.sched.cli_run import DEFAULT_DECL_DIR
    specs, errors = load_dir(DEFAULT_DECL_DIR)
    assert errors == []
    s = specs['FetchFinstmt']
    assert s.kind == 'inproc' and s.entry == 'FetchFinstmtTask'
    assert s.max_silence_hours == 252.0
