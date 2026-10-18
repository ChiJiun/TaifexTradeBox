# TaifexTradeBox：見價見量虛擬交易研究專案

這是一個以 TaifexTradeBox（TTB）虛擬交易資料與介面為基礎的研究專案，研究在「見價見量」成交規則下，結合 microprice、買賣盤不平衡與 order-flow signals 的高頻類 market-making 策略。

> 本專案是研究與模擬交易工具，不是獲利保證，也不是未經驗證即可連接真實市場的自動交易系統。所有績效必須扣除交易費、稅、滑價與成交限制後，使用樣本外資料驗證。

## 專案內容

```text
Sample/Python/
  ttb/api.py                TTB 訂閱、下單、刪改單與查詢介面
  ttb/market_data.py        行情監控與 ZMQ 接收模組
  ttb/orders.py             shadow／TTB 委託及成交對帳模組
  ttb/engine.py             可注入策略的執行引擎與 CSV 紀錄
  ttb/health.py             watchdog 使用的原子化健康狀態檔
  ttb/probe.py              遠價 1 口委託／撤單探測器
  ttb/reconciliation.py     全帳戶唯讀對帳與稽核工具核心
  ttb/risk.py               TTB 委託前風控與跨重啟委託速率計數
  ttb/contracts.py          TAIFEX 臺指/小臺/微臺商品乘數解析與安全驗證
  ttb/journal.py            append-only、hash-chained 交易事件與跨當機成交回建
  ttb/calendar.py           交易所休市日、到期日、契約代碼與人工換月閘門
  strategies/               可替換的策略範例
  strategy_core.py          策略、microprice、OFI 與模擬成交核心
  sim_hft_bot.py            組合上述模組的命令列入口
  ttb_order_probe.py        虛擬帳戶下單與撤單 smoke test
  reconcile_ttb.py          安全檢查帳戶與人工授權清除停機旗標
  OPERATOR_RUNBOOK.md       事故對帳與復原操作手冊
  automation/               Azure 監督、健康檢查與排程安裝腳本
  replay_market_maker.py    quote CSV 離線回放器
  monte_carlo_market_maker.py 區塊拔靴蒙地卡羅與成交模型比較
  optimize_virtual_competition.py 虛擬競賽費後總獲利參數搜尋
  test_market_maker.py      market maker 單元測試
  test_strategy_core.py     原有策略單元測試
  README.md                 Python 程式使用說明
  MARKET_MAKER.md           策略規則、假設與驗收計畫
  DYNAMIC_MARKET_MAKING_PLAN.md 動態做市研究計畫
azure/
  README.md                 Azure Windows VM 部署指南
```

## 執行環境

- Windows（TTB 為 Windows GUI 程式）
- Python 3.13
- 已安裝並登入 TTB（TTB 模式才需要）
- `pyzmq`、`requests`

安裝相依套件：

```powershell
py -3.13 -m pip install -r Sample/Python/requirements.txt
```

## 快速開始

在 `Sample/Python` 目錄執行完整測試：

```powershell
cd Sample/Python
py -3.13 -m unittest discover -v
```

先用當期有效商品代碼執行 shadow。shadow 不送單，只記錄行情、策略決策與模擬成交：

```powershell
py -3.13 sim_hft_bot.py --symbol <ACTIVE_SYMBOL> --mode shadow
```

切換到均線策略範例（仍不送出委託）：

```powershell
py -3.13 sim_hft_bot.py --symbol <ACTIVE_SYMBOL> --mode shadow --strategy moving-average
```

產生的 quote 與 decision CSV 位於 `Sample/Python/output/`，該目錄不會提交到 Git。

## 離線回放

使用 shadow 產生的 quote CSV 重播策略：

```powershell
py -3.13 replay_market_maker.py output/runs/quotes-TIMESTAMP.csv --symbol <ACTIVE_SYMBOL>
```

離線回放適合檢查參數、成交規則與風險控制，但不等同真實排隊成交，也不代表策略具備 alpha。

## TTB 虛擬下單

只有在以下條件都完成後才使用 TTB 模式：

1. 已完成單元測試與數個交易日 shadow。
2. TTB 已登入並選擇正確模擬交易模式。
3. 商品代碼、交易時段與費用設定已核對。
4. 啟動前帳戶為空倉、沒有未成交委託。

第一次測試先不要啟動策略，先執行遠價 1 口委託／撤單探測器：

```powershell
py -3.13 ttb_order_probe.py `
  --symbol <ACTIVE_SYMBOL> --side buy --distance-ticks 2000 `
  --confirm-virtual-order-probe --confirm-flat-account
```

探測器會等待一筆有效行情，以遠低於最佳買價的限價送出 1 口，取得委託編號後立即撤單，最後查詢委託與成交回報。遠價只能降低成交機率，不能保證絕不成交；若結果顯示 `unexpected_fill_quantity` 大於零，必須立即在 TTB 人工核對並平倉。

