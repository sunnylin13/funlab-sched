
# Funlab-Sched 問題排查決策樹

遇到常見問題時，依此決策樹可快速定位原因並採取修復步驟。

---

## 問題 1：任務在 Web UI 中不顯示

```
任務在 Web UI 中不顯示
    │
    ├─ 檢查：pyproject.toml 是否定義 entry_point?
    │
    ├─ 否 → 新增 entry_point
    │   │
    │   ├─ 範例：
    │   ├─ [tool.poetry.plugins."funlab_sched_task"]
    │   ├─ MyTask = "package.module:MyTaskClass"
    │   │
    │   └─ 然後執行：pip install -e /path/to/package
    │
    ├─ 是 → 檢查：entry_point 是否已在虛擬環境中註冊?
    │       執行：python diagnose_tasks.py
    │   │
    │   ├─ 已註冊 → 檢查：任務類別是否可導入?
    │   │   │
    │   │   ├─ 可導入 → 檢查：應用啟動日誌
    │   │   │   │
    │   │   │   ├─ 有 "Loading task MyTask" → 重啟應用
    │   │   │   │
    │   │   │   └─ 無 → 檢查：__init__ 是否呼叫 super().__init__(sched)?
    │   │   │       否 → 修正程式碼
    │   │   │       是 → 查看應用日誌尋找錯誤
    │   │   │
    │   │   └─ 不可導入 → 修正導入路徑或類別名稱
    │   │
    │   └─ 未註冊 → 執行「方案 A」快速修復
    │       │
    │       ├─ pip uninstall package -y
    │       ├─ pip install -e /path/to/package
    │       └─ python diagnose_tasks.py
    │           │
    │           ├─ 成功 → 重啟應用
    │           └─ 失敗 → 執行「方案 B」或「方案 C」
    │
    └─ 全部檢查後仍失效
        └─ 執行完整診斷腳本：python diagnose_tasks.py，依建議步驟修復
```

**快速修復命令**：
```bash
# 常見且快速的修復（約 80% 問題可解）
pip uninstall finfun-quantanlys -y
pip install -e /path/to/finfun-quantanlys
# 重新啟動應用
```

---

## 問題 2：任務參數在 Web UI 顯示不正確

```
參數顯示為空或錯誤
    │
    ├─ 檢查：是否於 dataclass 欄位的 metadata 中定義欄位型別?
    │   │
    │   ├─ 否 → 為欄位加入 metadata，例如：
    │   │   param: str = field(
    │   │       default='value',
    │   │       metadata={'type': StringField, ...}
    │   │   )
    │   │
    │   └─ 是 → 檢查：metadata 是否包含 'type'?
    │       │
    │       ├─ 否 → 新增 'type': StringField/IntegerField/...
    │       │
    │       └─ 是 → 檢查：type 是否為正確的 wtforms 類別?
    │           │
    │           ├─ 否 → 更正為正確欄位類型
    │           │
    │           └─ 是 → 檢查：是否有多餘空白或格式錯誤，清理並重啟應用
    │
    └─ 仍有問題
        └─ 比對 [QUICK_REFERENCE.md](QUICK_REFERENCE.md) 的範例，確認 metadata 結構一致
```

**檢查清單**：
```python
# ✓ 必要項
metadata={
    'type': StringField,        # 必須有 type
    'label': 'Display Name',     # 建議有 label
    'validators': [...]          # 如需驗證則加上
}

# ❌ 常見錯誤
metadata={'type': 'StringField'}     # type 應為類別，不是字串
metadata={'label': 'Name'}           # 缺少 type
```

---

## 問題 3：任務執行失敗

