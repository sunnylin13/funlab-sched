# Funlab-Sched 疑難排解

依症狀排解。指令預設已 `source ~/.venv/fund13/bin/activate`。

## 診斷指令速查

```bash
# 1) entry point 是否已註冊進 venv（含逐一嘗試載入）
python - <<'PY'
from importlib.metadata import entry_points
from funlab.sched.task import SchedTask
eps = list(entry_points(group='funlab_sched_task'))
print(f'找到 {len(eps)} 個 entry points:')
for ep in eps:
    try:
        cls = ep.load()
        ok = '✓' if issubclass(cls, SchedTask) else '✗ 未繼承 SchedTask'
    except Exception as e:
        ok = f'✗ 載入失敗: {e}'
    print(f'  {ok} {ep.name}: {ep.value}')
PY

# 2) 套件安裝來源與 metadata 位置
pip show your_package | grep -E 'Location|Editable'
python -c "import your_package, pathlib; print(your_package.__file__)"

# 3) 啟動日誌（正式服務）
tail -n 200 ~/workspaces/fund13/backups/web.log | grep -E "Loading task|SchedService|failed to load|disabled"

# 4) 開發環境手動起服務觀察
cd ~/workspaces/fund13/finfun && python run.py 2>&1 | grep -E "Loading task|SchedService|Error"
```

## 問題 1：任務在 Web UI 不顯示

依序排除：

1. **entry point 未註冊/未更新**（最常見，尤其 editable 安裝後改過 pyproject）
   → 跑上方指令 1。看不到 → 重裝：

   ```bash
   cd /home/sunnylin/workspaces/fund13/your_package && pip install -e .
   # 或 workspace 根：uv sync
   ```

   仍看不到 → 檢查 venv 內 metadata：

   ```bash
   cat ~/.venv/fund13/lib/python3.12/site-packages/your_package-*.dist-info/entry_points.txt
   ```

   內容過舊 → `pip uninstall your_package -y && pip install -e .`。
2. **常見 entry point 寫法錯誤**：

   ```toml
   # ❌ group 拼錯（多一個 s）
   [project.entry-points."funlab_sched_tasks"]
   # ❌ 用點號不用冒號
   YourTask = "your_package.module.YourTaskClass"
   # ❌ Poetry 舊語法混用（本 workspace 已全面 PEP 621）
   [tool.poetry.plugins."funlab_sched_task"]
   # ✅ 正確
   [project.entry-points."funlab_sched_task"]
   YourTask = "your_package.module:YourTaskClass"
   ```

3. **類別載入失敗**（指令 1 出現 ✗）→ import 鏈壞或相依缺；啟動日誌会有
   `Skipping task '...': failed to load class (...)`——任務載入失敗只跳過該任務，不會讓整個應用起不來，**一定要看 log 才會發現**。
4. **實例化/初始化階段失敗** → 日誌 `Task '...' disabled: failed during initialisation: ...`。
   常見：`__init__` 沒呼叫 `super().__init__(sched)`、傳了 Flask app 而非 SchedService、
   `__init__` 裡抓了尚未就緒的重資源（改用 `prepare_runtime()`）。
5. **config 把它關了**：`[TaskName] disable = true` 存在 → 日誌 `Skipped task ...: disabled in config`。
6. 以上都正常仍未見 → **重啟應用**（任務只在啟動/hook 觸發時掃描 entry point）。

### 情境補充

- **Windows**：檔案鎖會造成重裝不乾淨；先關掉跑著的 python 程序再重裝。
- **CI**：pipeline 內 `pip install -e` 後立即用指令 1 驗證 entry point 數量大於 0，再跑測試。

## 問題 2：任務參數在 UI 顯示不正確／表單空白

- 每個要顯示的 dataclass 欄位都要有 `metadata['type']`（**字串、wtforms 實名**）：

  ```python
  # ❌ 沒有 type：靠型別推導（PEP 604 `X | None` 會錯誤降為 StringField——funlab-libs 已知缺陷）
  count: int = field(default=10)
  # ❌ 自訂拼錯：'IntField' 靜默降成 StringField（STRING_TYPE_MAPPING 只有 'IntegerField'）
  count: int = field(default=10, metadata={'type': 'IntField'})
  # ✅
  count: int = field(default=10, metadata={'type': 'IntegerField', 'label': '數量'})
  ```

- `HiddenField` 欄位不會出現在對話框（設計如此，內部旗標用）。
- SelectField 沒給 `choices` → 渲染出空選單。
- 改完 metadata 記得**重啟應用**：表單類別在任務實例化時建立一次。

