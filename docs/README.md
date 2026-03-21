# Funlab-Sched 文件總覽

歡迎使用 Funlab-Sched 的開發文件集合。本目錄收錄任務排程系統的教學、範例與故障排除指南，並以台灣常用詞彙撰寫。

## 📚 文件結構

### 🚀 快速上手

- **[QUICK_REFERENCE.md](./QUICK_REFERENCE.md)** - 快速參考指南
  - 5 分鐘建立任務流程
  - 常用指令與程式片段
  - 欄位類型與驗證器說明
  - 常見錯誤快速查找

### 📖 完整指南

- **[DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md)** - 完整開發指引
  - 架構概覽
  - 詳細任務建立流程
  - Web UI 整合說明
  - 最佳實務與範例程式碼

### 🔧 故障排除

- **[ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md)** - Entry Points 問題診斷
  - 深度問題分析與檢查步驟
  - 多種修復方案比較
  - 自動診斷腳本與預防作法

---

## 📋 快速導覽

### 想做的事...

| 需求 | 推薦閱讀 |
|------|---------|
| 立即建立新任務 | [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) → "快速建立任務清單" |
| 了解系統運作 | [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) → "架構概覽" |
| 任務未顯示於 UI | [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md) |
| 查看設定範例 | [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) → "設定參考" |
| 偵錯任務執行問題 | [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) → "常見問題與解法" |
| 學習最佳實務 | [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) → "最佳實務" |
| Cron 表達式範例 | [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) → "Cron 範例" |
| 效能優化 | [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) → "效能優化" |

---

## 🎯 按場景查找

### 新手開發者

**推薦學習路徑**：

1. 閱讀 [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) 的「介紹」與「架構概覽」
2. 依照 [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) 的「建立新任務」逐步完成
3. 參考 [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) 的快速建立清單
4. 在 Web UI 上測試任務
5. 若遇問題，查閱 [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md)

**預估耗時**：30 分鐘

### 有經驗的開發者

**推薦查看**：

- [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) - 快速查找語法與設定
- [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md) - 問題診斷腳本

**預估耗時**：5 分鐘

### 維運或架構師

**推薦閱讀**：

- [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) - 完整系統說明
- [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md) - 深度診斷與預防作法
- 可選：檢視原始碼 `../funlab/sched/`

**預估耗時**：1 小時

---

## 🔍 常見問題索引

### 任務未顯示於 Web UI

**位置**： [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md#問題-任務在-web-ui-中不顯示)

**快速解法**：
```bash
pip uninstall finfun-quantanlys -y
pip install -e /path/to/finfun-quantanlys
# 重新啟動應用
```

### 參數不顯示或顯示錯誤

**位置**： [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md#問題-3-任務參數在-web-ui-中不顯示)

**快速解法**：確保 dataclass 欄位在 metadata 中已正確定義表單類型

### 任務執行失敗

**位置**： [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md#問題-4-任務執行失敗但未收到通知)

**快速解法**：使用 logger 並加上 `exc_info=True` 以紀錄完整例外堆疊

### Entry Point 診斷

**位置**： [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md#診斷步驟)

**快速檢查**：
```bash
python diagnose_tasks.py
```

---

## 💡 常用程式片段

### 最小化任務範本

```python
from dataclasses import dataclass, field
from wtforms import StringField, HiddenField
from wtforms.validators import DataRequired
from funlab.sched.task import SchedTask
from funlab.sched.service import SchedService
from funlab.utils import log
import logging

@dataclass
class MyTask(SchedTask):
    param: str = field(
        default='value',
        metadata={'type': StringField, 'validators': [DataRequired()]}
    )
    manually: bool = field(default=True, metadata={'type': HiddenField})

    def __init__(self, sched: SchedService):
        super().__init__(sched)

    def execute(self, param='value', manually=False, *args, **kwargs):
        mylogger = log.get_logger(__name__, level=logging.INFO)
        mylogger.info(f"執行任務: {param}")
        # 你的程式碼
```

### 在 pyproject.toml 中註冊

```toml
[tool.poetry.plugins."funlab_sched_task"]
MyTask = "your_package.module:MyTask"
```

### 在 config.toml 的設定範例

```toml
[MyTask]
    trigger = 'cron'
    hour = 9
    minute = 0
```

---

## 📊 版本歷史

| 版本 | 日期 | 更新 |
|------|------|------|
| 1.0 | 2026-02-10 | 初版，含完整開發指引與故障排除 |

---

## 🔗 相關資源

### 官方文件
- [APScheduler 官方文件](https://apscheduler.readthedocs.io/)
- [WTForms 文件](https://wtforms.readthedocs.io/)
- [Python Entry Points](https://packaging.python.org/specifications/entry-points/)

### 專案相關檔案
- [README.md](../README.md) - 專案說明
- [原始碼](../funlab/sched/) - 實作細節
- [pyproject.toml](../pyproject.toml) - 專案設定

---

## 👥 參與與貢獻

若發現文件內容有誤或希望改進，歡迎提出 issue 或 PR。

### 文件改進檢查清單

- [ ] 程式範例已測試
- [ ] 連結有效且指向正確位置
- [ ] 無拼字或語法錯誤
- [ ] 範例與當前版本一致
- [ ] 含有足夠說明與上下文

---

## 📞 取得協助

### 任務無法執行

**步驟 1**：閱讀相關章節
1. 檢查 [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) - "常見問題與解法"
2. 執行 `python diagnose_tasks.py`
3. 查閱 [ENTRY_POINTS_TROUBLESHOOTING.md](./ENTRY_POINTS_TROUBLESHOOTING.md)

**步驟 2**：收集偵錯資訊
- 應用啟動日誌
- entry_points 診斷輸出
- 任務執行日誌

**步驟 3**：提交問題（若仍未解決）
- 附上診斷輸出
- 附上完整錯誤訊息
- 說明已嘗試的修復步驟

---

## 📝 授權

所有文件遵照專案既有授權條款。

---

## ✅ 文檔完成狀態

**翻譯進度：** 100% ✅
- [x] DEVELOPMENT_GUIDE.md - 繁體中文（台灣用語）
- [x] README.md - 繁體中文（台灣用語）
- [x] QUICK_REFERENCE.md - 繁體中文（台灣用語）
- [x] ENTRY_POINTS_TROUBLESHOOTING.md - 繁體中文（台灣用語）
- [x] TROUBLESHOOTING_FLOWCHART.md - 繁體中文（台灣用語）
- [x] COMPLETION_SUMMARY.md - 繁體中文（台灣用語）

**校對狀態：** 100% ✅
- [x] 用語一致性檢查（台灣/臺灣 統一為「台灣」）
- [x] 格式檢查
- [x] 連結驗證

---

**最後更新**: 2026-02-11
**維護者**: Development Team
**狀態**: ✅ 文檔完整（台灣中文版）