```
任務執行失敗 / 無法取得結果
    │
    ├─ 檢查：execute() 方法簽名是否正確？
    │   def execute(self, *args, **kwargs):
    │   │
    │   ├─ 否 → 修改為正確簽名
    │   │
    │   └─ 是 → 檢查：是否使用 try/except 包裹並記錄例外?
    │       │
    │       ├─ 否 → 新增 try/except 與日誌記錄
    │       │
    │       └─ 是 → 檢查：是否在日誌中輸出 exc_info=True?
    │           │
    │           ├─ 否 → 加上 exc_info=True
    │           │
    │           └─ 是 → 檢查應用日誌以取得錯誤訊息並修復
    │
    └─ 找不到日誌
        └─ 檢查日誌級別是否正確設定（如 INFO）與日誌檔案位置
```

**偵錯範例**：
```python
def execute(self, *args, **kwargs):
    mylogger = log.get_logger(__name__, level=logging.INFO)

    try:
        mylogger.info(f"開始執行, args={args}, kwargs={kwargs}")

        # 業務邏輯
        result = self._do_work()

        mylogger.info(f"執行成功: {result}")
        return result

    except Exception as e:
        # 重要：紀錄完整堆疊並重新拋出
        mylogger.error(f"執行失敗: {e}", exc_info=True)
        raise
```

---

## 問題 4：動態排程 (plan_schedule) 無效

```
plan_schedule() 回傳的排程未生效
    │
    ├─ 檢查：是否在 config.toml 中定義了 trigger？
    │   │
    │   ├─ 是 → plan_schedule() 將被忽略，請移除 config.toml 中的 trigger 設定
    │   │
    │   └─ 否 → 檢查：plan_schedule() 是否被呼叫？
    │       │
    │       ├─ 新增日誌驗證，例如：
    │       │ def plan_schedule(self):
    │       │     mylogger = log.get_logger(__name__)
    │       │     plan = {...}
    │       │     mylogger.info(f"計畫: {plan}")
    │       │     return plan
    │       │
    │       └─ 執行應用並查看日誌
    │           │
    │           ├─ 有日誌 → 檢查回傳的 plan 是否正確（須包含 trigger）
    │           │
    │           └─ 無日誌 → 檢查是否已覆寫 plan_schedule() 或於 __init__ 設定 trigger
    │
    └─ 若仍無效
        └─ 參考 [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md#最佳實務-4-實作-plan_schedule-以支援動態排程) 的範例
```

**驗證排程**：
```bash
# 啟動應用時應看到 Loading task MyTask ... Done

# 檢視任務觸發器資訊
curl http://127.0.0.1:5000/sched/tasks
# 或於 Web UI 查看 "Trigger" 欄位
```

---

## 問題 5：資料庫操作失敗

```
任務中的資料庫操作發生錯誤
    │
    ├─ 檢查：是否使用 session context？
    │   │
    │   ├─ 否 → 改用：
    │   │   with self.dbmgr.session_context() as sa_session:
    │   │       # 你的程式
    │   │
    │   └─ 是 → 檢查：是否有適當的例外處理？
    │       │
    │       ├─ 否 → 新增 try/except
    │       │
    │       └─ 是 → 檢查：例外類型並針對性處理（SQLAlchemy、連線、權限等）
    │
    └─ 從日誌中取得完整堆疊資訊以進一步分析
```

**資料庫偵錯範例**：
```python
def execute(self, *args, **kwargs):
    mylogger = log.get_logger(__name__, level=logging.INFO)

    try:
        with self.dbmgr.session_context() as sa_session:
            mylogger.info("連線資料庫...")

            # 測試查詢
            count = sa_session.query(MyEntity).count()
            mylogger.info(f"找到 {count} 筆記錄")

            # 實際操作
            for item in sa_session.query(MyEntity).all():
                self._process(item)

            mylogger.info("資料庫操作完成")

    except Exception as e:
        mylogger.error(f"資料庫錯誤: {e}", exc_info=True)
        raise
```

---

## 問題 6：虛擬環境混亂

