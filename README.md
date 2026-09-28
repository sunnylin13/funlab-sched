# funlab-sched

排程模組，基於 APScheduler 提供定時任務管理，整合 finfun 業務排程。

## APScheduler 行為說明（與 `funlab/sched/conf/plugin.toml` 實際值一致）

### 初始化

- 使用 BackgroundScheduler（非同步背景執行緒）
- Job Store 預設為**記憶體**（程序重啟即失；跨重啟的任務請一律以 task.toml/config 重新註冊，勿依賴持久化）
- Executor 僅提供 ThreadPoolExecutor（default, max_workers=20）。任務 func 是綁定方法（持 scheduler 引用），processpool 必然 pickle 失敗，故不提供（SCH-08）
- 多進程 WSGI（gunicorn/uwsgi）下排程器預設拒絕啟動——每 worker 各起一份排程器會使任務重複執行；正式配置為 waitress 單進程，特殊場景可明示 `[SchedService] ALLOW_MULTI_WORKER_SCHEDULER=true` 放行（SCH-07）

### 任務失敗處理

- 任務失敗時記錄 ERROR log（含 traceback）
- 若安裝 funlab-sse，失敗通知透過 SSE 推播至前端
- 若安裝 funlab-auth 通知功能，可選 LINE/Email 告警（T-finfun-003 決議）

### Job Coalescing

- coalesce=true：若任務積壓（因上次執行過久），僅補跑一次
- max_instances=1：防止同一任務並發執行；確需併發的任務在自己的 task.toml `[TaskName]` 明示 `max_instances = N` 覆寫

### Misfire 處理

- misfire_grace_time=300：重啟/暫停後 5 分鐘內的遲到任務仍補跑（APScheduler 未設時的裸預設僅 1 秒）
- 超過則跳過並記錄警告
- 非冪等任務須自行保證補跑安全（既有 finfetch 任務皆 upsert 語意）

## 啟動方式

無需手動註冊。本套件以 PEP 621 entry point 由 PluginManager 自動載入：

```toml
# pyproject.toml（funlab-sched 自身）
[project.entry-points."funlab_plugin"]
SchedService = "funlab.sched.service:SchedService"
```

業務任務則由 finfun-* 各套件註冊 `[project.entry-points."funlab_sched_task"]`，
SchedService 啟動時自動發現並排入（背景載入，`BACKGROUND_TASK_LOADING = true`）。
