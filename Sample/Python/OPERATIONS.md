# TTB 操作手冊（單一作業入口）

> **作業規範以本文件為準。** [MARKET_MAKER.md](MARKET_MAKER.md) 解釋目前內建策略，[DYNAMIC_MARKET_MAKING_PLAN.md](DYNAMIC_MARKET_MAKING_PLAN.md) 和 [strategies/](strategies/) 是研究提案；部署細節見 [Azure README](../../azure/README.md)，異常恢復細節見 [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md)。提案不是可執行或可安全使用的承諾。

## 現況與禁止事項

- **已實作**：即時行情訂閱、shadow 模擬、單商品 TTB Gateway、風控、交易日曆、交易事件 Journal、Fail-Closed、操作員停止旗標、健康監控，以及離線 Walk-forward/OOS。
- **僅可配置**：交易時段、成交成本、各種風控限額、可選的關機平倉嘗試。變更設定不能代表已經完成實際 TTB 驗證。
- **尚未驗收**：正確虛擬競賽帳戶身分、真實 TTB `QUERYMARGIN` 欄位／單位、下單與取消競態，以及 GUI 重啟後完整恢復（#2/#4/#10）。
- **禁止**：未經 [§8](#8-競賽全自動模式opt-in) 的 `set_mode.ps1` 檢查就手改 config 啟用 `--mode ttb`、在有人手動交易的帳戶啟用自動恢復、帶倉換月、手動刪除異常對帳旗標。

## 0. VM 唯讀健康稽核（#10）

Windows VM 目前是否能持續監控，不應僅依 Task Scheduler 顯示 `Running` 推斷。執行不修改 VM 的稽核指令：

```powershell
cd C:\TTB\Sample\Python
powershell -ExecutionPolicy Bypass -File automation\audit_vm_readiness.ps1 -WorkDir (Get-Location).Path
```

回報 JSON 會列出 TTB 進程、API/行情監聽埠、Automation/Health Watchdog 工作排程、心跳年齡、**最後有效行情 UTC 時間距今秒數**、bot 狀態、人工待對帳旗標及部署必備檔案。當 `healthy` 但行情過期／時間缺失時會列出 `accepted_quote_missing_or_stale`，不能以健康檔心跳推斷交易就緒。休市空倉可繼續監控，但 `tradingReady=false`；若有持倉且價格過期則視為不可驗收。報告另有 `runtimeSafetyFeatures`，會檢查實際部署的 `sim_hft_bot.py`、`ttb/engine.py`、`ttb/market_data.py` 是否包含逾時強制停機、交易所行情延遲、行情順序完整性，以及最新有效行情 UTC 時間欄位。任一缺少就標記 `missing_runtime_feature_*`，避免舊版 VM 因為額外複製了新工具便被誤判。此判斷是部署版本差異偵測，**不是**程式來源簽章或無漏洞的證明。任何必要項缺失即 exit 2；`safeForSupervisedShadowSoak=true` **僅表示 Shadow 監控前置條件通過，不表示帳戶驗證通過或允許 Live**。`supervisedLiveTradingApproved` 一律為 false，直到 #2/#4 真實端到端驗收完成。

2026-10-14 Azure VM 只讀查核曾看到 `TTB Automation` 處於 Running，但 bot 健康狀態為 `stale_market_data`，且缺少 `TTB Health Watchdog` 排程、`health_watchdog.ps1`、最新 `ttb_readonly_capture.py` 和 release validator。**不能把該 VM 宣稱為已達成 24/7 完整運行。** 此稽核不會自行升級現有執行中的交易程式；請在人工安排維護與 dry-run 後再部署。

## 1. VM、GUI 與環境

實際 TTB GUI 應在互動式 Windows 工作階段登入。VM 實際 RDP NSG 僅限已核准的管理來源 IP，若 IP 改變，使用 [RDP allowlist 工具](../../azure/Set-TtbRdpAllowlist.ps1) 更新，不要改回公網 `*`。

```powershell
cd C:\TTB\Sample\Python
py -3.13 -m pip install -r requirements.txt
py -3.13 -m unittest discover -q
powershell -ExecutionPolicy Bypass -File automation\validate_release.ps1 -WorkDir (Get-Location).Path
```

正式部署前另外執行 `py -3.13 -m automation.check_binary_integrity`，核對 `TTB.exe` 與 `TTBHelp.py` 的來源快照 SHA256 及兩者版本字串一致。詳見 [Azure 的二進位供應鏈章節](../../azure/README.md#4-ttb-二進位版本來源控管15)。checksum 通過**不代表官方來源已驗證**。

確認 TTB GUI 已登入**正確的模擬競賽帳戶**、有效契約和當年度經人工核准的交易日曆。檢查服務 API 和 quote stream；程式無法憑當前已驗證 API 自動確認 GUI 競賽身分。

## 2. Shadow 模式：先蒐集行情／決策

```powershell
py -3.13 sim_hft_bot.py --symbol <CURRENT_VERIFIED_SYMBOL> --mode shadow
```

預設監控日盤 08:45–13:45、夜盤 15:00–隔日 05:00。**實際時間須經期交所當日休市表及契約到期日校正**；到期日正常日盤 13:30 截止，沒有該合約下午夜盤。系統不會自動換到下一個月份。Shadow 仍需 GUI 行情連線，但**不送出真實模擬帳戶委託**。

### 從程式解析的 CLI 預設值

以下區塊由 `automation/check_docs_consistency.py` 與 `sim_hft_bot.parse_args()` 做一致性檢查，CLI 改版後應同步修改。

<!-- CLI DEFAULTS BEGIN -->
| CLI 參數 | 預設 |
|---|---|
| `--mode` | `shadow` |
| `--strategy` | `market-maker` |
| `--regular-open` | `08:45` |
| `--regular-close` | `13:45` |
| `--after-hours-open` | `15:00` |
| `--after-hours-close` | `05:00` |
| `--quote-stale-seconds` | `5.0` |
| `--stale-fatal-seconds` | `120.0` |
| `--resume-fresh-ticks` | `3` |
| `--flatten-on-shutdown` | `False` |
| `--daily-loss-limit` | `10000.0` |
| `--max-drawdown` | `40000.0` |
| `--point-value` | `None` |
<!-- CLI DEFAULTS END -->

`--point-value=None` 的意思不是 0：預設依商品自動解析 TXF=200、MTX=50、TMF=10 元／點；其他商品必須明確提供經核對的乘數。手續費、稅率與滑價必須另行依真實競賽商品設定，CLI 預設不可視為官方規格。

## 3. 停機與資料中斷

- 交易時段行情距上次有效報價超過預設 **5 秒**，進入 `stale_market_data`：停止新的入口並處理已知掛單。第一次報價另有啟動寬限時間，不能把 5 秒理解為任何狀態下都立即退出。
- 自收到過行情起，持續超過預設 **120 秒** 的中斷會 Fail-Closed 結束（可用 `--stale-fatal-seconds` 調整）。收到連續預設 **3 筆**新鮮行情後才可能恢復正常報價。
- 程式在同一風控交易日保存 `risk-state.json`，並將成交等事件寫入 Journal。**不能將持久化風控等同於自動辨識或回復帳戶未知持倉。** 回覆矛盾、磁碟故障、Journal 損毀都應停止並人工對帳。
- Ctrl+C／停止旗標會啟動既有關機對帳；**預設不會保證主動平倉**。`--flatten-on-shutdown`（Supervisor `flattenOnShutdown`）只有在允許條件下**嘗試**平倉，可能失敗或部分成交，並不是可靠的斷線平倉保證。
- `/healthz` 表示程序存活，`/readyz` **只有 `healthy` 才 ready**，`/status` 提供完整 health。休市 `waiting_market` 不表示故障，但也不是交易就緒。特別是 `health.updated_at` 可在無有效行情時持續更新；驗證行情新鮮度必須使用 `last_quote_wall_utc`／`last_quote_exchange_time` 及 QuoteMonitor 的完整性計數器，不可只看心跳時間。

## 4. 異常與人工恢復

若 `output/supervisor/manual-reconciliation-required.flag` 存在或 bot 非正常退出，**保持鎖定**。先在 TTB GUI 檢查完整帳戶、所有商品持倉及工作中委託，然後執行唯讀：

```powershell
py -3.13 reconcile_ttb.py
py -3.13 ttb_readonly_capture.py
```

只有完整核對且符合 [異常對帳手冊](OPERATOR_RUNBOOK.md) 四項明確確認條件時，才能執行受控的本機旗標清除；工具不會自動下單、撤單或平倉。

## 5. TTB 虛擬下單：目前仍有人工驗收阻塞

只有在完成 #2/#4 真實 API fixture、受控的一口委託/撤銷測試、帳戶全空、保證金欄位及單位和費稅校準、風控門檻確認後，才可由人工操作員評估開啟。除了 `--confirm-virtual-orders`、`--confirm-flat-account`，仍需要正確 `--max-notional`、`--min-available-margin` 和**實測** `--available-margin-field`。缺任何一項將拒絕新增曝險。切換方式只有 [§8](#8-競賽全自動模式opt-in) 的 `automation\set_mode.ps1 -Mode ttb`，它會先檢查前提並在首次啟動時自動完成一口委託／撤單測試。

一口 smoke test 會真的送出虛擬委託，僅能依 [README 的嚴格人工流程](README.md#真實-ttb-api-驗證準備2) 執行。即使遠價也可能成交，絕不從相同價格／口數推定委託歸屬。

## 6. 離線研究與驗收

`walk_forward_oos.py` 會分交易日隔離 Holdout、跑三種成交模型與 Monte Carlo，輸出 JSON/Markdown，且合成或無來源驗證資料不能通過研究閘門。研究成功也**不等於**實際 TTB 虛擬下單已通過 #2/#4。完整範例見 [README](README.md#walk-forwardout-of-sample-研究驗收8)。

## 7. 操作前檢查

- [ ] Windows、TTB GUI 與登入帳戶由人確認，行情沒有回放或時間異常
- [ ] 期交所休市表、合約月份／到期日與契約乘數核對
- [ ] 最近 GitHub CI 與本機離線驗證通過
- [ ] 本機風控狀態、Journal、rate counters 沒有損毀或遺失
- [ ] 所有未知委託、工作中委託與非零持倉均已由 GUI 人工解決
- [ ] `mode=shadow` 作為預設；live 的人工驗收阻塞未解除不得切換

## 8. 競賽全自動模式（opt-in）

目標是校內 TTB 模擬競賽開盤時間無人值守：VM 重開機 → Windows 自動登入 → `TTB Automation` 啟動 TTB → 自動登入競賽 → 開盤交易 → 異常時有界自動恢復。各元件：

| 元件 | 作用 |
|---|---|
| `symbol: "auto:TMF"`（也支援 `auto:TXF`、`auto:MTX`） | 依核准日曆選近月；到期日當天（13:30 收盤、無夜盤）起改用次月。bot 持倉或有掛單時**不換月**，空倉後以 exit 0 結束，由 supervisor 以新月份重啟。查詢：`py -3.13 -m automation.resolve_symbol auto:TMF` |
| `automation/ttb_login.py`（`autoLogin: true`） | 以 UI Automation 填入 Windows 認證管理員 `TaifexTradeBox/TTB` 的帳密、按「登入」、選 `ttbCompetition` 競賽，最後以唯讀 API 確認 `QUERYMARGIN`／委託／部位都回 `0000`。登入前強制**關閉** TTB「下單失敗每 5 秒自動重送」（`chkAutoPut`，TTB 預設為開），否則 TTB 會自行重送 bot 不認得的委託；並保持「斷線自動重連」開啟。live（`mode: "ttb"`）時 supervisor 加 `--require-auto-resubmit-off`：即使 TTB 已經登入，也會讀取 `chkAutoPut`，必要時關閉並套用；讀不到或關不掉就登入失敗，不啟動 bot。密碼不寫入檔案或 log。 |
| `auto_recover_ttb.py`（`autoRecovery`） | bot 非正常退出或啟動時帳戶不乾淨：撤銷**全部**工作中委託、以市價 IOC 平掉 TXF/MTX/TMF 期貨部位、兩次唯讀檢查皆乾淨才清除 `manual-reconciliation-required.flag`，每步驟寫入 `output/reconciliation` 稽核。每日最多 `maxAttemptsPerDay` 次（`output/supervisor/auto-recovery.json` 持久化），超過、遇到選擇權／超量部位、或平倉後仍不平，就維持鎖定並由 Watchdog 告警，回到 [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) 人工流程。休市帶倉會延到下一個交易時段再處理。 |
| `run_ttb_automation.ps1` | 每輪：確保 TTB 執行 → 等 8080/51141 → 自動登入（失敗每 60 秒重試）→ 需要時自動恢復 → 啟動 bot。 |

**部署全步驟（依序執行；1–6 一次性，7 以後每次上線）**

| # | 在哪裡 | 動作 |
|---|---|---|
| 1 | 本機 | `az login` 後 `powershell -ExecutionPolicy Bypass -File azure\Deploy-TtbRuntime.ps1 -Apply`：把已提交的程式碼部署到 VM（備份、測試、失敗自動還原、重新註冊排程）。RDP 連不上時先跑 `azure\Set-TtbRdpAllowlist.ps1 -Apply`。**GCP**：`gcloud auth login` 後 `gcp\Deploy-TtbRuntime.ps1 -Bootstrap -Apply`（首次會先裝 .NET 10／VC++／Python 並套用第 2 步主機設定），執行帳號為 `ttbuser`，詳見 [gcp/README.md](../../gcp/README.md)；防火牆用 `gcp\Set-TtbRdpAllowlist.ps1 -Apply`。 |
| 2 | VM（系統管理員 PowerShell） | `powershell -ExecutionPolicy Bypass -File C:\TTB\azure\Set-TtbVmHost.ps1 -Apply`：關閉 Windows Update 自動重開機、時區 Taipei；輸出 `dotnet10`、`vcRuntime` 必須為 true（TTB 2026.07.08 需三個 .NET 10 runtime，pywinauto 需 VC++ 2015-2022 x64）。 |
| 3 | VM（以 `azureuser`） | `cmdkey /generic:TaifexTradeBox/TTB /user:<TTB 帳號 e-mail> /pass`，提示時輸入 TTB 密碼（畫面不顯示）。`cmdkey /list:TaifexTradeBox/TTB` 確認。 |
| 4 | VM（系統管理員 PowerShell） | `powershell -ExecutionPolicy Bypass -File C:\TTB\azure\Set-TtbAutologon.ps1`：輸入 Windows 密碼，存成 LSA secret。之後 VM 重開機會自動登入 → `TTB Automation` 登入觸發 → 自動開 TTB 並登入競賽。 |
| 5 | VM（TTB GUI） | 手動登入一次 TTB，選競賽，在「報價設定」加入當月與次月代碼（例如 `TMFJ6`、`TMFK6`）；記下競賽列表中的名稱。 |
| 6 | VM | `C:\TTB\Sample\Python\automation\config.json`：以 `config.example.json` 為底，設 `symbol: "auto:TMF"`、`autoLogin: true`、`ttbCompetition: "<競賽名稱>"`。`Start-ScheduledTask 'TTB Automation'`，用 `py -3.13 -m automation.ttb_login --check-only` 確認 `logged_in=true`、`twd_available_margin_positive=true`（為 false 表示競賽帳戶尚未出入金，請在競賽網站把資金轉入台幣保證金帳戶）。 |
| 7 | VM | shadow 觀察至少一個交易時段：`Invoke-RestMethod http://127.0.0.1:8765/status` 的 `state` 應為 `healthy`、`last_quote_wall_utc` 持續更新；`output\supervisor\supervisor.log` 的 `ttb_login` 行應顯示 `"quotes": {"<近月>": "subscribed"}`。 |
| 8 | VM（系統管理員 PowerShell） | 切換實際下單：`cd C:\TTB\Sample\Python; powershell -ExecutionPolicy Bypass -File automation\set_mode.ps1 -Mode ttb`（乾跑，列出檢查）→ 全部通過後加 `-Apply`。它會套用 [config.competition.example.json](automation/config.competition.example.json) 的風控參數並重啟 supervisor。 |
| 9 | 自動 | 第一次 live 啟動時 `automation/live_gate.py` 在開盤後自動執行一口遠價委託／撤單測試（每個合約一次，紀錄在 `output\order-probes\verified-<代碼>.json`）；通過才啟動策略，失敗則留下人工對帳旗標並停止。之後每個交易時段自動進出場。 |
| 10 | 隨時 | 退回 shadow：`set_mode.ps1 -Mode shadow -Apply`。緊急停止：建立 `output\supervisor\stop.requested`（live 會先嘗試平倉再停）。 |

**啟用 live 的硬性前提**：此競賽帳戶**只給 bot 交易**（`exclusiveBotAccount: true`，自動恢復會撤銷所有委託並平掉所有期貨部位）；`set_mode.ps1` 檢查的認證、競賽名稱、API 登入、TWD 可用保證金 > 0、無對帳旗標缺一不可。`--max-notional`／`--min-available-margin` 預設只允許 1 口 TMF，依競賽資金調整。

**策略**：競賽設定使用 `strategies.combined:CombinedCompetitionMaker`（01＋03＋04）。04 的每日停損／停利以 `extraArgs` 中 `--strategy-params-json` 的 `previous_day_equity` 為基準（預設 2,000,000，取自 2026-10-28 TTB 權益數），權益明顯變動時請手動更新；未設定時 04 會拒絕所有新進場。每日成交另同步到 `output\trades.sqlite3`（見 README）。

**風險提醒**：自動化只保證「機器會自己跑」，不保證策略獲利；先以 shadow 的決策與模擬損益判斷是否上真單及口數。

GUI 自動登入已在 TTB 2026.07.08.1000（控制項 `txtAccnt`、`txtPwd`、`btnLogOn`、`cbGameList`、`dvQuote`）的 Azure VM 上實測：關閉 TTB 後 supervisor 自動重開、登入、選競賽並重啟 bot（2026-10-18）。TTB 改版後先用 `py -3.13 -m automation.ttb_login --dump-controls` 檢查。

其他注意事項：

- **報價訂閱在 TTB GUI**：TTBHelp 的 `QUOTEDATA` 只開本機 ZMQ 監聽，TTB 只推送「報價設定」頁已加入的商品；TTB 登入時會對已存清單逐一查基本報價，查不到（例如盤前或收盤後）就把該商品從清單**永久刪除**（2026-10-28 在 VM 實測，見反編譯的 `BaseForm.LoadSymbol`）。所以 `ttb_login --subscribe <近月>` 每次登入都會檢查，缺少時自動按「新增訂閱」補回，supervisor log 回報 `subscribed`、`added` 或 `add_failed:[...]`。補訂失敗時 live gate 收不到報價會延後，下一輪登入再補；換月由 `resolve_symbol` 決定近月，不用手動加次月。
- **API 連線不再經 TTBHelp 建構子**：`TTBApi.connect` 以相同端點直接 HTTP（10 秒 timeout），並用 TTBHelp 的版本字串做 `CONNECT` 握手。原 TTBHelp 在 TTB 未開或版本不符時會 `os._exit(0)`，supervisor 會誤判為正常結束。
- **TTB 2026.07.08.1000**（2026-10-18 起的 repo 版本）需要 .NET 10 Desktop Runtime；TTB.exe 與 `TTBHelp.py` 必須同版（握手會比對版本，`check_binary_integrity` 也會檢查）。

此頁為**操作單一權威來源**。策略研究文件僅用於設計與比較，不應凌駕本操作流程。
