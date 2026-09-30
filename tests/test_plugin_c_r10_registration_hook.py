"""R10（kanban t_e56e99f5）：SchedService 改監聽 plugins_registration_complete，
移除 plugin_name in {'pluginmanager','PluginManagerView'} 字串匹配。
180s daemon Timer 兜底邏輯必須原樣保留（紅線）。
"""
import inspect
import types

from funlab.sched.service import SchedService


def _stub(loader_calls):
    svc = types.SimpleNamespace()
    svc._start_loader_thread_once = lambda: loader_calls.append(True)
    return svc


def test_hook_callback_starts_loader_without_plugin_name_match():
    """新 hook 的 context 不帶 plugin_name——回呼不得再做字串比對。"""
    calls: list[bool] = []
    SchedService._hook_start_loader_after_fundmgr(_stub(calls), {})
    assert calls == [True]
    calls.clear()
    # 舊語意：其他 plugin_name 不觸發。新語意：一律觸發（一次性 hook）。
    SchedService._hook_start_loader_after_fundmgr(_stub(calls), {"plugin_name": "whatever"})
    assert calls == [True]


def test_registers_new_hook_and_drops_string_matching():
    src = inspect.getsource(SchedService.__init__)
    assert "plugins_registration_complete" in src, "__init__ 必須改註冊 plugins_registration_complete"
    assert "'plugin_after_init'" not in src and '"plugin_after_init"' not in src, \
        "不得再監聽 plugin_after_init"
    hook_src = inspect.getsource(SchedService._hook_start_loader_after_fundmgr)
    assert "pluginmanager" not in hook_src and "PluginManagerView" not in hook_src


def test_180s_daemon_timer_fallback_preserved():
    """紅線：180s daemon Timer 兜底不得移除。"""
    src = inspect.getsource(SchedService.__init__)
    assert "threading.Timer(180.0" in src
    assert "fallback_timer.daemon = True" in src
