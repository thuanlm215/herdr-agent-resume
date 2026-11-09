# herdr-agent-resume — Windows 11 版

在 Herdr 桌面 App 重開機（或 core 重啟）後，把原本在每個終端裡執行的 **Claude Code** 與 **Codex** session 接回來。
來源專案：<https://github.com/thuanlm215/herdr-agent-resume>（MIT，見 `LICENSE`、`NOTICE`）。

## 運作方式（摘要）

1. **Hook**：Claude Code / Codex 開始 session 時執行 `agent_resume.py hook <agent>`，把 session 記在該終端的 `agent-resume` 標籤（herdr-core 保管，重開機後仍在）：agent、session id、工作目錄、transcript、啟動旗標、agent 與 shell 的 PID、Boot ID、shell 的啟動時間。
2. **Watcher**：每個 agent 一個，agent 結束時若 shell 還在 = 你自己退出 → 清掉標籤（**正常退出的不會被恢復**）；若 shell 跟著消失（重開機、登出、core 重啟）→ 標籤保留。
3. **恢復**：標籤的 Boot ID / shell 與目前不同、且該終端是閒置的 PowerShell 或 cmd → 輸入 `claude --resume <id>` / `codex resume <id>`。不重送提示詞、不重跑操作。

Windows 上 core 回報的 foreground 永遠是 shell 本身，所以「Agent 是否已在執行」是從程序樹判斷（shell 底下有任何非 conhost 子程序就視為忙碌，不會重複啟動）。

## 安裝（Windows 11）

需求：Python 3.8+（`python` 在 PATH 上，非 Microsoft Store 捷徑）、Herdr App、Claude Code 和／或 Codex。

```powershell
git clone <此專案> herdr-agent-resume-windows
cd herdr-agent-resume-windows

python scripts\install.py --dry-run          # 先預覽：不會改任何檔案
python scripts\install.py                    # 安裝
```

安裝程式會：

