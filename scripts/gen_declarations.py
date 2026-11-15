"""宣告檔初始盤點產生器（卡 t_bdf4eb9c 工項③配套；報告 §4-B0②盤點登記）。

定位＝一次性盤點工具，不進執行路徑：把現行三口徑排程事實（crontab、
Hermes cron jobs、funlab_sched_task entry-point）快照成 `schedule/tasks.d/`
宣告檔，讓 B0 從第一天起就有可盤點的宣告檔（inventory drift=0 自證）。

保守預設（產生後由 owner 人工複核，檔內有 TODO 標記）：
- safety 一律 `writes_db=true, idempotent=false`（最保守＝禁自動重試）。
- hermes／os kind 僅納管登記，不執行（紅線：零搬家）。
用法：python scripts/gen_declarations.py [--out schedule/tasks.d] [--force]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HEADER = (
    '# 初始盤點快照：由 scripts/gen_declarations.py 產生（卡 t_bdf4eb9c）。\n'
    '# TODO owner 複核 safety／trigger／heartbeat（產生器採最保守預設）。\n'
)

AT_MAP = {'@daily': '0 0 * * *', '@midnight': '0 0 * * *',
          '@hourly': '0 * * * *', '@weekly': '0 0 * * 0',
          '@monthly': '0 0 1 * *', '@yearly': '0 0 1 1 *'}


def cron_lines() -> list[str]:
    out = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    return out.stdout.splitlines()


def hermes_jobs() -> list[dict]:
    jobs = []
    for f in sorted(Path.home().glob('.hermes/profiles/*/cron/jobs.json')):
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            continue
        rows = data.get('jobs', data) if isinstance(data, dict) else data
        if isinstance(rows, list):
            for j in rows:
                if isinstance(j, dict) and j.get('id'):
                    jobs.append({'id': j['id'],
                                 'enabled': bool(j.get('enabled', True)),
                                 'profile': f.parts[-4]})
    return jobs


def entry_point_names() -> list[str]:
    from importlib.metadata import entry_points
    return sorted(ep.name for ep in entry_points(group='funlab_sched_task'))


def _slug(s: str) -> str:
    s = re.sub(r'[^0-9a-zA-Z]+', '-', s).strip('-').lower()
    return s[:60] or 'x'


def crontab_decls(lines: list[str]) -> list[dict]:
    decls, seen = [], set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' in line.split()[0]:
            continue
        if line.startswith('@'):
            at, _, cmd = line.partition(' ')
            expr = AT_MAP.get(at, '')
        else:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            expr, cmd = ' '.join(parts[:5]), parts[5]
        slug = _slug(cmd.split()[0]) if cmd.split() else 'line'
        base, i = slug, 1
        while slug in seen:
            i += 1
            slug = f'{base}-{i}'
        seen.add(slug)
        d = {'id': f'exec.{slug}', 'owner': 'dev-ops', 'kind': 'exec',
             'entry': cmd, 'legacy_ref': cmd,
             'safety': {'writes_db': True, 'idempotent': False}}
        if expr:
            d['trigger'] = {'type': 'cron', 'expr': expr, 'tz': 'Asia/Taipei'}
        decls.append(d)
    return decls


def toml_dumps(d: dict) -> str:
    # 簡潔 TOML 序列化：標量頂層＋dict 用 [table] 段（白名單欄全覆蓋）
    scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in d.items() if isinstance(v, dict)}
    lines = []
    for k, v in scalars.items():
        lines.append(f'{k} = {json.dumps(v, ensure_ascii=False)}')
    for k, v in tables.items():
        lines.append(f'\n[{k}]')
        for kk, vv in v.items():
            lines.append(f'{kk} = {json.dumps(vv, ensure_ascii=False)}')
    return '\n'.join(lines) + '\n'


def write_all(decls: list[dict], out_dir: Path, force: bool) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for d in decls:
        p = out_dir / f"{d['id']}.toml"
        if p.exists() and not force:
            continue          # 人工修過的檔不覆蓋
        body = HEADER + toml_dumps(d)
        p.write_text(body, encoding='utf-8')
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(Path(__file__).resolve().parents[1]
                                         / 'schedule' / 'tasks.d'))
    ap.add_argument('--force', action='store_true',
                    help='覆蓋已存在的宣告檔（預設不覆蓋人工檔）')
    args = ap.parse_args(argv)

    decls: list[dict] = []
    for cmd in crontab_decls(cron_lines()):
        decls.append(cmd)
    for j in hermes_jobs():
        decls.append({'id': f"hermes.{j['profile']}.{j['id']}",
                      'owner': j['profile'], 'kind': 'hermes',
                      'hermes_job': j['id'], 'enabled': j['enabled'],
                      'legacy_ref': f"hermes job {j['id']}"})
    for ep in entry_point_names():
        decls.append({'id': f'inproc.{ep}', 'owner': 'dev-ops',
                      'kind': 'inproc', 'entry': ep,
                      'safety': {'writes_db': True, 'idempotent': False}})

    n = write_all(decls, Path(args.out), args.force)
    print(f'宣告檔寫入 {n} 份（跳過既存 {len(decls) - n} 份）→ {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
