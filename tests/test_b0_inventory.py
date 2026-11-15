"""B0 工項④ `sched-inventory-check` 盤點防漂移 Seam 測試（卡 t_bdf4eb9c）。

邊界＝funlab.sched.inventory.check(specs, crontab_lines, hermes_jobs, entry_points)
→ InventoryReport。比對規則期望值出處：
- 卡面要求4：比對實際排程項 vs 宣告檔，未登記項告警。
- 報告 §3.3 legacy_ref（對账錨點）／§2 盤點三口徑（crontab／hermes jobs／entry-point）。
純函式設計：外部事實由呼叫端注入（CLI main 才去抓 crontab/jobs.json）。
"""
from __future__ import annotations

from funlab.sched.inventory import InventoryReport, check
from funlab.sched.manifest import TaskSpec


def _spec(**kw):
    base = dict(id='x', owner='dev-ops', kind='exec', entry='true')
    base.update(kw)
    return TaskSpec(**base)


CRON = [
    '10 1 * * * /usr/local/bin/fund13-sched-run fetch.finstmt',
    '5 2 * * * /opt/legacy/other_task.sh',
    '@daily /opt/legacy/disk_check.sh',
]
HERMES = [
    {'id': 'abc123', 'profile': 'fund13-dev-ops', 'enabled': True},
    {'id': 'dead99', 'profile': 'fund13-manager', 'enabled': True},
    {'id': 'gone00', 'profile': 'fund13-manager', 'enabled': False},  # 停用不計
]
EPS = ['FetchFinstmt', 'BookKeeping', 'SipWatch', 'OrphanEntry']


class TestCrontabDrift:
    def test_unregistered_line_reported(self):
        specs = {'a': _spec(id='a', kind='exec', legacy_ref='fund13-sched-run fetch.finstmt')}
        rep = check(specs, crontab_lines=CRON, hermes_jobs=HERMES, entry_points=EPS)
        assert isinstance(rep, InventoryReport)
        bad = [m['source'] for m in rep.missing]
        assert bad.count('crontab') == 2        # other_task.sh ＋ disk_check.sh

    def test_registered_line_ok(self):
        specs = {'a': _spec(id='a', legacy_ref='fund13-sched-run fetch.finstmt')}
        rep = check(specs, crontab_lines=[CRON[0]], hermes_jobs=[], entry_points=[])
        assert rep.missing == [] and rep.ok

    def test_comment_lines_ignored(self):
        rep = check({}, crontab_lines=['# crontab 註解', 'PATH=/usr/bin'],
                    hermes_jobs=[], entry_points=[])
        assert rep.missing == []


class TestHermesDrift:
    def test_enabled_job_not_declared(self):
        specs = {'h': _spec(id='h', kind='hermes', hermes_job='abc123')}
        rep = check(specs, crontab_lines=[], hermes_jobs=HERMES, entry_points=[])
        assert [m['hermes_job'] for m in rep.missing if m['source'] == 'hermes'] == ['dead99']

    def test_declared_but_absent_job_flagged(self):
        specs = {'h': _spec(id='h', kind='hermes', hermes_job='nope404')}
        rep = check(specs, crontab_lines=[], hermes_jobs=HERMES, entry_points=[])
        assert [d['hermes_job'] for d in rep.stale] == ['nope404']


class TestEntryDrift:
    def test_unregistered_entry_point(self):
        specs = {'i': _spec(id='i', kind='inproc', entry='FetchFinstmt'),
                 'b': _spec(id='b', kind='inproc', entry='BookKeeping'),
                 's': _spec(id='s', kind='inproc', entry='SipWatch')}
        rep = check(specs, crontab_lines=[], hermes_jobs=[], entry_points=EPS)
        assert [m['entry'] for m in rep.missing if m['source'] == 'entry_point'] == \
            ['OrphanEntry']

    def test_declared_inproc_without_entry_point(self):
        specs = {'g': _spec(id='g', kind='inproc', entry='GhostTask')}
        rep = check(specs, crontab_lines=[], hermes_jobs=[], entry_points=EPS)
        assert [d['entry'] for d in rep.stale] == ['GhostTask']


class TestReportShape:
    def test_warnings_are_human_readable_and_counted(self):
        rep = check({}, crontab_lines=CRON, hermes_jobs=HERMES, entry_points=EPS)
        # 全無宣告 ⇒ 未登記：crontab 3 ＋ hermes enabled 2 ＋ entry 4
        assert len(rep.missing) == 9
        assert len(rep.warnings) == len(rep.missing) + len(rep.stale)
        assert all(isinstance(w, str) and w for w in rep.warnings)
        assert rep.drift_count == len(rep.missing) + len(rep.stale)

    def test_empty_world_clean(self):
        rep = check({}, crontab_lines=[], hermes_jobs=[], entry_points=[])
        assert rep.drift_count == 0 and rep.warnings == []
