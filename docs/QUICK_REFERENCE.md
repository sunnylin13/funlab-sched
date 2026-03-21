# Funlab-Sched 快速參考指南

## 快速建立任務清單

### ✅ 第 1 步：定義任務（約 5 分鐘）

在 `your_package/task.py` 中：

```python
from dataclasses import dataclass, field
from wtforms import StringField, IntegerField, HiddenField
from wtforms.validators import DataRequired
from funlab.sched.task import SchedTask
from funlab.sched.service import SchedService
from funlab.utils import log
import logging

@dataclass
class YourCustomTask(SchedTask):
    param1: str = field(
        default='value',
        metadata={'type': StringField, 'label': 'Param 1', 'validators': [DataRequired()]}
    )
    manually: bool = field(default=True, metadata={'type': HiddenField})

    def __init__(self, sched: SchedService):
        super().__init__(sched)
        self.dbmgr = sched.app.dbmgr

    def plan_schedule(self):
        # 可选：动态调度
        return {'trigger': 'cron', 'hour': 9, 'minute': 0}

    def execute(self, param1='value', manually=False, *args, **kwargs):
        mylogger = log.get_logger(__name__, level=logging.INFO)
        mylogger.info(f"执行任务: {param1}")
        # 你的业务逻辑
        pass
```

### ✅ 第 2 步：註冊任務（約 1 分鐘）

在 `pyproject.toml` 中：

```toml
[tool.poetry.plugins."funlab_sched_task"]
YourCustom = "your_package.task:YourCustomTask"
```

### ✅ 第 3 步：設定任務（約 2 分鐘）

在主應用的 `config.toml` 中：

```toml
[YourCustomTask]
    trigger = 'cron'
    hour = 9
    minute = 0
```

### ✅ 第 4 步：安裝並驗證（約 5 分鐘）

```bash
# 在你的套件目錄
poetry install

# 在主應用目錄（先卸載再以 editable 安裝）
pip uninstall your_package -y
pip install -e /path/to/your_package

# 驗證 entry points
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])
for ep in tasks:
    print(f'{ep.name}: {ep.value}')
"

# 若看到你的任務 → 重啟應用並開啟 http://127.0.0.1:5000/sched/tasks
# 若看不到 → 參考下方 "常見問題"
```

---

## 常用指令

### 檢視所有已註冊的任務

```bash
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
sched_tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])
print('已註冊的任務:')
for ep in sched_tasks:
    print(f'  {ep.name}: {ep.value}')
"
```

### 重新安裝套件（常用於修復 entry_point 問題）

```bash
# 方法 1：以 editable 模式重新安裝（最常用）
cd /path/to/your_package
pip uninstall your_package -y
pip install -e .

# 方法 2：使用 Poetry（建議用於 lockfile 管理）
cd /path/to/main/app
poetry update your_package

# 方法 3：清除快取並重新建置安裝
cd /path/to/your_package
rm -rf dist/ *.egg-info .pytest_cache/
poetry build
pip install --force-reinstall dist/your_package*.whl
```

### 檢視任務日誌

```bash
# 應用啟動時的任務載入日誌
tail -f log/app.log | grep "Loading task"

# 任務執行日誌
tail -f log/task.log | grep "YourCustomTask"
```

---

## 欄位類型與驗證器

### 支援的欄位類型

| 欄位類型 | 說明 | 範例 |
|---------|------|------|
| `StringField` | 文字輸入 | 名稱、路徑 |
| `IntegerField` | 整數輸入 | 計數、timeout |
| `FloatField` | 浮點數 | 百分比、比率 |
| `BooleanField` | 布林值 | 開關選項 |
| `DateField` | 日期選擇 | 起始/結束日期 |
| `HiddenField` | 隱藏欄位 | 內部旗標 |

### 常用驗證器

