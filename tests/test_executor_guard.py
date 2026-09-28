"""SCH-08/SCH-09：執行器防呆＋庫內預設配置（misfire_grace_time=300、max_instances=1、無 processpool）。"""
from types import SimpleNamespace

from funlab.sched.task import SchedTask


class _FakeLogger:
    def __init__(self):
        self.messages = []

    def warning(self, msg='', *a, **k):
        self.messages.append(('warning', str(msg)))

    def info(self, msg='', *a, **k):
        self.messages.append(('info', str(msg)))


def _bare_task(stub_service):
    """跳過重 __init__ 的 SchedTask 執行期實例（只測 _validate_runtime_executable）。"""
    class _T(SchedTask):
        def execute(self, *a, **k):
            pass
    t = _T.__new__(_T)
    t.sched = stub_service
    t.name = 'Guard'
    t._task_def = {'id': 'Guard', 'name': 'Guard'}
    t.mylogger = _FakeLogger()
    return t


def test_non_default_executor_warns(stub_service):
    t = _bare_task(stub_service)
    t._task_def.update({'executor': 'processpool'})
    t._validate_runtime_executable()
    assert any('processpool' in m[1] for m in t.mylogger.messages if m[0] == 'warning')


def test_default_executor_silent(stub_service):
    t = _bare_task(stub_service)
    t._validate_runtime_executable()
    assert t.mylogger.messages == []


def test_default_conf_has_no_processpool():
    import pathlib
    import tomllib
    data = tomllib.loads((pathlib.Path(__file__).resolve().parents[1]
                          / 'funlab/sched/conf/plugin.toml').read_text(encoding='utf-8'))
    executors = data['SchedService'].get('executors', {})
    assert 'processpool' not in executors
    assert 'misfire_grace_time' in data['SchedService'].get('job_defaults', {})


def test_default_conf_max_instances_is_1():
    import pathlib
    import tomllib
    data = tomllib.loads((pathlib.Path(__file__).resolve().parents[1]
                          / 'funlab/sched/conf/plugin.toml').read_text(encoding='utf-8'))
    assert data['SchedService']['job_defaults']['max_instances'] == 1


def test_default_conf_misfire_grace_time_is_300():
    import pathlib
    import tomllib
    data = tomllib.loads((pathlib.Path(__file__).resolve().parents[1]
                          / 'funlab/sched/conf/plugin.toml').read_text(encoding='utf-8'))
    assert data['SchedService']['job_defaults']['misfire_grace_time'] == 300