```powershell
py -3.13 sim_hft_bot.py `
  --symbol <ACTIVE_SYMBOL> `
  --mode ttb `
  --confirm-virtual-orders `
  --confirm-flat-account
```

程式中斷、斷線或 VM 重啟後，必須先回 TTB 檢查實際持倉和委託，再重新啟動。live 虛擬委託模式遇到致命錯誤會 fail-closed，不會由 supervisor 無條件重啟，以免重複委託或疊加部位。**不得手動刪除** `manual-reconciliation-required.flag`；請依 [人工對帳操作手冊](Sample/Python/OPERATOR_RUNBOOK.md) 執行 `reconcile_ttb.py`，核對全帳戶後才授權清除。

## 策略概要

- 以 best bid / ask 數量估算 microprice。
- 結合 Level-1 order-flow imbalance 與近期成交方向。
- 中性訊號時提供雙邊報價；方向訊號時限制入口方向。
- 首筆成交後只允許減倉，避免無限制馬丁格爾加碼。
- 固定口數、固定停利停損、時間出場、日損失與最大回撤鎖定。
- Live TTB 入口單另經過 gateway 級別的 margin、notional、inventory、委託/撤單頻率檢查；實際 `QUERYMARGIN` 欄位未驗證前**不允許啟用新進場**。
- 行情中斷、未知委託回報或交易時段結束時停止開倉。

詳細規則與限制請閱讀 [Sample/Python/MARKET_MAKER.md](Sample/Python/MARKET_MAKER.md)。

## Azure 24/7 部署

TTB 需要 Windows GUI 與本機 API，因此建議使用單台 Windows Azure VM，而不是 Linux container 或 serverless：

- 建議 `Standard_B2as_v2`、64 GiB Standard SSD。
- 不要對外公開 TTB 的 `8080` 或 ZMQ `51141`。
- 使用 Tailscale 做後續安全連線。
- `automation/run_ttb_automation.ps1` 會維持程式、等待 TTB 本機端口，並在休市時保持待命。
- `automation/install_azure_tasks.ps1` 建立登入與每日排程；TTB GUI 仍需要互動式 Windows 工作階段。
- 先通過 shadow 與 `ttb_order_probe.py`，再把 `automation/config.json` 切換為 `ttb`。
- 建立 Azure Budget，並在非交易時段停止/釋放 VM 以降低成本。

完整命令與安全設定請閱讀 [azure/README.md](azure/README.md)。

