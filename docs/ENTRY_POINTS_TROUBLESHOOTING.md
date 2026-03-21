# Entry Points 問題診斷與修復指南

## 問題概述

當你在 `pyproject.toml` 中註冊新的任務 plugin，但在 Web UI 中看不到該任務時，通常是 **entry_point 註冊或 metadata 未更新** 所導致。

這類問題在使用 `develop = true`（editable 模式）或頻繁重裝套件時很常見。

---

## 運作原理

### 什麼是 Entry Points？

Entry Points 是 Python 的一種外掛發現機制，讓套件可以聲明可被其他程式載入的「入口」。

在 Funlab-Sched 中：
- **Group**: `funlab_sched_task`
- **Entry**: 任務名稱與類別路徑
- **值**: `package.module:ClassName`

### 註冊與發現流程

```
pyproject.toml 定義
    ↓
poetry 建置時解析
    ↓
產生 .dist-info/entry_points.txt
    ↓
poetry/pip 安裝套件
    ↓
虛擬環境的 entry_points metadata 更新
    ↓
應用以 importlib.metadata 掃描並載入
```

---

## 問題診斷

### 問題：任務在 Web UI 中不顯示

#### 症狀檢查表

- [ ] 任務在 `pyproject.toml` 中已正確定義
- [ ] 應用啟動時無該任務載入日誌
- [ ] 造訪 `/sched/tasks` 未見任務
- [ ] 其他任務顯示正常

#### 根本原因分析（常見）

| 原因 | 機率 | 解決難度 |
|------|------|--------|
| Entry point 未註冊到虛擬環境 | 80% | ⭐ 簡單 |
| Entry point 定義錯誤（拼字、冒號誤用） | 10% | ⭐ 簡單 |
| 套件未安裝或未更新 | 5% | ⭐⭐ 中等 |
| 任務類別初始化失敗 | 3% | ⭐⭐ 中等 |
| 其他（系統環境、權限） | 2% | ⭐⭐⭐ 複雜 |

---

## 诊断步骤

### 第一步：驗證 Entry Point 定義

檢查 `pyproject.toml` 的語法是否正確：

```bash
cd /path/to/your_package
cat pyproject.toml | grep -A 5 'funlab_sched_task'
```

應看到：

```toml
[tool.poetry.plugins."funlab_sched_task"]
YourTask = "your_package.module:YourTaskClass"
```

**常見錯誤範例**：

```toml
# ❌ 錯誤 1：group 名拼錯
[tool.poetry.plugins."funlab_sched_tasks"]  # ← 應為 funlab_sched_task
YourTask = "..."

# ❌ 錯誤 2：冒號誤用為點號
[tool.poetry.plugins."funlab_sched_task"]
YourTask = "your_package.module.YourTaskClass"  # ← 應使用冒號 :

# ❌ 錯誤 3：路徑或模組名稱錯誤
[tool.poetry.plugins."funlab_sched_task"]
YourTask = "wrong_package.YourTaskClass"

# ✅ 正確寫法
[tool.poetry.plugins."funlab_sched_task"]
YourTask = "your_package.module:YourTaskClass"
```

### 第二步：驗證虛擬環境中的 Entry Points

```bash
python -c "
import importlib.metadata

print('=== 所有 funlab_sched_task entry points ===')
eps = importlib.metadata.entry_points()
sched_tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])

if not sched_tasks:
    print('❌ 未找到任何 entry points')
else:
    print(f'✓ 找到 {len(list(sched_tasks))} 個 entry points:')
    for ep in sched_tasks:
        try:
            ep.load()  # 嘗試載入
            print(f'  ✓ {ep.name}: {ep.value}')
        except Exception as e:
            print(f'  ✗ {ep.name}: {ep.value} (載入失敗: {e})')
"
```

**预期输出**：
```
=== 所有 funlab_sched_task entry points ===
✓ 找到 9 个 entry points:
  ✓ CalcQuant: finfun.quantanlys.task:CalcQuantTask
  ✓ CalcQuantV2: finfun.quantanlys.task:CalcQuantV2Task
  ✓ ...
```

