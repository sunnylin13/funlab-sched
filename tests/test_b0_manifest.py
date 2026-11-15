"""宣告檔 schema＋載入器測試（卡 t_bdf4eb9c 工項④；報告 §3.3 條文＝獨立期望來源）。

期望值出處：KB Report-fund13-schedule-task統一排程層規劃評估-2026-11-15.md §3.3
範例 TOML 逐字（§3.3 L197-214）＋卡面白名單鍵要求。
"""
import textwrap

import pytest

from funlab.sched.manifest import load_dir, load_file

# §3.3 範例逐字（獨立來源：KB 報告 L197-214）
SPEC_33_EXAMPLE = textwrap.dedent('''
    id            = "fi_guards.gate1"
    owner         = "dev-ops"
    kind          = "exec"
    entry         = ".deploy/fi_guards.sh gate1"
    trigger       = { type = "cron", expr = "0 10 * * *", tz = "Asia/Taipei", calendar = "daily" }
    timeout_sec   = 600
    retry         = { max = 2, backoff_sec = [60, 300], on = ["exit_nonzero", "timeout"] }
    misfire       = { grace_sec = 900, policy = "run_once" }
    concurrency   = { key = "pg_prod_write", max = 1 }
    depends_on    = [{ task = "factor.refresh", within = "same_day", on_fail = "skip_and_alert" }]
    safety        = { writes_db = false, idempotent = true }
    alert         = { channel = "hermes:fund13-dev-pm", on = ["failed", "missed", "timeout", "retry_exhausted"], dedupe_hours = 6 }
    heartbeat     = { max_silence_hours = 26 }
    lifecycle     = { kind = "recurring" }
    enabled       = true
    legacy_ref    = "crontab:A06"
''')


def _write(tmp_path, name, text):
    p = tmp_path / 'tasks.d'
    p.mkdir(exist_ok=True)
    (p / name).write_text(textwrap.dedent(text), encoding='utf-8')
    return p


class TestLoadFileHappy:
    def test_section_33_example_loads_all_fields(self, tmp_path):
        d = _write(tmp_path, 'fi_guards.gate1.toml', SPEC_33_EXAMPLE)
        specs, errors = load_dir(d)
        assert errors == []
        assert set(specs) == {'fi_guards.gate1'}
        s = specs['fi_guards.gate1']
        assert s.owner == 'dev-ops'
        assert s.kind == 'exec'
        assert s.entry == '.deploy/fi_guards.sh gate1'
        assert s.trigger == {'type': 'cron', 'expr': '0 10 * * *',
                             'tz': 'Asia/Taipei', 'calendar': 'daily'}
        assert s.timeout_sec == 600
        assert s.retry == {'max': 2, 'backoff_sec': [60, 300],
                           'on': ['exit_nonzero', 'timeout']}
        assert s.misfire == {'grace_sec': 900, 'policy': 'run_once'}
        assert s.concurrency == {'key': 'pg_prod_write', 'max': 1}
        assert s.depends_on == [{'task': 'factor.refresh', 'within': 'same_day',
                                 'on_fail': 'skip_and_alert'}]
        assert s.safety['writes_db'] is False and s.safety['idempotent'] is True
        assert s.alert['dedupe_hours'] == 6
        assert s.heartbeat['max_silence_hours'] == 26
        assert s.lifecycle == {'kind': 'recurring'}
        assert s.enabled is True
        assert s.legacy_ref == 'crontab:A06'

    def test_enabled_defaults_true_and_minimal_spec(self, tmp_path):
        d = _write(tmp_path, 'x.toml', '''
            id = "inproc.FetchDailyPrice"
            owner = "dev-ops"
            kind = "inproc"
            entry = "FetchDailyPriceTask"
            safety = { writes_db = true, idempotent = false }
        ''')
        specs, errors = load_dir(d)
        assert errors == []
        assert specs['inproc.FetchDailyPrice'].enabled is True


class TestUnknownKeyRejection:
    """卡面：白名單鍵驗證，未知鍵拒載並告警（＝回傳 errors 供呼叫端告警）。"""

    @pytest.mark.parametrize('fragment,bad_key', [
        ('bogus_top = 1\n', 'bogus_top'),
        ('retry = { max = 1, jitter = 5 }\n', 'retry.jitter'),
        ('concurrency = { key = "k", lock = "x" }\n', 'concurrency.lock'),
        ('safety = { writes_db = true, idempotent = true, careful = false }\n', 'safety.careful'),
        ('lifecycle = { kind = "recurring", autoremove = true }\n', 'lifecycle.autoremove'),
        ('alert = { channel = "log", on = ["failed"], mention = "boss" }\n', 'alert.mention'),
        ('heartbeat = { max_silence_hours = 1, email = "a@b" }\n', 'heartbeat.email'),
        ('misfire = { grace_sec = 1, policy = "run_once", extra = 2 }\n', 'misfire.extra'),
        ('depends_on = [{ task = "t", within = "same_day", on_fail = "skip_and_alert", cc = 1 }]\n',
         'depends_on[0].cc'),
        ('trigger = { type = "cron", expr = "0 1 * * *", tz = "Asia/Taipei", jitter = 3 }\n',
         'trigger.jitter'),
    ])
    def test_unknown_key_rejected_not_silently_dropped(self, tmp_path, fragment, bad_key):
        # SCH-14 事故防再發：未知鍵不得流入後續 add_job；拒載＋錯誤回報。
        base = 'id = "a"\nowner = "dev-ops"\nkind = "exec"\nentry = "true"\n'
        d = _write(tmp_path, 'a.toml', base + fragment)
        specs, errors = load_dir(d)
        assert specs == {}
        assert any(bad_key in e for e in errors)

    def test_unknown_key_alertable_via_errors(self, tmp_path):
        d = _write(tmp_path, 'a.toml', 'id = "a"\nowner = "o"\nkind = "exec"\nentry = "x"\nnope = 1\n')
        specs, errors = load_dir(d)
        assert any('a.toml' in e for e in errors)  # 錯誤訊息帶檔名供告警