```python
from wtforms.validators import DataRequired, Length, NumberRange, Email, Regexp

@dataclass
class Example(SchedTask):
    # 必填
    name: str = field(
        default='',
        metadata={'type': StringField, 'validators': [DataRequired()]}
    )

    # 長度限制
    code: str = field(
        default='',
        metadata={'type': StringField, 'validators': [Length(min=3, max=10)]}
    )

    # 數值範圍
    count: int = field(
        default=10,
        metadata={'type': IntegerField, 'validators': [NumberRange(min=1, max=1000)]}
    )

    # 郵件驗證
    email: str = field(
        default='',
        metadata={'type': StringField, 'validators': [Email()]}
    )

    # 正則匹配
    pattern: str = field(
        default='',
        metadata={
            'type': StringField,
            'validators': [Regexp(r'^[a-zA-Z0-9]+$', message='只允許英數字')]
        }
    )
```

---

## 設定參考

### 觸發器（trigger）設定

```toml
# ---- Cron 觸發器 ----
[MyCronTask]
    trigger = 'cron'
    year = '2026'          # 可選
    month = '1-6'          # 1-12 或範圍
    day = '1-15'           # 1-31 或範圍
    week = '*'             # 1-53 或 *
    day_of_week = 'mon-fri' # mon-sun
    hour = '0-23'          # 0-23
    minute = '*/15'        # 0-59，*/15 表示每 15 分鐘
    second = '0'           # 0-59

# ---- Interval 觸發器 ----
[MyIntervalTask]
    trigger = 'interval'
    weeks = 0
    days = 1
    hours = 0
    minutes = 30
    seconds = 0

# ---- Date 觸發器（一次性）----
[MyOnceTask]
    trigger = 'date'
    run_date = '2026-12-31 23:59:59'

# ---- 通用設定 ----
[AnyTask]
    coalesce = true           # 合併錯過的執行
    max_instances = 1         # 最多同時執行的實例數
    replace_existing = true   # 同名任務是否取代
```

### Cron 範例

```toml
# 每天 09:00
[Task]
    trigger = 'cron'
    hour = 9
    minute = 0

# 每個工作日 09:00 與 17:00
[Task]
    trigger = 'cron'
    day_of_week = 'mon-fri'
    hour = '9,17'
    minute = 0

# 每月 1 日 02:00
[Task]
    trigger = 'cron'
    day = 1
    hour = 2
    minute = 0

# 每 15 分鐘
[Task]
    trigger = 'cron'
    minute = '*/15'

# 每週一、三、五 10:30
[Task]
    trigger = 'cron'
    day_of_week = 'mon,wed,fri'
    hour = 10
    minute = 30

# 每月最後一天 23:59
[Task]
    trigger = 'cron'
    day = 'last'
    hour = 23
    minute = 59
```

---
### 常見錯誤處理

### 問題 1：entry_point 已註冊但任務未載入

```bash
# 步驟 1：驗證 entry_point 是否已註冊
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])
found = [t for t in tasks if 'YourTask' in t.name]
print('YourTask 已註冊' if found else '❌ YourTask 未註冊')
"

# 步驟 2：檢查應用啟動日誌是否有載入訊息，如 "Loading task YourTask ..."

# 步驟 3：重新啟動應用並檢查日誌
```

### 問題 2：參數在 Web UI 中顯示不正確

```python
# ❌ 錯誤：缺少 metadata
@dataclass
class Bad(SchedTask):
    param: int = field(default=10)

# ✅ 正確：完整的 metadata
@dataclass
class Good(SchedTask):
    param: int = field(
        default=10,
        metadata={
            'type': IntegerField,
            'label': 'My Parameter',
            'description': '參數說明'
        }
    )
```

### 問題 3：任務執行失敗但未顯示錯誤