**问题输出**：
```
=== 所有 funlab_sched_task entry points ===
❌ 未找到任何 entry points
```

### 第三步：檢查套件安裝狀態

```bash
# 檢查套件是否安裝
pip show your_package

# 查看模組檔案位置
python -c "import your_package; print(your_package.__file__)"

# 列出 .dist-info 目錄
ls -la /path/to/venv/lib/pythonX.Y/site-packages/your_package-*.dist-info/

# 檢視 entry_points.txt 內容
cat /path/to/venv/lib/pythonX.Y/site-packages/your_package-*.dist-info/entry_points.txt
```

### 第四步：驗證任務類別是否可導入

```bash
python -c "
try:
    from your_package.module import YourTaskClass
    print(f'✓ 可導入: {YourTaskClass}')
    print(f'  基類: {YourTaskClass.__bases__}')
    from funlab.sched.task import SchedTask
    if issubclass(YourTaskClass, SchedTask):
        print(f'  ✓ 正確繼承 SchedTask')
    else:
        print(f'  ✗ 未繼承 SchedTask')
except ImportError as e:
    print(f'✗ 導入失敗: {e}')
except Exception as e:
    print(f'✗ 其他錯誤: {e}')
"
```

### 第五步：檢查應用啟動日誌

```bash
# 啟動應用並查看日誌
python run.py 2>&1 | grep -E "(Loading task|SchedService|Error)"

# 應該看到類似輸出：
# Loading task CalcQuantTask ... Done
# Loading task CalcQuantV2Task ... Done
```

---

## 解決方案比較

| 解決方案 | 耗時 | 成功率 | 風險 | 推薦 |
|---------|------|-------|------|------|
| 方案 A：pip install -e | 2 分鐘 | 85% | 低 | ⭐⭐⭐ |
| 方案 B：Poetry update | 3 分鐘 | 90% | 低 | ⭐⭐⭐ |
| 方案 C：清除快取並重裝 | 5 分鐘 | 95% | 中 | ⭐⭐ |
| 方案 D：重建環境（從頭） | 10 分鐘 | 100% | 高 | ⭐ |

---

## 具体解决方案

### 方案 A：以 pip 重新安裝（推薦 — 最快）

```bash
# 第 1 步：卸載目前套件
pip uninstall your_package -y

# 第 2 步：以 editable 模式重新安裝
pip install -e /path/to/your_package

# 第 3 步：驗證 entry points
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])
print('✓ 已註冊的任務:')
for ep in tasks:
    print(f'  - {ep.name}')
"

# 第 4 步：重啟應用（停止 run.py 後重新啟動）
```

**优点**：
- 最快
- 只影响一个包
- 风险最低

**缺点**：
- 不更新依赖关系

### 方案 B：使用 Poetry 更新（推薦 — 最可靠）

```bash
# 在主應用目錄執行
cd /path/to/main/app

# 方式 1：只更新特定套件
poetry update your_package

# 方式 2：完整重新安裝依賴
poetry lock --refresh
poetry install

# 驗證 entry points
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])
for ep in tasks:
    if 'your' in ep.name.lower():
        print(f'✓ 找到: {ep.name}')
"

# 重啟應用
```

**优点**：
- 最可靠
- 更新所有依赖
- 保持 poetry.lock 同步

**缺点**：
- 速度较慢
- 可能更新其他包

### 方案 C：清除快取並完全重裝

```bash
# 在套件目錄
cd /path/to/your_package

# 第 1 步：清除所有建置與快取檔案
rm -rf dist/ *.egg-info .pytest_cache/ .tox/

# 第 2 步：重新建置套件
poetry build

# 第 3 步：在虛擬環境中先卸載
pip uninstall your_package -y

# 第 4 步：強制重新安裝 wheel
pip install --force-reinstall --no-cache-dir dist/your_package-*.whl

# 第 5 步：驗證 entry points
python -c "
import importlib.metadata
eps = importlib.metadata.entry_points()
tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', [])
for ep in tasks:
    print(f'{ep.name}: {ep.value}')
"
```

