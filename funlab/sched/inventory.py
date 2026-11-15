"""`sched-inventory-check` 盤點防漂移工具（卡 t_bdf4eb9c 工項④；報告 §4-B0④）。

目的＝防「實際排程項游離於宣告檔之外」：新增 crontab 行／Hermes job／
entry-point 而未登記宣告檔 → 告警。這是 §2 盤點快照的「活體版」。

形制：
- `check()` 純函式：外部事實由呼叫端注入（易測、可離線復算）。
- `main()` CLI：抓 crontab -l ＋ ~/.hermes/profiles/*/cron/jobs.json ＋
  entry-point group `funlab_sched_task`，比對後印報告＋逐項告警。
- 比對錨點＝宣告檔 legacy_ref（crontab 行子串）／hermes_job（id 全等）／
  entry（entry-point 名全等）。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from funlab.sched.manifest import TaskSpec, load_dir

# cron 行啟頭：@keyword 或 5 欄時間式（首欄為 數字/*/範圍/步進）
_CRON_HEAD = re.compile(r'^(@\w+|[\d*][\d*,/\-]*\s)')


@dataclass
class InventoryReport:
    missing: list[dict] = field(default_factory=list)   # 實際有、宣告無（漂移）
    stale: list[dict] = field(default_factory=list)     # 宣告有、實際無
    ok: list[dict] = field(default_factory=list)        # 兩邊對上

    @property
    def drift_count(self) -> int:
        return len(self.missing) + len(self.stale)

    @property
    def warnings(self) -> list[str]:
        out = []
        for m in self.missing:
            out.append(f"未登記排程項 [{m['source']}]: "
                       f"{m.get('line') or m.get('hermes_job') or m.get('entry')}")
        for d in self.stale:
            out.append(f"宣告檔指向的實際項已不存在 [{d['source']}]: "
                       f"{d.get('hermes_job') or d.get('entry')}"
                       f"（宣告 id={d.get('declared_in')}）")
        return out


def check(specs: dict[str, TaskSpec], *, crontab_lines: list[str],
          hermes_jobs: list[dict], entry_points: list[str]) -> InventoryReport:
    """三口徑對账（報告 §2）。純函式，不碰外部系統。"""
    rep = InventoryReport()

    # ① crontab 行：非空、非註解、非環境式赋值、形似排程行
    exec_refs = [(sid, s.legacy_ref) for sid, s in specs.items()
                 if s.kind in ('exec', 'inproc') and s.legacy_ref]
    for raw in crontab_lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' in line.split()[0]:
            continue
        if not _CRON_HEAD.match(line):
            continue
        hit = next((sid for sid, ref in exec_refs if ref in line), None)
        if hit:
            rep.ok.append({'source': 'crontab', 'line': line, 'declared_in': hit})
        else:
            rep.missing.append({'source': 'crontab', 'line': line})

    # ② Hermes cron jobs：enabled 的要有宣告；宣告的 hermes_job 要在實際清單
    #    （enabled=false 的宣告＝刻意停用，不參與雙向對账）
    declared_hermes = {s.hermes_job: sid for sid, s in specs.items()
                       if s.kind == 'hermes' and s.hermes_job and s.enabled}
    actual_job_ids = {j.get('id') for j in hermes_jobs}
    for j in hermes_jobs:
        if not j.get('enabled'):
            continue
        jid = j.get('id')
        if jid in declared_hermes:
            rep.ok.append({'source': 'hermes', 'hermes_job': jid,
                           'declared_in': declared_hermes[jid]})
        else:
            rep.missing.append({'source': 'hermes', 'hermes_job': jid,
                                'profile': j.get('profile')})
    for jid, sid in declared_hermes.items():
        if jid not in actual_job_ids:
            rep.stale.append({'source': 'hermes', 'hermes_job': jid,
                              'declared_in': sid})

    # ③ entry-point（inproc 域）：實際要有宣告；宣告的 entry 要在實際清單
    declared_eps = {s.entry: sid for sid, s in specs.items()
                    if s.kind == 'inproc' and s.entry and s.enabled}
    for ep in entry_points:
        if ep in declared_eps:
            rep.ok.append({'source': 'entry_point', 'entry': ep,
                           'declared_in': declared_eps[ep]})
        else:
            rep.missing.append({'source': 'entry_point', 'entry': ep})
    for ep, sid in declared_eps.items():
        if ep not in entry_points:
            rep.stale.append({'source': 'entry_point', 'entry': ep,
                              'declared_in': sid})
    return rep


# ---------------------------------------------------------------- CLI 抓取端
def _collect_crontab() -> list[str]:
    import subprocess
    try:
        return subprocess.run(['crontab', '-l'], capture_output=True,
                              text=True, timeout=30).stdout.splitlines()
    except Exception:
        return []


def _collect_hermes_jobs() -> list[dict]:
    out = []
    for f in Path.home().glob('.hermes/profiles/*/cron/jobs.json'):
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            continue
        jobs = data.get('jobs', data) if isinstance(data, dict) else data
        if isinstance(jobs, list):
            for j in jobs:
                if isinstance(j, dict) and j.get('id'):
                    out.append({'id': j.get('id'),
                                'enabled': bool(j.get('enabled', True)),
                                'profile': f.parts[-4]})
    return out


def _collect_entry_points() -> list[str]:
    from importlib.metadata import entry_points
    try:
        return sorted(ep.name for ep in
                      entry_points(group='funlab_sched_task'))
    except Exception:
        return []


def main(argv=None, *, declarations=None, recorder=None, alerter=None) -> int:
    from funlab.sched.manifest import load_dir as _ld
    from funlab.sched.cli_run import DEFAULT_DECL_DIR
    decl_dir = Path(declarations) if declarations else DEFAULT_DECL_DIR
    specs, errors = _ld(decl_dir)
    rep = check(specs, crontab_lines=_collect_crontab(),
                hermes_jobs=_collect_hermes_jobs(),
                entry_points=_collect_entry_points())
    for err in errors:
        print(f'[inventory] 宣告檔載入錯誤: {err}')
    for w in rep.warnings:
        print(f'[inventory] {w}')
    print(f'[inventory] ok={len(rep.ok)} missing={len(rep.missing)} '
          f'stale={len(rep.stale)}')
    if rep.drift_count and alerter is not None:
        try:
            alerter.send('sched_inventory_check', 'inventory_drift',
                         f'盤點防漂移 {rep.drift_count} 項：\n' +
                         '\n'.join(rep.warnings[:20]))
        except Exception:
            pass
    return 1 if rep.drift_count else 0


if __name__ == '__main__':
    sys.exit(main())