```python
# 確保使用 mylogger 並設定正確日誌等級
def execute(self, *args, **kwargs):
    mylogger = log.get_logger(__name__, level=logging.INFO)

    try:
        mylogger.info("開始執行")
        # 業務邏輯
        mylogger.info("執行成功")
    except Exception as e:
        # 重要：使用 exc_info=True 紀錄完整錯誤堆疊
        mylogger.error(f"執行失敗: {e}", exc_info=True)
        raise  # 重新拋出讓框架知道失敗
```

### 問題 4：動態排程 (plan_schedule) 不生效

```python
# ❌ 錯誤：在 __init__ 中直接修改 task_def
def __init__(self, sched):
    super().__init__(sched)
    self._task_def.update({'hour': 9})  # 太晚執行

# ✅ 正確：於 plan_schedule 中回傳
def plan_schedule(self):
    return {'trigger': 'cron', 'hour': 9, 'minute': 0}
```

---

## 偵錯技巧

### 列印 dataclass 欄位

```python
from dataclasses import fields

@dataclass
class MyTask(SchedTask):
    pass

# 在 __init__ 中偵錯
def __init__(self, sched):
    super().__init__(sched)
    for f in fields(self):
        print(f"{f.name}: {getattr(self, f.name)}")
```

### 驗證任務設定

```python
def __init__(self, sched):
    super().__init__(sched)
    # 列印任務定義
    import json
    mylogger.info(f"Task definition: {json.dumps(self.task_def, indent=2, default=str)}")
    # 列印表單欄位
    mylogger.info(f"Form fields: {list(self.form_class._unbound_fields)}")
```

### 檢查資料庫連線

```python
def __init__(self, sched):
    super().__init__(sched)
    try:
        with self.dbmgr.session_context() as sa_session:
            # 測試查詢
            result = sa_session.execute("SELECT 1")
            mylogger.info("✓ 資料庫連線正常")
    except Exception as e:
        mylogger.error(f"✗ 資料庫連線失敗: {e}")
```
# ✅ 高效：使用 eager loading
from sqlalchemy.orm import joinedload
items = session.query(Item).options(joinedload(Item.related)).all()
for item in items:
    print(item.related.name)  # 单次查询
```

### 2. 批量处理

```python
# ❌ 低效：逐个插入
for data in large_dataset:
    session.add(MyEntity(**data))

# ✅ 高效：批量插入
session.bulk_insert_mappings(MyEntity, large_dataset)
session.commit()
```

### 3. 分页处理大数据集

```python
def execute(self, *args, **kwargs):
    mylogger = log.get_logger(__name__)
    with self.dbmgr.session_context() as sa_session:
        page_size = 1000
        offset = 0

        while True:
            items = sa_session.query(Item)\
                .limit(page_size)\
                .offset(offset)\
                .all()

            if not items:
                break

            for item in items:
                self._process_item(item)

            offset += page_size
            mylogger.info(f"已处理 {offset} 条记录")
```

---

## 最佳实践检查清单

- [ ] 继承 `SchedTask`
- [ ] 使用 `@dataclass` 装饰器
- [ ] 在 `__init__` 中调用 `super().__init__(sched)`
- [ ] 在 `pyproject.toml` 中注册 entry_point
- [ ] 为所有非隐藏参数定义 metadata
- [ ] 实现 `execute()` 方法
- [ ] (可选) 实现 `plan_schedule()` 方法
- [ ] 使用 `log.get_logger()` 记录日志
- [ ] 在异常时使用 `exc_info=True`
- [ ] 在 `config.toml` 中定义任务配置
- [ ] 测试任务的手动执行
- [ ] 验证参数在 Web UI 中正确显示
- [ ] 记录和监控执行时间

---

## 参考文件

| 文件 | 用途 |
|------|------|
| [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) | 完整开发指引 |
| [../README.md](../README.md) | 项目简介 |
| [../funlab/sched/task.py](../funlab/sched/task.py) | SchedTask 基类 |
| [../funlab/sched/service.py](../funlab/sched/service.py) | SchedService 实现 |

---

**快速参考版本**: 1.0
**最后更新**: 2026-02-10