- 先跑 `herdr-app check`，通過才複製 `mods\agent-resume`（含 `winproc.py`）到 `~\.config\herdr-app\mods\`；
- 在 `keys.json` 新增 **Ctrl+Shift+R** → `mod.open:agent-resume`（已被占用就中止，不覆蓋）；
- 在 `~\.claude\settings.json`、`~\.codex\hooks.json` 的 `SessionStart` **最後面追加**一個 hook（既有 hook 位置與內容不變；Codex 依位置記住信任）；
- 備份每一個要改的檔案到 `~\.config\herdr-app\backups\agent-resume\<時間戳>\`，並寫 `manifest.json`。

Codex 下次啟動會問「Hooks need review」，請選信任。已在執行中的 session 要等下次啟動／resume 才會被記錄；Claude Code 已在執行的 session 會由安裝程式 `adopt` 立即記錄。

hook 指令格式：`python "C:/…/agent_resume.py" hook claude`（正斜線，bash / cmd / PowerShell 皆可）。

### 更新、解除安裝

```powershell
python scripts\install.py --update           # 只更新本安裝程式放的檔案；你手改過會拒絕
python scripts\install.py --uninstall        # 只移除我們的 hook、按鍵、mod、背景 watcher
python scripts\install.py --uninstall --dry-run
```

### 備份與還原

```powershell
python scripts\install.py --list-backups
python scripts\install.py --restore --dry-run        # 預覽最新一份備份會還原什麼
python scripts\install.py --restore                  # 還原最新備份
python scripts\install.py --restore 20261109T031500.123456Z
```

還原會：把備份的檔案原樣寫回、刪除該次安裝新建的檔案、還原 mod 資料夾與背景 watcher 登錄值。

## 使用

- 重開機後打開 Herdr，按 **Ctrl+Shift+R**。每個「遺失 agent、且停在閒置 PowerShell／cmd」的終端會被輸入恢復指令。Toast 會說明恢復了哪些、哪些沒有（以及原因）。
- 預覽（不輸入任何東西）：`python ~\.config\herdr-app\mods\agent-resume\agent_resume.py status`
- 健康檢查：`… agent_resume.py doctor`（Python、hc 是否可連、設定、失敗中的 session）
- 日誌：`… agent_resume.py log 30`（`~\.config\herdr-app\data\agent-resume\log.jsonl`）
- 嘗試紀錄：`…\data\agent-resume\state.json`（每個 session 最近 10 次：manual/auto、typed/failed、原因）

### 失敗與手動重試

agent 輸入後 5 秒內又回到 shell（例如 session 已不存在）→ 記為 `failed`，原因與 agent 最後一行輸出會出現在 toast、日誌和 `state.json`。標籤**不會被刪**，再按 Ctrl+Shift+R 即可重試（手動不受「只嘗試一次」限制）。

### 自動恢復（預設關閉）

```powershell
python scripts\install.py --update --auto                 # 登錄背景 watcher（HKCU\...\Run\HerdrAgentResume）；仍然是關閉的
python …\agent_resume.py auto on                          # 啟用
python …\agent_resume.py auto off                         # 停用
python …\agent_resume.py auto status
python scripts\install.py --update --no-auto              # 移除登錄
```

- 不修改任何 PowerShell Profile，也不碰其他 shell 啟動檔。
- watcher 只在 `auto` 開啟、core 可連線時每 10 秒看一次；只處理「標籤顯示遺失 agent」的終端，你新開的終端不受影響。
- **每個被記錄的 session 只自動嘗試一次**（以標籤的 `at` 為準）；失敗後不會重試，要重試請按 Ctrl+Shift+R。成功恢復後 hook 會重新記錄，下次重開機又可自動恢復一次。

## 安全

- **不繼承權限略過參數**：`--dangerously-skip-permissions`、`--allow-dangerously-skip-permissions`、`--permission-mode bypassPermissions`、Codex 的 `--dangerously-bypass-approvals-and-sandbox`、`-s danger-full-access`、`-a never`、`-c sandbox_mode/approval_policy=…`、任何 `--dangerously*`／`--yolo`／`--always-approve`，在儲存和輸入時都會被丟掉；Toast 會寫「flags left out」。
- 不帶 prompt、不帶 `--resume/--continue/-p/--session-id` 等 session 選擇旗標。
- **PowerShell**：所有來自標籤的值（路徑、session、旗標）都用單引號字面值輸入，`'`、`‘ ’ ‚ ‛` 一律加倍；`$()`、反引號、`;`、`|`、`&` 都只是字面字元。含控制字元／換行的值整筆拒絕。含雙引號的旗標值會被略過（PowerShell 原生參數傳遞會破壞它）。以真實 PowerShell 回讀驗證過。
- **cmd.exe**：路徑與旗標含 `% ^ & | < > ! "` 或控制字元者直接略過（cmd 沒有安全的引號）；UNC 路徑不支援 `cd /d`，略過。
- session id 必須符合 `^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$`。
- 只支援 PowerShell（`pwsh`、`powershell`）與 cmd；其他 shell（Git Bash、WSL）的終端會被略過並說明原因。

## 與 Linux/macOS 版的差異

| 項目 | Windows |
|---|---|
| 支援的 agent | Claude Code、Codex（其餘上游支援的 agent 在 Windows 不處理） |
| 程序／PID | ctypes：Toolhelp32 程序表、`GetProcessTimes`、`NtQueryInformationProcess`（命令列） |
| Boot ID | `NtQuerySystemInformation(SystemTimeOfDayInformation)` 的 BootTime；另外存 shell 的建立時間，因為 Fast Startup 會保留開機時間、PID 又會被重用 |
| 鎖 | `msvcrt.locking`，放在 `%LOCALAPPDATA%\herdr-agent-resume\` |
| 自動恢復 | 背景 watcher（Run 登錄值），不改 shell 啟動檔 |
| 編碼 | hook 以 UTF-8 讀 stdin，設定檔一律 UTF-8，並保留原本的 CRLF/LF |

## 已知限制

- Codex：hook 內容格式沿用上游（Linux 上驗證過）。在 Windows 上，真實 Codex 的 hook 尚未實測；單元測試涵蓋程序鏈（`pwsh → node → codex.exe`）、hook、plan、命令。
- Claude 在未信任的資料夾會停在信任提示；恢復只負責輸入命令。
- 恢復用的 shell 由 core 決定（目前觀察是 `pwsh.exe`）。若 core 的 `shell` 設為不支援的 shell，會略過並說明。
- 輸入的恢復指令會留在 PowerShell 的歷史記錄裡（PSReadLine）。

## 測試

```powershell
npm run test:windows       # JS toast 測試 + Python 單元測試（108 項，2 項 POSIX 專用略過）
herdr-app check mods\agent-resume
```

詳見 `docs/windows-test-report.md`。
