# Funlab-Sched 開發指引

## 目錄
- [介紹](#介紹)
- [架構概覽](#架構概覽)
- [建立新任務](#建立新任務)
- [任務註冊與發現](#任務註冊與發現)
- [Web UI 整合](#web-ui-整合)
- [常見問題與解決方案](#常見問題與解決方案)
- [最佳實務](#最佳實務)

---

## 介紹

**Funlab-Sched** 是一個基於 APScheduler 的 Flask 外掛，為 Funlab-Flaskr 應用提供任務排程功能。主要特色：

- 支援週期性與一次性任務排程
- 提供 Web UI 管理介面
- 支援動態任務發現與載入
- 支援手動與自動執行
- 任務執行狀態追蹤與記錄

### 核心元件

| 元件 | 功能 |
|------|------|
| `SchedService` | 排程服務主類，負責任務載入與執行管理 |
| `SchedTask` | 任務基底類別，所有任務應繼承此類別 |
| `BackgroundScheduler` | APScheduler 的背景排程器 |
| `/sched/tasks` | Web UI 路由，用於顯示與操作任務 |

---

## 架構概覽

### 任務發現流程

```
┌─────────────────────────────────────────┐
│     應用啟動時                           │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│   SchedService.__init__()                │
│   - _load_config()                       │
│   - _load_tasks()                        │
│   - register_routes()                    │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│   load_plugins(group="funlab_sched_task")│
│   從 entry_points 載入所有註冊的任務        │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│   為每個任務建立實例                      │
│   - 呼叫 task.plan_schedule()             │
│   - 將任務加入排程器                      │
│   - 存到 sched_tasks 字典                 │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│   _scheduler.start()                     │
│   開始監聽並執行任務                      │
└─────────────────────────────────────────┘
```

### 設定層次

1. **pyproject.toml**（任務套件）
    - 使用 `[tool.poetry.plugins."funlab_sched_task"]` 註冊任務 entry point
    - 這是任務能被發現的必要條件

2. **config.toml**（主應用）
    - `[SchedService]` 配置排程器參數
    - `[TaskName]` 設定個別任務的參數與觸發器

3. **task.toml**（舊制）
    - 先前的備援設定，現已以 `config.toml` 為主

---

## 建立新任務

### 步驟 1：定義任務類別

在你的專案中建立任務類別，繼承 `SchedTask`：

```python
from dataclasses import dataclass, field
from wtforms import StringField, IntegerField
from wtforms.validators import DataRequired
from funlab.sched.task import SchedTask
from funlab.sched.service import SchedService
from datetime import date
import logging
from funlab.utils import log

@dataclass
class MyCustomTask(SchedTask):
    """
    自定义任务示例
    """
    param1: str = field(
        default='default_value',
        metadata={
            'type': StringField,
            'label': '参数 1',
            'description': '参数1的说明',
            'validators': [DataRequired()]
        }
    )

    param2: int = field(
        default=100,
        metadata={
            'type': IntegerField,
            'label': '参数 2',
            'description': '参数2的说明'
        }
    )

    manually: bool = field(
        default=True,
        metadata={'type': HiddenField}
    )

    def __init__(self, sched: SchedService):
        super().__init__(sched)
        self.dbmgr = sched.app.dbmgr

    def plan_schedule(self):
        """
        (可选) 动态规划任务的调度时间

        如果在 config.toml 中未定义 trigger，
        此方法会被调用以确定任务的调度计划。

        返回值：
            dict: APScheduler job 定义，包含 trigger、hour、minute 等
            None: 如果此任务不需要自动调度
        """
        today = date.today()
        plan = {'trigger': 'cron', 'minute': '30'}

        if today.weekday() < 5:  # 工作日
            plan.update({'hour': '9, 17'})
        else:  # 周末
            plan.update({'hour': '10'})

        return plan

    def execute(self, param1: str = 'default_value',
                param2: int = 100, manually=False, *args, **kwargs):
        """
        任务执行的入口方法

        参数应与 dataclass 字段对应
        """
        self.do_work(param1, param2, manually)

    def do_work(self, param1: str, param2: int, manually: bool = False):
        """
        实际的任务逻辑
        """
        mylogger = log.get_logger(__name__, level=logging.INFO)
        mylogger.info(f"开始执行任务: param1={param1}, param2={param2}, manually={manually}")

        try:
            # 你的业务逻辑
            result = self._process_data(param1, param2)
            mylogger.info(f"任务完成: {result}")
        except Exception as e:
            mylogger.error(f"任务失败: {e}")
            raise

    def _process_data(self, param1: str, param2: int):
        """实现具体的数据处理"""
        return f"处理 {param1} x {param2} 次"
```

### 步驟 2：在 pyproject.toml 註冊

在你專案的 `pyproject.toml` 中加入 entry_point：

```toml
[tool.poetry.plugins."funlab_sched_task"]
MyCustom = "your_package.module:MyCustomTask"
```

**重要**：使用 `funlab_sched_task` 作為 group 名稱，SchedService 才能找到你的任務。

### 步驟 3：在 config.toml 中設定

在主應用的 `config.toml` 中為任務加入設定（可選；若不設定則僅能手動執行）：

```toml
[MyCustomTask]
    # 如果需要自动调度，添加以下配置
    trigger = 'cron'
    day_of_week = 'mon,tue,wed,thu,fri'
    hour = 9
    minute = 30
    coalesce = true
    replace_existing = true

    # 默认参数
    [MyCustomTask.kwargs]
        param1 = 'initial_value'
        param2 = 200
```

### 步驟 4：安裝專案

```bash
# 在套件目錄
poetry install

# 或在主應用目錄更新套件
poetry update your_package
```

---

## 任務註冊與發現

### 運作原理

Funlab-Sched 使用 Python 的 `importlib.metadata.entry_points()` 機制自動發現任務：

1. **靜態註冊**：在 `pyproject.toml` 中聲明 entry point
2. **動態發現**：應用啟動時，SchedService 掃描所有 entry_points
3. **載入與實例化**：為每個發現的任務建立實例並完成設定

### 验证任务是否注册

```bash
# 检查 entry_points 是否包含你的任务
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
sched_tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])
for ep in sched_tasks:
    print(f'{ep.name}: {ep.value}')
"
```

---

## Web UI 整合

### 任務在 UI 的顯示

造訪 `http://127.0.0.1:5000/sched/tasks` 可檢視所有已註冊的任務。

### UI 功能

| 功能 | 說明 |
|------|------|
| 任務列表 | 顯示所有已載入的任務 |
| Last Status | 上次執行的狀態與時間 |
| Next Run | 下次執行的排程時間 |
| Trigger | 任務的觸發器設定 |
| Max Instances | 最多同時執行的實例數 |
| 參數對話框 | 點選任務名稱以開啟參數設定視窗 |

### 執行任務

1. 點選任務名稱以開啟參數對話框
2. 如需修改參數則進行變更
3. 點選「Run Task」按鈕立即執行
4. 點選「Save Args」以儲存為預設參數

---

## 常見問題與解決方案

### ❌ 問題 1：新註冊的任務在 Web UI 中不顯示

**症狀**：
- 任務已在 `pyproject.toml` 中定義
- 但在 Web UI 的任務列表看不到該任務

**可能原因**：
- 使用 `develop = true`（editable）安裝模式，導致虛擬環境中的 entry_points metadata 未更新
- 套件未被正確重新安裝或重建

**解決方案**：

#### 方案 A：完全重新安裝（建議）

```bash
# 1. 卸載目前套件
pip uninstall your_package -y

# 2. 以 editable 模式重新安裝
pip install -e /path/to/your_package

# 3. 驗證 entry_points
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
sched_tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])
for ep in sched_tasks:
    print(f'{ep.name}: {ep.value}')
"
```

#### 方案 B：使用 Poetry

```bash
# 在主應用目錄執行
cd /path/to/main/app

# 移除並重新加入依賴
poetry remove your_package
poetry add /path/to/your_package --editable

# 或直接更新套件
poetry update your_package
```

#### 方案 C：清除快取並重新安裝

```bash
# 在套件目錄
cd /path/to/your_package

# 清除建置快取
rm -rf dist/ *.egg-info .pytest_cache/

# 重新建置
poetry build

# 在虛擬環境中強制重新安裝
pip install --force-reinstall --no-cache-dir dist/your_package-*.whl
```

### ✅ 完整範例（以 CalcQuantV2 為例）

```bash
# 1. 在 finfun-quantanlys 的 pyproject.toml 新增或確認 entry point
# [tool.poetry.plugins."funlab_sched_task"]
# CalcQuantV2 = "finfun.quantanlys.task:CalcQuantV2Task"

# 2. 在 finfun-quantanlys 目錄安裝
cd D:\08.dev\fundlife\finfun-quantanlys
poetry install

# 3. 在主應用目錄卸載並重新以 editable 安裝
cd D:\08.dev\fundlife\finfun
pip uninstall finfun-quantanlys -y
pip install -e D:\08.dev\fundlife\finfun-quantanlys

# 4. 驗證 entry points
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
sched_tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])
for ep in sched_tasks:
    if 'CalcQuant' in ep.name:
        print(f'{ep.name}: {ep.value}')
"

# 5. 重啟應用並檢查 Web UI
# 停止 run.py，然後重新啟動
# 開啟 http://127.0.0.1:5000/sched/tasks
```

---

### ❌ 问题 2：任务初始化失败

**症状**：
```
ERROR: Task initialization failed
AttributeError: 'NoneType' object has no attribute 'dbmgr'
```

**原因**：
- `SchedTask.__init__()` 中的参数传递错误
- 未正确调用 `super().__init__(sched)`

**解決方案**：

```python
# ❌ 錯誤
def __init__(self, app):
    super().__init__(app)  # 錯誤！應該傳入 SchedService，而非 Flask app

# ✅ 正確
def __init__(self, sched: SchedService):
    super().__init__(sched)
    self.dbmgr = sched.app.dbmgr
```

---

### ❌ 问题 3：任务参数在 Web UI 中不显示

**症状**：
- 点击任务名称，参数对话框为空或参数显示不正确

**原因**：
- 未在 dataclass 字段的 metadata 中定义表单类型
- 使用了不支持的 wtforms 字段类型

**解决方案**：

```python
from wtforms import HiddenField, StringField, IntegerField, BooleanField

@dataclass
class MyTask(SchedTask):
    # ✅ 正确：指定了 type 和 validators
    name: str = field(
        default='default',
        metadata={
            'type': StringField,
            'label': 'Task Name',
            'validators': [DataRequired()]
        }
    )

    # ❌ 错误：没有 type metadata
    count: int = field(default=10)

    # ✅ 正确：隐藏字段
    internal_id: bool = field(
        default=True,
        metadata={'type': HiddenField}
    )
```

---

### ❌ 问题 4：任务执行失败但未收到通知

**症状**：
- 在 Web UI 中手动执行任务
- 没有看到执行结果或错误信息

**原因**：
- 异常被吞掉或未正确传递
- logger 配置不正确

**解决方案**：

```python
from funlab.utils import log
import logging

def execute(self, *args, **kwargs):
    mylogger = log.get_logger(__name__, level=logging.INFO)

    try:
        # 记录执行开始
        mylogger.info(f"任务开始执行: args={args}, kwargs={kwargs}")

        # 业务逻辑
        result = self._do_work()

        # 记录成功
        mylogger.info(f"任务执行成功: {result}")
        return result

    except Exception as e:
        # 记录错误并重新抛出
        mylogger.error(f"任务执行失败: {e}", exc_info=True)
        raise  # 重新抛出异常，让 SchedService 捕获并发送通知
```

---

## 最佳實務

### 1. 任務命名規範

```python
# ✅ 範例
class DataSyncTask(SchedTask):
    pass

class ReportGenerationTask(SchedTask):
    pass

# ❌ 避免
class Task1(SchedTask):
    pass

class MyFunction(SchedTask):
    pass
```

### 2. 使用 HiddenField 標示內部參數

```python
@dataclass
class MyTask(SchedTask):
    manually: bool = field(
        default=True,
        metadata={'type': HiddenField}  # 在 Web UI 中隱藏
    )

    internal_flag: bool = field(
        default=False,
        metadata={'type': HiddenField}
    )
```

### 3. 提供合理的預設值與驗證

```python
from wtforms.validators import DataRequired, Length, NumberRange

@dataclass
class ConfigTask(SchedTask):
    api_key: str = field(
        default='',
        metadata={
            'type': StringField,
            'label': 'API Key',
            'validators': [DataRequired(), Length(min=10)],
            'description': '來自 API 提供者的金鑰'
        }
    )

    timeout: int = field(
        default=30,
        metadata={
            'type': IntegerField,
            'label': 'Timeout (秒)',
            'validators': [NumberRange(min=1, max=300)],
            'description': '請求超時時間'
        }
    )
```

### 4. 實作 plan_schedule() 以支援動態排程

```python
def plan_schedule(self):
    """
    根據當前日期動態調整任務排程

    例如：財報任務在財報公布期間會更頻繁執行
    """
    from datetime import date
    from finfun.utils import fin_cale

    today = date.today()
    plan = {'trigger': 'cron', 'minute': '10'}

    # 財報發布期間：每天多個時間點執行
    if (today.month in [3, 4, 5, 8, 11] and today.day <= 31):
        plan.update({'hour': '2, 10, 16, 21'})
    else:
        # 非財報期間：每月一次
        next_report = fin_cale.nearest_finstmt_will_issue_period_start_dt(today)
        plan.update({
            'year': next_report.year,
            'month': next_report.month - 1,
            'day': '28',
            'hour': '2'
        })

    return plan
```

### 5. 正確處理資料庫會話

```python
def execute(self, *args, **kwargs):
    mylogger = log.get_logger(__name__)

    # 使用 dbmgr 的上下文管理
    with self.dbmgr.session_context() as sa_session:
        try:
            # 在會話中執行資料庫操作
            results = sa_session.query(MyEntity).all()

            # 處理資料
            for result in results:
                self._process_result(result)

            mylogger.info("資料處理完成")

        except Exception as e:
            mylogger.error(f"資料庫操作失敗: {e}")
            raise
```

### 6. 使用日誌追蹤任務進度

```python
def execute(self, *args, **kwargs):
    mylogger = log.get_logger(__name__, level=logging.INFO)
    start_time = time.time()

    try:
        total_items = 1000

        for idx in range(total_items):
            # 處理項目
            self._process_item(idx)

            # 定期輸出進度
            if (idx + 1) % 100 == 0:
                elapsed = time.time() - start_time
                speed = (idx + 1) / elapsed
                eta = (total_items - idx - 1) / speed
                mylogger.info(f"進度: {idx+1}/{total_items}, 預估剩餘時間: {eta:.0f} 秒")

        elapsed = time.time() - start_time
        mylogger.info(f"任務完成，耗時: {elapsed:.2f} 秒")

    except Exception as e:
        mylogger.error(f"任務失敗: {e}")
        raise
```

### 7. 撰寫任務描述與文件

```python
@dataclass
class DataProcessTask(SchedTask):
    """
    資料處理任務

    此任務負責：
    1. 從資料來源擷取資料
    2. 資料清洗與轉換
    3. 結果寫入資料庫
    4. 產生處理報告
    """

    source: str = field(
        default='api',
        metadata={
            'type': StringField,
            'label': '資料來源',
            'description': '資料來源：api, file, database'
        }
    )
```

---

## 参考资源

- [APScheduler 文档](https://apscheduler.readthedocs.io/)
- [Funlab-Flaskr](https://github.com/sunnylin13/funlab-flaskr)
- [WTForms 文档](https://wtforms.readthedocs.io/)

---

## 故障排除流程图

```
任务在 Web UI 中不显示
        │
        ▼
检查 pyproject.toml 中的 entry_point
    │         │
  ✓ 定义了    ✗ 未定义
    │         │
    ▼         ▼
检查虚拟环境   添加 entry_point：
中的注册      [tool.poetry.plugins."funlab_sched_task"]
    │         YourTask = "module:YourTask"
    │         然后 pip install -e .
  ✓ 已注册    │
    │         ▼
    ▼         重新运行验证命令
验证应用
    │         ✓ 现在显示了
    │         │
  ✓ 加载了    ▼
    │         重启应用
    │
    ▼
重启应用      ✓ 任务可见
并访问
Web UI       ✓ 完成！

✗ 仍未显示？
    │
    ▼
清除缓存：
rm -rf dist *.egg-info
poetry build
pip install --force-reinstall
    │
    ▼
重新验证
```

---

**版本**: 1.0
**最後更新**: 2026-02-10
**維護者**: Development Team