```
多次安裝仍無法解決問題
    │
    ├─ 方案 1：快速清理（約 2 分鐘）
    │   │
    │   ├─ pip uninstall finfun-quantanlys -y
    │   ├─ pip uninstall finfun-fundmgr -y  (如有相依套件)
    │   ├─ pip cache purge
    │   ├─ pip install -e /path/to/finfun-quantanlys
    │   └─ python diagnose_tasks.py
    │
    ├─ 方案 2：完整重建（約 5 分鐘）
    │   │
    │   ├─ cd /path/to/finfun-quantanlys
    │   ├─ rm -rf dist/ *.egg-info
    │   ├─ poetry build
    │   ├─ pip install --force-reinstall dist/*.whl
    │   └─ python diagnose_tasks.py
    │
    └─ 方案 3：重建虛擬環境（約 10 分鐘）
        │
        ├─ deactivate
        ├─ cd /path/to/main/app
        ├─ rm -rf /path/to/venv
        ├─ poetry env use python3.11
        ├─ poetry install
        └─ python diagnose_tasks.py
```

---

## 快速參考表

### 根據症狀快速定位

| 症狀 | 最可能原因 | 建議方案 |
|------|-----------|--------|
| Web UI 中看不到任務 | Entry point 未註冊 | 方案 A |
| 參數顯示為空 | 缺少 metadata | 新增 metadata |
| 任務無法執行 | 初始化失敗 | 檢查 __init__ |
| 執行失敗卻無錯誤 | 日誌未設定 | 加入日誌並 exc_info=True |
| 動態排程不生效 | config.toml 覆蓋 | 移除 trigger 設定 |
| 資料庫錯誤 | 連線或 SQL 問題 | 測試連線與查詢 |

### 根據修復耗時選擇

| 耗時 | 方案 | 成功率 |
|------|------|-------|
| 2 分鐘 | pip install -e | 85% |
| 3 分鐘 | poetry update | 90% |
| 5 分鐘 | 清除快取並重裝 | 95% |
| 10 分鐘 | 重建虛擬環境 | 100% |

---

## 診斷工具

### 1. 自動診斷腳本

位置： [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md#完整診斷與修復腳本)

使用方式：
```bash
python diagnose_tasks.py
```

### 2. 手動檢查指令

```bash
# 檢查 entry_points
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
tasks = eps.select(group='funlab_sched_task')
for ep in tasks:
    print(f'{ep.name}: {ep.value}')
"

# 驗證可導入性
python -c "from finfun.quantanlys.task import CalcQuantTask; print('✓')"

# 觀察日誌
tail -f log/app.log | grep -E "(Loading task|CalcQuant|Error)"
```

### 3. Web UI 檢查

```
1. 造訪: http://127.0.0.1:5000/sched/tasks
2. 檢視任務列表
3. 點選任務名稱檢查參數
4. 點選 "Run Task" 執行
5. 檢視 "Last Status" 欄位的執行結果
```

---

## 何時請求協助

### 自助排查

✅ 建議步驟：

1. 執行 `python diagnose_tasks.py`
2. 閱讀 [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md)
3. 依決策樹流程逐步檢查
4. 參考 [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) 的程式範例

### 若仍無法解決

📝 請收集下列資訊：

```bash
# 1. 診斷輸出
python diagnose_tasks.py > diagnosis.log

# 2. 應用日誌（最後 100 行）
tail -n 100 log/app.log > app.log

# 3. 任務程式碼
cat path/to/your_task.py > task_code.py

# 4. 設定節選
cat config.toml | grep -A 10 "MyTask" > config_section.toml
```

提交問題時，請一併提供上述資訊，以及：
- 已嘗試的修復步驟
- 當前錯誤訊息
- 任務完整程式碼

---

## 參考文件

- 📖 [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) - 完整開發指引
- 🚀 [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) - 快速參考
- 🔧 [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md) - 深度診斷
- 📚 [README.md](./README.md) - 文件總覽

---

**版本**: 1.0
**最后更新**: 2026-02-10
**用途**: 快速问题排查