**競賽全自動模式**（opt-in，見 [OPERATIONS.md §8](Sample/Python/OPERATIONS.md#8-競賽全自動模式opt-in)）：`symbol: "auto:TMF"` 依交易日曆自動換近月；`automation/ttb_login.py` 以 Windows 認證管理員的帳密自動登入 TTB 並選競賽（同時關閉 TTB 預設開啟的「下單失敗自動重送」）；bot 異常時 `auto_recover_ttb.py` 在每日次數上限內撤單、市價平倉並經兩次檢查後才重啟。`azure/Deploy-TtbRuntime.ps1` 可一鍵把已提交的版本部署到 VM 並在測試失敗時還原。

### 目前 Azure 實例

目前已建立一台用於 TTB 研究的 Windows VM：

```text
資源群組：a112502533_rg_3957
VM：ttb-sim-01
區域：East Asia
規格：Standard_B2as_v2
系統：Windows Server 2022
固定公網 IP：20.189.93.22
RDP 來源限制：2026-10-13 已改為管理端目前出口 IPv4 的 `/32`；變更網路時請先更新 Azure NSG 白名單，禁止改回 `*`
```

RDP 連線：

```powershell
mstsc /v:20.189.93.22
```

2026-10-18 唯讀檢查：VM 執行中，但**沒有 Windows 登入工作階段**，TTB 與 bot 都沒有在跑，部署版本仍是 9 月舊版，`config.json` 仍是 shadow／`TMFJ6`（10/21 到期）。以下為歷史紀錄：截至 2026-09-24 10:30（Asia/Taipei），VM 已部署本專案最新版；`TTB Automation` 正以 `TMFJ6`、`shadow` 模式執行。健康檢查已確認 TTB/Python 程序、8080/51141、本機健康心跳及即時 tick 全部正常，部位為 0、沒有活動委託。策略因 shadow 的模擬時段損失上限進入 `paused`，監測仍持續，但這不是正 alpha 的證據，因此尚未啟用自動虛擬委託。程式端遠價下單／撤單仍須在人工確認空倉且沒有未成交單後，以 `ttb_order_probe.py` 完成端到端驗證。管理員密碼不會儲存在 repo 或 README。

### 「24/7」的實際界線

- Bot 可以全天候常駐，休市時寫入 `waiting_market` 健康狀態；預設同時辨識日盤 `08:45–13:45` 與夜盤 `15:00–05:00`，並可由 CLI 覆寫或停用夜盤。
- 2026 年臺指系列的休市/特殊交易日由 `automation/taifex_calendar_2026.json` 控管；2027 年日曆尚未核准前，不會繼續沿用 2026 日曆。到期月合約最後交易日上午 13:30 收盤，當日下午沒有夜盤；期滿不可在原商品自動開新倉，須人工完成換月與帳戶核對。
- 同一交易日的 realized PnL、drawdown halt、勝負 streak 與 sizing 狀態會原子化寫入 `output/risk-state.json`；程序重啟後會恢復，避免藉由重啟意外洗掉風控。
- 本機 `127.0.0.1:8765` 提供 `/healthz`、`/readyz` 與 `/status`，供 VM watchdog 或監控代理 probe；不要把此 port 公開到 Internet。
- 行情 ZMQ 訂閱使用精確商品 topic、有限緩衝區、時間與成交量完整性檢查；拒收重複、亂序或過期的 tick，不把舊訊息視為有效新行情。詳細拒收計數見 health。
- 交易時行情中斷會撤銷已知掛單並暫停；行情恢復後先觀察連續新 tick，持倉不為零時優先送出風險退出意圖。若曾正常收到行情後持續斷線超過預設 120 秒，程序會 fail-closed 結束；shadow 可由 supervisor 重啟，live TTB 則停止並要求人工對帳。
- TTB 是 GUI 程式；RDP 可以中斷，但不可登出 Windows。VM 重開機後仍可能需要人工登入 TTB、選擇正確競賽並核對帳戶。
- Live gateway 會用 TTB 的委託報告與 `QUERYRESTOREFILLREPORT()` 做 broker-side 持倉對帳；若發現既有 working order、非零持倉或持倉長時間不一致，仍會 fail closed。異常退出後 supervisor 不會自動重啟 live 模式，避免未知狀態下重送委託。
- quote/decision CSV 會依台北日期每日輪替；runtime 檔預設保留 14 天，supervisor log 超過 10 MB 會輪替，避免長時間運作塞滿磁碟。
- `TTB Health Watchdog` 每 5 分鐘執行健康檢查；若設定環境變數 `TTB_ALERT_WEBHOOK_URL`，故障轉態、持續故障每小時與恢復時都可送出 webhook。

非使用期間可釋放 VM 以停止計算費用：

```powershell
az vm deallocate --resource-group a112502533_rg_3957 --name ttb-sim-01
```

## Windows CI 與離線 Release 驗證

每個發往 `main` 的 pull request，以及推送至 `main` 的 commit，都會由 [GitHub Actions / Validate Python and PowerShell](.github/workflows/validate.yml) 使用 Windows runner、Python 3.13 執行：安裝 `requirements.txt`、`unittest`、`compileall`、CLI `--help`、所有 automation PowerShell 腳本語法檢查，以及 `validate_release.ps1 -SkipUnitTests`。測試與語法錯誤會導致 CI job 失敗；可以從該次 GitHub Actions run 取得完整執行 log。CI 不啟動 TTB GUI，也不送出任何委託，不需要 GitHub secrets。

本機或 Azure 發版前可執行：

```powershell
cd Sample/Python
powershell -ExecutionPolicy Bypass -File automation/validate_release.ps1 -WorkDir (Get-Location).Path
```

CI 成功**不能取代** TTB 虛擬帳戶的人工驗證（[#2](https://github.com/ChiJiun/TaifexTradeBox/issues/2)）。正式部署還須通過 `preflight_ttb.ps1`，確認登入帳戶、持倉、委託、API 回覆及環境安全。此 CI 的角色是守住可重現的離線測試與封裝完整性。

## 研究與驗收原則

目前實作是研究版本，尚未完成官方撮合器的端到端校準。下一步應取得多個交易日資料，按日期做樣本內/樣本外切分，至少檢查：

- 扣費後損益與最大回撤
- 成交後價格漂移與 adverse selection
- 委託取消、部分成交與重複行情
- 持倉尾部時間與斷線恢復
- 不同商品、波動狀態與交易時段的穩定性

只有在上述驗收通過後，才考慮調整口數或加入更複雜的風險恢復機制。

## 相關文件

- [Python 程式 README](Sample/Python/README.md)
- [策略詳細說明](Sample/Python/MARKET_MAKER.md)
- [動態做市與蒙地卡羅計畫](Sample/Python/DYNAMIC_MARKET_MAKING_PLAN.md)
- [原有策略說明](Sample/Python/STRATEGY.md)
- [TTB 單一操作手冊](Sample/Python/OPERATIONS.md)：啟動、風控、異常、恢復及操作檢查
- [Azure 部署說明](azure/README.md)（含 TTB.exe/TTBHelp.py 完整性快照與人工升級流程）
- [RDP 白名單管理腳本](azure/Set-TtbRdpAllowlist.ps1)：查核或限制 Azure NSG 3389 至管理端目前 IPv4
