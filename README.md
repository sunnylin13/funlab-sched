# funlab-sched

排程模組，基於 APScheduler 提供定時任務管理，整合 finfun 業務排程。

## APScheduler 行為說明

### 初始化

- 使用 BackgroundScheduler（非同步背景執行緒）
- Job Store 預設為 SQLAlchemy（使用主資料庫）
- Executor 預設為 ThreadPoolExecutor（max_workers=10）

### 任務失敗處理

- 任務失敗時記錄 ERROR log（含 traceback）
- 若設定 unlab-sse，失敗通知透過 SSE 推播至前端
- 若設定 unlab-auth 通知功能，可選 LINE/Email 告警（T-finfun-003 決議）

### Job Coalescing

- coalesce=True：若任務積壓（因上次執行過久），僅補跑一次
- max_instances=1：防止同一任務並發執行

### Misfire 處理

- misfire_grace_time=300：5 分鐘內的遲到任務仍執行
- 超過則跳過並記錄警告

## 啟動方式

`python
from funlab.sched import scheduler_plugin
app.register_blueprint(scheduler_plugin.blueprint)
scheduler_plugin.setup(app)
`

## 待實作（Wave 3）

- T-sched-001：PluginManager 整合（依賴 T-libs-003，已完成）
- T-sched-002：失敗通知集中化
