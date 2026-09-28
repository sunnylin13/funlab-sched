"""copy-on-write 發佈：載入中讀者不會炸 RuntimeError（SCH-04，實證 E7）。"""
import threading

from types import SimpleNamespace


class _Task:
    def __init__(self, tid):
        self.id = tid


def _call(service, name, *a):
    from funlab.sched.service import SchedService
    return getattr(SchedService, name)(service, *a)


def _stub():
    svc = SimpleNamespace()
    svc.sched_tasks = {}
    svc._task_build = None
    return svc


def test_reader_never_sees_half_built_dict():
    svc = _stub()
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                for _ in _call(svc, '_snapshot_tasks'):
                    pass
            except Exception as e:          # pragma: no cover - 缺陷時才會進
                errors.append(e)

    def writer():
        for i in range(30000):
            _call(svc, '_stage_task', _Task(f'k{i}'))
            _call(svc, '_publish_tasks')
        stop.set()

    t1, t2 = threading.Thread(target=reader), threading.Thread(target=writer)
    t1.start(); t2.start(); t2.join(timeout=30); stop.set(); t1.join(timeout=5)
    assert errors == []
    assert len(svc.sched_tasks) == 30000


def test_publish_swaps_whole_snapshot():
    svc = _stub()
    _call(svc, '_stage_task', _Task('A'))
    _call(svc, '_stage_task', _Task('B'))
    snapshot_before = svc.sched_tasks
    _call(svc, '_publish_tasks')
    assert set(snapshot_before) == set()          # 舊快照不受後續寫入污染
    assert set(svc.sched_tasks) == {'A', 'B'}