class TestEnumAndRequiredValidation:
    @pytest.mark.parametrize('fragment,expect_kw', [
        ('kind = "daemon"\n', 'kind'),
        ('kind = "exec"\ntrigger = { type = "weird" }\n', 'trigger.type'),
        ('kind = "exec"\nlifecycle = { kind = "eternal" }\n', 'lifecycle.kind'),
        ('kind = "exec"\nmisfire = { policy = "queue_up" }\n', 'misfire.policy'),
        ('kind = "exec"\nretry = { max = 1, on = ["rm_rf"] }\n', 'retry.on'),
        ('kind = "exec"\nalert = { on = ["party_time"] }\n', 'alert.on'),
        ('kind = "exec"\ndepends_on = [{ task = "t", within = "same_century", on_fail = "skip_and_alert" }]\n',
         'depends_on'),
        ('kind = "exec"\ndepends_on = [{ task = "t", within = "same_day", on_fail = "nuke" }]\n',
         'depends_on'),
    ])
    def test_enum_violation_rejected(self, tmp_path, fragment, expect_kw):
        d = _write(tmp_path, 'a.toml', 'id = "a"\nowner = "o"\n' + fragment)
        specs, errors = load_dir(d)
        assert specs == {}
        assert any(expect_kw in e for e in errors)

    def test_missing_required_fields(self, tmp_path):
        d = _write(tmp_path, 'a.toml', 'id = "a"\n')
        specs, errors = load_dir(d)
        assert specs == {}
        joined = ' '.join(errors)
        assert 'owner' in joined and 'kind' in joined

    def test_exec_and_inproc_require_entry(self, tmp_path):
        for kind in ('exec', 'inproc'):
            d = _write(tmp_path, f'{kind}.toml', f'id = "a"\nowner = "o"\nkind = "{kind}"\n')
            specs, errors = load_dir(d)
            assert specs == {}, kind
            assert any('entry' in e for e in errors)

    def test_once_needs_expires_at_converge_needs_stop_when(self, tmp_path):
        d = _write(tmp_path, 'a.toml',
                   'id = "a"\nowner = "o"\nkind = "exec"\nentry = "x"\nlifecycle = { kind = "once" }\n')
        specs, errors = load_dir(d)
        assert specs == {} and any('expires_at' in e for e in errors)
        d = _write(tmp_path, 'b.toml',
                   'id = "b"\nowner = "o"\nkind = "exec"\nentry = "x"\nlifecycle = { kind = "converge" }\n')
        specs, errors = load_dir(d)
        assert specs == {} and any('stop_when' in e for e in errors)

    def test_exec_inproc_require_safety_block(self, tmp_path):
        # 金融寫入紀律：safety 不得有預設值，強制宣告。
        d = _write(tmp_path, 'a.toml', 'id = "a"\nowner = "o"\nkind = "exec"\nentry = "x"\n')
        specs, errors = load_dir(d)
        assert specs == {} and any('safety' in e for e in errors)


class TestLoadDirDuplicates:
    def test_duplicate_id_rejected(self, tmp_path):
        d = tmp_path / 'tasks.d'
        d.mkdir()
        base = 'id = "dup"\nowner = "o"\nkind = "hermes"\nhermes_job = "x"\n'
        (d / 'a.toml').write_text(base, encoding='utf-8')
        (d / 'b.toml').write_text(base.replace('hermes_job = "x"', 'hermes_job = "y"'), encoding='utf-8')
        specs, errors = load_dir(d)
        assert 'dup' not in specs
        assert any('dup' in e and 'duplicate' in e.lower() for e in errors)

    def test_hermes_kind_without_safety_ok(self, tmp_path):
        # hermes/os 為納管登記（本層不執行），safety 非必填
        d = _write(tmp_path, 'h.toml', 'id = "her.abc"\nowner = "manager"\nkind = "hermes"\nhermes_job = "abc"\n')
        specs, errors = load_dir(d)
        assert errors == []
        assert specs['her.abc'].hermes_job == 'abc'

    def test_missing_dir_returns_error(self, tmp_path):
        specs, errors = load_dir(tmp_path / 'nope')
        assert specs == {}
        assert errors

    def test_malformed_toml_reported(self, tmp_path):
        d = _write(tmp_path, 'bad.toml', 'id = "a" oops [[[\n')
        specs, errors = load_dir(d)
        assert specs == {} and errors