## 問題 3：任務執行失敗但看不到原因

- 先確認「真的執行了」：日誌找 `Task <name> execution started`（`_execute_with_hooks` 必印）。
  有 started 沒 completed → 看同 thread 的 traceback；`_execute_with_hooks` 會以
  `exc_info=True` 記錄後**重新拋出**，APScheduler 記為 `Failed`，UI Last Status 也會顯示 exception。
- 完全沒有執行紀錄 → 排程沒觸發（問題 4）或排程器沒起（`GET /health` 看
  `plugins.sched` 的 `scheduler_running`/`tasks_loaded`）。
- 手動執行沒反應：
  - 送出後 UI「Manual Queue」應出現佇列時間，「Manual Result」出現 Queued→Executed/Failed。
  - 卡在 Queued 很久 → 排程器忙碌或 misfire（手動 job grace 300s）。
  - 「排程器尚未啟動（state=...）」通知 → loader 還沒跑完或死掉（日誌找
    `Fatal error during task loading`）。
  - 「任務已在執行中，請稍後再試」→ 同任務 `_M` job 尚未結束，這是去重設計。

## 問題 4：動態排程 plan_schedule() 不生效／被 config 干擾

真實語意（以原始碼為準）：`_load_single_task` **無條件**呼叫 `plan_schedule()`，
回傳 truthy dict 就**覆寫** config 同名鍵（trigger/hour/minute/...）。

```
plan_schedule() 排程沒照預期生效
 ├─ config [TaskName] 也有 trigger？→ 兩者都會被 plan 覆寫；若 plan 回傳 None 才用 config
 ├─ plan 回傳了但 job 時間不對？→ 檢查 return dict 鍵名（trigger='cron' + hour/minute/day_of_week…）
 ├─ 第一次啟動就生效、隔天時間沒變？→ plan_schedule 只在載入時呼叫一次；
 │   要執行期滾動調整請在 prepare_runtime() 用 self.job.reschedule(**plan)（實例旗標防重複）
 └─ 驗證：啟動日誌「Loaded task/Task loaded」後，UI Trigger/Next Run 欄位直接看 job 實際 trigger
```

## 問題 5：同一任務同時跑好幾個／上一次沒跑完又開始

- `job_defaults.max_instances`（`[SchedService]`，現值 3）允許同任務併發。業務任務應為 1，
  併發需求放個別 `[TaskName] max_instances`。詳見 IMPROVEMENT_PLAN SCH-09。
- 手動重複提交已被 `_M` job 去重擋住；auto×auto 由 max_instances 管。
- 日誌訊號：`maximum number of running instances reached` = 被擋（這是 max_instances=1 時 Expected 的跳過）。

## 問題 6：重啟後任務「遲到沒補跑」或「補跑重複」

- `[SchedService] job_defaults` 現況未設 `misfire_grace_time` → APScheduler 預設 **1 秒**：
  遲到超過 1s 就整次丟棄（UI 可能出現 `Missed at:...`）。要「5 分鐘內補跑」要明示
  `misfire_grace_time = 300`；搭配 `coalesce = true` 才是「積壓只補一次」。
  詳見 IMPROVEMENT_PLAN SCH-08/09。
- 執行時間偏移一小時/八小時 → `timezone` 未設成 `Asia/Taipei`（本專案所有環境都該顯式設定）。

## 問題 7：環境混亂（裝了幾次都一樣）

按成本遞增：

```bash
# A. 重裝目標套件（最常用）
pip uninstall your_package -y && pip install -e /home/sunnylin/workspaces/fund13/your_package

# B. workspace 統一同步
cd /home/sunnylin/workspaces/fund13 && uv sync

# C. 清建置殘留再裝
cd /home/sunnylin/workspaces/fund13/your_package
rm -rf dist/ build/ *.egg-info
pip install -e .

# D. 最後手段：重建 venv（用 workspace 根 uv sync 重建，勿手動 rm 半個 venv）
```

每步之後都跑一次「診斷指令 1」確認 entry point 清單，再重啟應用看日誌。

## 何時請求協助

提交：診斷指令 1 的完整輸出、啟動日誌相關段（`grep -E "Loading task|SchedService" web.log`）、
任務類別原始碼、`config.toml` 的 `[SchedService]` 與 `[TaskName]` 段落、已嘗試的步驟。

## 相關

- 開發流程與慣例：[DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md)
- 已知缺陷與修復計畫：[IMPROVEMENT_PLAN.md](./IMPROVEMENT_PLAN.md)