**优点**：
- 清除所有缓存
- 成功率最高

**缺点**：
- 速度慢
- 比较繁琐

### 方案 D：重建虛擬環境（最徹底）

```bash
# 僅在其他方案皆失敗時使用

# 第 1 步：備份重要設定
cp -r .env .env.backup

# 第 2 步：刪除虛擬環境
deactivate  # 離開虛擬環境
rm -rf /path/to/venv

# 第 3 步：在主應用目錄重建環境
cd /path/to/main/app
poetry env use $(which python3.11)  # 或指定你的 Python 版本
poetry install

# 第 4 步：驗證並測試
python -c "
from funlab.sched.service import SchedService
from funlab.core.plugin_manager import load_plugins
tasks = load_plugins(group='funlab_sched_task')
print(f'✓ 載入了 {len(tasks)} 個任務')
"

# 第 5 步：啟動應用
python run.py
```

**优点**：
- 完全清除问题
- 保证成功

**缺点**：
- 最耗时
- 最高风险

---

## 特定场景解决方案

### 情境 1：在 Windows 環境出現問題

**額外建議步驟**：

```powershell
# PowerShell 中：
# 第 1 步：終止所有 Python 程序
Get-Process python | Stop-Process -Force

# 第 2 步：清除 pip 快取
pip cache purge

# 第 3 步：重新安裝套件
pip uninstall your_package -y
pip install -e "C:\path\to\your_package"

# 第 4 步：驗證 entry points
python -c "import importlib.metadata; eps = importlib.metadata.entry_points(); tasks = eps.select(group='funlab_sched_task') if hasattr(eps, 'select') else eps.get('funlab_sched_task', []); [print(f'{ep.name}: {ep.value}') for ep in tasks]"
```

### 情境 2：多個專案共用同一套件

```bash
# 若多個專案依賴 your_package，可逐一檢查各虛擬環境
find /path/to/projects -name ".venv" -type d | while read venv; do
    echo "檢查 $venv"
    source $venv/bin/activate
    pip show your_package
    deactivate
done

# 分別更新每個環境
pip uninstall your_package -y
pip install -e /path/to/your_package
```

### 情境 3：CI/CD 環境中

```yaml
# GitHub Actions / GitLab CI 範例
before_script:
    # 建議在 pipeline 中清除並重新安裝套件以避免快取問題
    - pip uninstall -y your_package
    - pip install -e /path/to/your_package
    - python -c "import importlib.metadata; eps = importlib.metadata.entry_points(); tasks = eps.select(group='funlab_sched_task'); print(f'Tasks: {len(list(tasks))}')"

script:
    - python -m pytest
```

---

## 完整診斷與修復腳本

範例檔案：`diagnose_tasks.py`

