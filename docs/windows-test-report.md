# Windows 測試報告（2026-11-09）

環境：Windows 11 Home 10.0.26300、Python 3.13、Node 26、Herdr app / herdr-core 0.1.19、Claude Code 2.1.319、Codex 0.167.0。

## 1. 自動化測試（隔離，不碰真實 Herdr / ~/.claude / ~/.codex / HKCU）

`npm run test:windows`：JS 5 項 + Python 108 項（2 項為上游 POSIX 專用，Windows 略過）= **全部通過**。`herdr-app check mods/agent-resume` 通過。

| 範圍 | 檔案 | 重點 |
|---|---|---|
| 程序／PID／Boot | `tests/test_windows.py` | 真實程序的 ppid/名稱/啟動時間/存活/等待結束/命令列（含中文、空白）；node 包裝鏈；nested／outside 判斷；Boot ID 穩定；PID 重用（shell_start） |
| Shell 命令安全 | 同上 | PowerShell 單引號字面值、智慧引號、`$()`、反引號、`; | &`、控制字元；**用真實 PowerShell 回讀 14 種惡意字串皆相同**；cmd 不安全字元拒絕；session id 注入 |
| 危險旗標 | 同上 | claude／codex 各種寫法（含 `--flag=value`、`-c sandbox_mode=…`）不被繼承 |
| plan／resume | 同上 | 閒置 shell 才輸入；Agent 已在跑不重複；未知 shell 略過；同 session 只恢復一處；失敗記錄與手動重試；dry-run 不輸入；busy 鎖 |
| 自動恢復 | 同上 | 預設關；每個標籤 `at` 只嘗試一次；手動不受限；config 損毀＝關 |
| Watcher | 同上 | 正常退出清標籤；關機保留；被取代的終端／較新的 session 不動 |
| Hook | 同上 | 中文路徑、旗標（丟危險旗標）、shell_start、子代理／nested／outside／非 SessionStart 忽略 |
| 安裝程式 | `tests/test_install_windows.py` | dry-run 零變更；hook 追加在最後且保留既有；不覆蓋按鍵、衝突時零變更；UTF-8＋CRLF 保留；備份＋manifest；還原／列出；uninstall 只移除自己的；Run 登錄值；從不碰 PowerShell Profile；寫入失敗整體回滾；真實 `herdr-app check` |

## 2. 隔離環境下的真實測試（`HC_SESSION=rtest`，獨立 core，不碰預設 core）

| 步驟 | 結果 |
|---|---|
| 真實 Claude Code 的 SessionStart hook（`--settings` 暫存檔載入，未改 `~/.claude`） | ✅ 標籤含 session、`測試 專案's` 路徑（中文＋空白＋單引號）、pid、shell、boot、shell_start、`--model sonnet` |
| 正常 `/exit` | ✅ watcher 清除標籤 → 不會被恢復 |
| 模擬重開機（結束隔離 holder 程序樹，core 以 keep 換成新 shell，同位址） | ✅ 標籤保留；Boot 相同但 shell 不同 → 判定遺失 |
| `status`（乾跑） | ✅ 列出將輸入的命令，不輸入任何東西 |
| 手動 `resume` | ✅ Claude 回到原對話，旗標 `--model sonnet` 保留，**問「之前的暗號」答出 `PAPAYA-4410`**，未重送提示詞 |
| 再跑一次 `resume` | ✅ 無事可做（不重複啟動），hook 重新記錄新 pid/shell |
| 自動恢復預設關：開 watcher 30 秒 | ✅ 完全沒有輸入 |
| `auto on` | ✅ 約 10 秒內兩個遺失的終端都被恢復；無標籤的普通終端未被碰 |
| 自動模式重試 | ✅ 已嘗試過的 session 不再自動輸入；`status` 仍可手動重試 |
| 預設 core 全程 | ✅ pid 15500、t12/t13/t14 不變 |

## 3. 尚未驗證／限制

- **真實 Codex 的恢復**：未驗證。Codex 的 hook 只能讀 `CODEX_HOME\hooks.json`；要實測就得改你真實的 `~/.codex/hooks.json`（安裝程式會備份、可還原）或複製 `auth.json` 到暫存 CODEX_HOME。兩者我都沒擅自做。測試時意外啟動了一個真實 Codex（cmd 沒搜尋目前資料夾），它停在輸入提示、沒有送任何 prompt，已 `/exit`；它結束時印出的 `codex resume <id>` 與我們輸入的格式一致。
- cmd.exe 路徑只有單元測試（core 重生出來的 shell 是 `pwsh.exe`）。
- Claude 在未信任資料夾停在信任提示（已觀察到），恢復只負責輸入命令。
- 沒有 Herdr 視窗的隔離環境：Ctrl+Shift+R 的 popup／toast 只做了 `herdr-app check`，沒在真實視窗上看過（隔離 core 沒有 GUI）。

## 4. 測試遺留物（全部在隔離範圍內）

- 隔離 core：`HC_SESSION=rtest`（pid 24256）與 `HC_SESSION=unittest-no-such-core`（早期一次誤觸自動啟動的空 core）仍在運作、沒有終端。停止指令：`$env:HC_SESSION='rtest'; hc server stop`（另一個同理）。我的 `hc server stop` 被權限分類器擋下，所以沒有自行停止。
- `~\.claude.json` 新增了 1 個測試資料夾的信任紀錄（`herdr-agent-resume-windows-testbed/測試 專案's`，`hasTrustDialogAccepted=true`）。另一個資料夾的信任提示我選了「No, exit」，沒有紀錄。
- `~\.claude\projects\` 下有 2 個測試 session 的 transcript（目錄名含 `testbed`：`51ff88e3…`、`b37096ed…`）。第一次啟動的 session 因繼承 `CLAUDE_CODE_CHILD_SESSION` 而沒有 transcript。
- 測試資料夾：`~\Documents\herdr-agent-resume-windows-testbed\`。