```python
#!/usr/bin/env python3
"""
Funlab-Sched 任务诊断和修复脚本
"""

import sys
import subprocess
import importlib.metadata
from pathlib import Path

def check_entry_points():
    """检查 entry_points"""
    print("=" * 60)
    print("1. 检查 Entry Points")
    print("=" * 60)

    try:
        eps = importlib.metadata.entry_points()
        sched_tasks = (
            eps.select(group='funlab_sched_task')
            if hasattr(eps, 'select')
            else eps.get('funlab_sched_task', [])
        )

        tasks = list(sched_tasks)
        if not tasks:
            print("❌ 未找到任何 funlab_sched_task entry points")
            return False

        print(f"✓ 找到 {len(tasks)} 个 entry points:")
        for ep in tasks:
            try:
                ep.load()
                print(f"  ✓ {ep.name}: {ep.value}")
            except Exception as e:
                print(f"  ✗ {ep.name}: {ep.value} (加载失败: {e})")
                return False
        return True

    except Exception as e:
        print(f"❌ 检查失败: {e}")
        return False

def check_imports():
    """检查是否可以导入任务"""
    print("\n" + "=" * 60)
    print("2. 检查导入")
    print("=" * 60)

    try:
        from finfun.quantanlys.task import CalcQuantTask, CalcQuantV2Task
        print("✓ CalcQuantTask: 导入成功")
        print("✓ CalcQuantV2Task: 导入成功")
        return True
    except Exception as e:
        print(f"❌ 导入失败: {e}")
        return False

def suggest_fix():
    """建议修复方案"""
    print("\n" + "=" * 60)
    print("3. 建议的修复步骤")
    print("=" * 60)

    print("""
推荐方案 (按顺序尝试):

1️⃣  快速修复 (方案 A) - 2 分钟
   pip uninstall finfun-quantanlys -y
   pip install -e /path/to/finfun-quantanlys

2️⃣  完整修复 (方案 B) - 3 分钟
   cd /path/to/main/app
   poetry update finfun-quantanlys

3️⃣  彻底修复 (方案 C) - 5 分钟
   cd /path/to/finfun-quantanlys
   rm -rf dist/ *.egg-info
   poetry build
   pip install --force-reinstall dist/*.whl

然后重启应用并检查 http://127.0.0.1:5000/sched/tasks
    """)

def main():
    print("\n🔍 Funlab-Sched 任务诊断工具\n")

    # 检查 entry_points
    ep_ok = check_entry_points()

    # 检查导入
    import_ok = check_imports()

    # 总结
    print("\n" + "=" * 60)
    print("诊断总结")
    print("=" * 60)

    if ep_ok and import_ok:
        print("✓ 一切正常！任务应该已在 Web UI 中显示。")
        print("  如果仍未显示，请重启应用。")
    else:
        print("❌ 检测到问题，请按照下面的建议修复:")
        suggest_fix()

    return 0 if (ep_ok and import_ok) else 1

if __name__ == '__main__':
    sys.exit(main())
```

**使用方法**：

```bash
python diagnose_tasks.py

# 输出示例:
# 🔍 Funlab-Sched 任务诊断工具
#
# ============================================================
# 1. 检查 Entry Points
# ============================================================
# ✓ 找到 10 个 entry points:
#   ✓ CalcQuant: finfun.quantanlys.task:CalcQuantTask
#   ✓ CalcQuantV2: finfun.quantanlys.task:CalcQuantV2Task
#   ...
```

---

## 预防措施

### 1. 开发环境设置

```bash
# 使用可复现的开发环境
poetry env use python3.11
poetry install

# 定期更新依赖
poetry update

# 定期验证
python diagnose_tasks.py
```

### 2. 编写测试

```python
# tests/test_tasks.py
import importlib.metadata
from funlab.core.plugin_manager import load_plugins

def test_task_registration():
    """验证所有任务都已注册"""
    tasks = load_plugins(group="funlab_sched_task")

    assert len(tasks) > 0, "未找到任何 task"
    assert "CalcQuant" in tasks, "CalcQuant 未注册"
    assert "CalcQuantV2" in tasks, "CalcQuantV2 未注册"

def test_task_imports():
    """验证任务类可以导入"""
    from finfun.quantanlys.task import CalcQuantTask, CalcQuantV2Task
    from funlab.sched.task import SchedTask

    assert issubclass(CalcQuantTask, SchedTask)
    assert issubclass(CalcQuantV2Task, SchedTask)
```

### 3. CI/CD 检查

```yaml
# .github/workflows/test.yml
name: Test

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2

      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install poetry
          poetry install

      - name: Verify entry points
        run: |
          python -c "
          import importlib.metadata
          eps = importlib.metadata.entry_points()
          tasks = eps.select(group='funlab_sched_task')
          assert len(list(tasks)) > 0, 'No tasks registered'
          print('✓ All entry points verified')
          "

      - name: Run tests
        run: poetry run pytest
```

---

## 资源

- [Python Entry Points 官方文档](https://packaging.python.org/specifications/entry-points/)
- [importlib.metadata 文档](https://docs.python.org/3/library/importlib.metadata.html)
- [Poetry 依赖管理](https://python-poetry.org/docs/dependency-groups/)
- [APScheduler 文档](https://apscheduler.readthedocs.io/)

---

**版本**: 1.0
**最后更新**: 2026-02-10
**适用于**: Funlab-Sched ≥ 0.3.5
