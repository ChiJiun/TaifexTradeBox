# TTB Python 策略程式

> **唯一操作入口：[OPERATIONS.md](OPERATIONS.md)**。本頁說明技術模組及範例，若與操作手冊衝突，應先依操作手冊處理。

這個目錄包含 TTB 虛擬交易的行情監控、委託模組、策略執行器、離線回放器與測試。策略目標是研究「見價見量」成交規則下的 microprice / order-flow market making；目前不宣稱已取得 alpha，也不保證獲利。

## 多模組架構

```text
TTBHelp.pyc
  └─ ttb/api.py             TTB API：訂閱、新單、刪單、改價、改量、查詢
      ├─ ttb/market_data.py 行情註冊、ZMQ 接收、Quote 轉換
      └─ ttb/orders.py      shadow 模擬與 TTB 委託／成交對帳
           └─ ttb/engine.py 策略迴圈、風險時段、CSV 紀錄
                └─ strategies/  可替換策略
```

`ttb/health.py` 會原子化更新 `output/health.json`，供 Azure watchdog 判斷程序狀態；`ttb/probe.py` 與 `ttb_order_probe.py` 則負責獨立於策略的遠價下單／撤單測試。

`strategy_core.py` 保留共用的資料模型、部位帳本、特徵與內建 market maker。`sim_hft_bot.py` 現在只負責組合模組及處理命令列參數；舊程式從該檔匯入 `TTBOrderGateway` 的方式仍可使用。

## 快速開始

需要 Windows、Python 3.13，以及已啟動並登入的 TTB。先安裝相依套件：

```powershell
py -3.13 -m pip install -r requirements.txt
```

執行測試：

```powershell
py -3.13 -m unittest discover -v
```

## 執行模式

先以當期有效商品代碼替換 `<ACTIVE_SYMBOL>`，建議先執行至少數個交易日的 shadow：

```powershell
py -3.13 sim_hft_bot.py --symbol <ACTIVE_SYMBOL> --mode shadow
```

程式可跨休市時段保持執行。預設日盤 `08:45–13:45`、夜盤 `15:00–05:00` 都會被辨識為交易時段；可使用 `--regular-open/--regular-close/--after-hours-open/--after-hours-close` 覆寫，或用 `--disable-after-hours` 停用夜盤。休市時健康狀態為 `waiting_market`；交易時行情超過 `--quote-stale-seconds` 未更新，會撤銷已知掛單並進入 `stale_market_data`，收到 `--resume-fresh-ticks` 筆新行情後才恢復入口決策；若曾經收到過行情後持續斷線超過 `--stale-fatal-seconds`（預設 120 秒），程序會 fail-closed 結束，讓 shadow supervisor 可重啟，而 `ttb` live 模式則留下人工對帳旗標後停止。

同一交易日的風控狀態預設保存到 `output/risk-state.json`，包含 realized PnL、peak equity、halt reason 與 sizing streak；程序重啟時會恢復。這個檔案只用於 session risk persistence，不用來恢復未知持倉或工作中委託，live TTB 仍以 API 對帳為準並 fail closed。

本機監控端點預設綁定 `127.0.0.1:8765`：`/healthz` 表示程序存活、`/readyz` **僅在 `healthy` 回 ready，其餘狀態回 503**、`/status` 回傳完整 health snapshot。可用 `--status-port 0` 停用。

也可以用同一個行情、委託和帳本模組執行範例均線策略：

```powershell
py -3.13 sim_hft_bot.py --symbol <ACTIVE_SYMBOL> --mode shadow `
  --strategy moving-average --short-window 5 --long-window 20 --entry-ticks 0.5
```

**虛擬下單仍不可直接依兩個確認旗標啟用。** 必須完成 #2/#4 真實 API／margin 單位驗收、所有工作中委託與持倉對帳，並配置正確的限額後才可由操作員考慮切換。詳細安全流程以 [OPERATIONS.md](OPERATIONS.md) 為準；舊版缺少必要風控的 live 指令已移除。

異常退出、網路中斷或重啟後，必須先在 TTB 檢查實際掛單與部位，再重啟程式。不可把此程式視為保證斷線平倉的無人值守系統。

**委託所有權防護（#11）**：TTB gateway 會拒絕執行期間新出現、且沒有被 bot 以委託編號識別的 working/filled order（包含手動委託）。即使該委託的方向、價格和數量剛好符合 pending intent，也**不能**據此當成 bot 自己的單。若 `NEWORDER` 成功但沒有回傳可驗證的委託編號，bot 同樣會停機，將 pending 狀態交由人工確認。此限制可能使目前的 TTB API 無法直接無人值守下單；須完成真實回覆格式驗證後再設計安全的 ID correlation，不應直接移除防護。錯誤時健康狀態會包含 `active_orders.unknown_orders` 和 `active_orders.unsafe_reason`。

## 重要安全修正與商品參數

`--point-value` 不再預設 200：TXF、MTX、TMF 會分別依期交所契約規格自動使用 **200、50、10 元／點**。其他商品必須明確指定，且已知商品不接受錯誤覆寫；`--fee-per-fill`、稅率仍必須依商品及虛擬競賽規則校準。這也修正過去範例 `TMFJ6` 可能用 200 元／點錯算損益與曝險的問題。

TTB 下單前對帳回覆缺少 `Data`、委託剩餘量/持倉口數缺失、持倉方向不明時一律 fail-closed，不會把未知狀態解釋為空倉。單筆委託數量也由 gateway 限制不得超過 `--max-order-qty`。週五夜盤跨至週六凌晨仍屬下一個營業日的風控交易日；損毀或不同商品的 `risk-state.json` 不再被忽略並重置當日損益。

**真實 TTB API 的完整回覆格式仍待 #2 核實**；保守拒絕不代表已完成真實帳戶驗收。

## Walk-forward／Out-of-sample 研究驗收（#8）

以 `walk_forward_oos.py` 一次執行嚴格日期切分、expanding-window Walk-forward、三種成交模型、區塊 bootstrap 蒙地卡羅、費稅滑價與未平倉退出成本、1／2／5／10 秒 signed mid-price drift、參數鄰域敏感度及固定的 acceptance gates。最後的 Holdout 日期**從不參與選參數或敏感度排名**。產出 JSON（CI/agent）與 Markdown（人工審查）。

先做不連線也不會送單的 smoke test：

```powershell
cd Sample/Python
py -3.13 walk_forward_oos.py --symbol TMFJ6 `
  --synthetic-days 8 --quotes-per-day 36 `
  --fee-per-fill 20 --tax-rate 0.00002 --slippage-ticks 1 `
  --target-ticks 4,6 --half-width-ticks 2,4 `
  --mc-trials 5 --output output/research/synthetic-walkforward
```

合成資料的 `production_research_gate_pass` **永遠是 false**，即使參數碰巧獲利。使用真實報價：

```powershell
py -3.13 walk_forward_oos.py output/runs `
  --symbol TMFJ6 --fee-per-fill <VERIFIED_FEE> `
  --tax-rate <VERIFIED_TAX_RATE> --slippage-ticks <VERIFIED_SLIPPAGE> `
  --holdout-days 2 --initial-train-days 3 `
  --source-manifest <OPERATOR_VERIFIED_MANIFEST.json> `
  --output output/research/tmf-walkforward
```

輸入 CSV 必須命名 `quotes-YYYYMMDD[-session].csv`，至少六個**不同日期**且每個日期只有一份資料；若同一天有多段紀錄，必須先校正並人工合併，不得跨日隨機拆分。需要人工核對的來源 manifest 格式：

```json
{
  "source": "verified_ttb_quotes",
  "trading_day_reconciled": true,
  "reviewer": "OPERATOR_NAME",
  "reviewed_at": "YYYY-MM-DD",
  "files": {"quotes-YYYYMMDD-HHMMSS.csv": "<ACTUAL_FILE_SHA256>"}
}
```

其中 `files` 必須列出本次全部輸入檔案的真實 SHA256。**不得自動為合成資料產生假「verified」manifest**。此檔只證明有人聲明核對且檔案未被修改，程式無法獨立確認交易所來源真實性。沒有 manifest 或缺少完整成交日、盈虧、費率、滑價等資料，報告仍可產生，但不可通過研究驗收。

內建門檻：留存期至少 100 次平倉循環、費後損益正值、正確持倉結束、回撤不超過 30 萬元、最差單日不低於負 20 萬元、任何單日不獨占超過 50% 正報酬、成交率至少 1%、`probabilistic` Monte Carlo 虧損率不高於 10%。CLI 只允許**加嚴**這些限制。Sharpe 報告為「未年化日 Sharpe」，不得直接視為年化值。三種成交模型不是 broker 實際成交承諾；研究驗收也**不是自動下單核准**，仍須 #2/#4 真實 API/保證金與帳戶人工驗收。

## 多商品 Shadow 組合監控（#12，**不允許 multi-live**）

`automation/shadow_portfolio.py` 可依 JSON manifest 同時管理 2–10 個不同商品的 Shadow 子程序，每個實例擁有獨立 `output/portfolio/instances/INSTANCE_ID/` 下的 health、risk-state、Journal、logs、stop-file，以及各自的 `127.0.0.1` HTTP port。監控端彙總**已估值損益及名目曝險**，依事前門檻執行全域停機；任何子程序異常退出、健康資料損毀、超過心跳期限、交易日不一致或風控超限，都會建立 `portfolio-halt.flag` 並要求全體停止。跨重啟的日內權益峰值保留於 `portfolio-risk-state.json`，不會因重啟清零；`portfolio-health.json` 提供彙總狀態。

```powershell
cd Sample/Python
Copy-Item automation/shadow_portfolio.example.json automation/shadow_portfolio.json
# 更新 symbol（必須為當前有效合約）、商品乘數、費用與限額
py -3.13 -m automation.shadow_portfolio --config automation/shadow_portfolio.json
# 唯讀 dry-run 確認每個子程序的參數與路徑後，才啟動：
py -3.13 -m automation.shadow_portfolio --config automation/shadow_portfolio.json --run

# VM Windows Task Scheduler：預設 dry-run，明確 -Apply 才註冊 Shadow 任務
powershell -ExecutionPolicy Bypass -File automation/install_shadow_portfolio_task.ps1
```

組合損益採各實例期貨帳本的 `marked_pnl`（扣估計平倉成本），名目曝險按 `abs(position) × last_price × point_value`；它們**不是** broker 證實的可用保證金或帳戶風險指標。**程式碼會拒絕 manifest 中的 `mode=ttb` 或任何企圖以額外參數覆寫 Shadow 的行為。** #12 尚需 broker 多商品保證金與全帳戶統一限額、同時下單競態及真實 VM 長期運行驗收，因此只完成 Shadow 子集。正常操作不得手動清除全域停機旗標；先查明事件原因並重新驗收。

## 策略模組（#9，僅離線研究驗證）

三個新模組可透過 `--strategy-module` 載入，且**所有委託都必須經過 TTB Gateway**，沒有直接送單權限：

```powershell
py -3.13 sim_hft_bot.py --symbol TXFA7 --mode shadow `
  --strategy-module strategies.sticky_maker:StickyOneTickMaker

py -3.13 sim_hft_bot.py --symbol TXFA7 --mode shadow `
  --strategy-module strategies.order_flow_maker:OrderFlowGatedMaker `
  --strategy-params-json '{"entry_threshold":0.25,"short_window":8,"long_window":40}'

py -3.13 sim_hft_bot.py --symbol TXFA7 --mode shadow `
  --strategy-module strategies.competition_pnl:CompetitionPnlShaping `
  --strategy-params-json '{"previous_day_equity":2000000,"daily_profit_fraction":0.002}'
```

`previous_day_equity` 必須事先從正確帳戶核實；未提供時，CompetitionPnlShaping 預設拒絕新進場。停機原因由 engine 的既有風控快照保存。配置參數可以序列化為 JSON，但不能以此繞過單筆口數、保證金、名目曝險或委託頻率限制。有限攤平（02）與最終 TSMC 組合（05）仍未實作，現有策略也尚未通過真實 TTB 資料的 walk-forward 成效及 Live API 驗收。

## 24/7 加速故障測試（#10）

離線測試完全使用 `ShadowGateway`，不載入 TTB GUI 或向交易 API 下單：

```powershell
cd Sample/Python
py -3.13 -m automation.soak_offline --hours 48 --interval-minutes 10
```

固定日期從 2026-10-13 開始，以虛擬時鐘模擬 48 小時，涵蓋日盤、夜盤、休市及午夜切換，每 10 分鐘送入一筆有效行情，檢查 quote/decision CSV 每日輪替和行數、持倉仍為零，並輸出 Python traced heap 峰值、保留量、輸出檔總位元組和執行秒數。此測試已加入 GitHub Actions，超過檔案/記憶體門檻或資料遺失會使 CI 失敗。

`test_soak_faults.py` 另注入行情寫入及健康檔同時失敗、交易 Journal 寫入失敗、並行 HTTP status 查詢、以及 `/readyz` 對行情過期／暫停的拒絕。Worker 當機時保證 `stop_event` 設置，外部 supervisor 才能察覺錯誤並執行既有 fail-closed 流程。

**限制**：48 小時指的是加速市場時鐘，不是實際連續運行 48 小時；報價負載與 Windows 真正開機登入、TTB GUI 斷線及 broker 競態無法只靠此測試保證。#10 尚需實際 VM soak、完整 supervisor 故障注入與資源監測證據。

## 交易日曆與契約到期（#6）

`ttb/calendar.py` 支援官方公告的開休市日及 TXF／MTX／TMF 契約月碼 `A`～`L`、年份個位數。2026 年日曆在 [automation/taifex_calendar_2026.json](automation/taifex_calendar_2026.json)，來源為[期交所 115 年開休市公告](https://www.taifex.com.tw/file/taifex/CHINESE/11/attach/台期交字第1140003036號函.pdf)。此檔只涵蓋 2026 年，不能預設適用 2027 年；公告若修改，須先更新並複核。

- 休市日期（包含春節封關 2/12、2/13）回報 `exchange_holiday`，不當成交易時段的行情中斷；2/11 下午起的春節前最後夜盤，交易日風控歸入 2/23。
- 到期月份合約的最後交易日原則上為第三個星期三（如 `TMFJ6` 為 2026-10-21）；當天日盤 13:30 截止，13:30 後及當天下午不再進入該合約夜盤。到期日停止開新倉，有持倉則優先送出風險退出意圖。
- 期滿商品回報 `manual_contract_rollover_required`、不再自動下單。系統不會自動挑選下一個商品，也不會假設舊倉可以移到新月份。啟動新月份商品前會檢查**整個帳戶的所有商品**，若舊合約仍有持倉或未成交單則拒絕啟動；須先在 TTB GUI 人工完成對帳，核對全部部位為零後再修改 `symbol`。
- 指定日曆無法涵蓋目前日期（例如 2027 年）則回報 `calendar_unverified` 並停止 live 新進場。若有新年度官方日曆，先匯入/核對，再使用 `--calendar-file <VERIFIED_JSON>` 啟動。

**限制**：未來年度休市表、臨時停市與交易規則異動無法預測。實際啟用 live 前仍須人工比對期交所當日公告與 TTB 競賽模式；絕不可僅憑計算日期自動換倉。

## 行情完整性、積壓與停止指令（#13）

即時訂閱改成精確商品 ZMQ topic `TTB$Tick$SYMBOL`；ZMQ `RCVHWM=1000` 防止無限制佇列膨脹。若網路層積壓超過上限，底層訊息可能被捨棄；策略端 20,000 筆 queue 滿時採**丟棄最舊行情**，但若進入策略的行情等待超過 2 秒則停機，不會拿過時報價繼續下單。

`QuoteMonitor` 對有效報價檢查重複內容、倒退時間、成交量異常重置與 TickTime 對照臺北時間的延遲（預設 `--max-exchange-lag-seconds 15`）。無法解析 TickTime 的即時訊息同樣拒絕；拒絕的訊息不會進入策略，因此不會把行情中斷狀態偽裝成正常。`/status`、`health.json` 的 `market_data` 包含 `received`、`accepted`、`duplicates`、`out_of_order`、`lagged`、`volume_resets` 等計數。由於 TTB 真實 TickTime 格式尚待 #2 驗證，若資料不符合 `HH:MM:SS[.fraction]`，會安全拒絕而非猜測時區或解除保護。

Supervisor 傳遞 `--stop-file output/supervisor/stop.requested`，bot 每秒檢查是否收到停止請求並執行既有關機對帳；若當時仍有持倉或撤單尚未確認，仍可能留下人工對帳旗標。不得把 stop file 當成「保證立即平倉」的命令。

## Live 下單前風控（#4）

TTB gateway 在送新入口單之前會檢查：

- `--max-orders-per-minute`（預設 20）、`--max-cancels-per-minute`（預設 60；不禁止安全性撤單，但過量後封鎖新的入口）、`--max-daily-orders`（預設 250，台北日曆日）。
- `--max-inventory`：計入當前部位與雙向工作中委託的最壞淨持倉。
- `--max-notional`：依商品乘數與委託價估算的名目曝險上限，live 進場**必須明確設定正數**。
- `--available-margin-field` 與 `--min-available-margin`：從 `QUERYMARGIN()` 的指定欄位確認可用保證金下限；**欄位必須先由真實 TTB 虛擬帳戶的回報驗證，不得猜測**。未提供欄位或回覆格式不確定則拒絕新入口。

次數統計先寫入 `output/rate-counters-SYMBOL.json` 再送 API，正常重啟不會歸零。該檔案**不可人工重設**，也不會被 14 天的 CSV cleanup 移除。持倉風險退出採 reduce-only 驗證，仍允許在超過一般進場速率限制時執行風險退出。

Gateway 的保證金、持倉/未成交單名目曝險、單筆委託口數和送單頻率已統一交由 `ttb.risk.assess_pretrade()` 驗證。若不符合條件，**不會呼叫 `NEWORDER`**；封鎖原因記錄於 Journal 的 `risk_block` 事件，並顯示在 `gateway.snapshot().risk_block_reason` 和健康狀態的 `risk_block_reason`。可使用以下完全離線、以合成 margin fixture 測試同一份風控程式碼：

```powershell
py -3.13 -m unittest test_shared_pretrade_policy -v
```

此測試不會連到 TTB。合成 fixture 只驗證判斷和阻擋機制，**不代表 `QUERYMARGIN` 真實欄位及單位已證實**，也不應以合成欄位啟用 live。`QUERYMARGIN()` 實際欄位語義仍待 #2 端到端驗收確認。

使用 supervisor 切換 `config.json` 為 live 時，`extraArgs` **必須**額外包含（以下全為待人工校準的 placeholder，不能直接用於 live）：

```json
"extraArgs": [
  "--base-qty", "1", "--max-order-qty", "1", "--max-inventory", "1",
  "--max-notional", "<VERIFIED_NOTIONAL_LIMIT>",
  "--available-margin-field", "<VERIFIED_TTB_FIELD>",
  "--min-available-margin", "<VERIFIED_MARGIN_HEADROOM>"
]
```

未填正數或未能解析欄位時，preflight 或 gateway 將 fail closed。沒有 TTB 實際資料時，不應取消這些檢查來嘗試虛擬 live 交易。

## Durable Execution Journal（#5）

虛擬委託模式會把 `order_intent`、`order_ack`、`cancel_intent`、`cancel_ack`、broker 委託/部位摘要、`fill`、風控 halt 變化和 shutdown 寫進 `output/journal/events-SYMBOL-YYYY-MM-DD.jsonl`。每筆事件含序號及雜湊鏈，寫入後 `fsync`；成交**先寫入 journal，再更新 ledger**。報告沒有變動則不重複寫入。

異常重啟後會驗證現有事件檔完整性，並透過當日 `fill` 重建成交口數、已實現損益、交易成本及累積成交量。若 journal 顯示未平倉、snapshot 包含 journal 沒有的成交、或事件檔截斷/損毀，會拒絕啟動並要求人工對帳；不會因重啟默默重置損失限制。Journal 為 append-only 按風控交易日分檔，不會由既有 14 天 runtime cleanup 自動刪除。請另設受控備份與保留期限，不要手動編輯或清除 journal。可用 `--journal-dir` 指定路徑。

**限制**：此功能不等同真實 TTB 帳戶身分已驗證，事故後仍須先完成 [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md)；無法用 journal 推定失去委託編號的單屬於 bot。新版本首次部署前，若已經發生交易但沒有舊版 journal，將因 checkpoint 不一致而拒絕直接啟動，應先人工核對。

## 異常中斷與人工對帳

`reconcile_ttb.py` 提供唯讀檢查 TTB 全帳戶（包含不同商品）持倉與未成交單的 CLI：

```powershell
py -3.13 reconcile_ttb.py
```

唯讀輸出不會清除 `manual-reconciliation-required.flag`。必須先在 TTB GUI 確認正確虛擬帳戶、所有商品空倉/無掛單且 bot 已停止；只有在獨立重複查詢均通過後，才可執行：

```powershell
py -3.13 reconcile_ttb.py --clear-flag `
  --confirm-virtual-account --confirm-flat-account `
  --confirm-no-working-orders --confirm-bot-stopped
```

稽核紀錄寫入 `output/reconciliation/`，這個 CLI 不會自動下單、撤單或平倉。實際競賽/帳戶身分仍需要 GUI 人工確認，不能只相信 API 回覆。完整流程參見 [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md)。

## 真實 TTB API 驗證準備（#2）

**先進行完全唯讀的去識別化擷取，不會下單或撤單：**

```powershell
cd C:\TTB\Sample\Python
py -3.13 ttb_readonly_capture.py
```

輸出到 `output/api-captures/ttb-readonly-*.json`，只保留委託/持倉的必要欄位、對單號加鹽雜湊，保證金僅保留欄位名稱與型別，不保存原始帳戶識別資訊或金額。**人工檢視輸出是否仍含有敏感欄位後**才能將去識別化 fixture 用於 GitHub 測試。TTB 帳戶身分仍必須從 GUI 確認，API 未證實可以判別競賽帳戶。實際執行時沒有已核對的資料就不要猜 `--available-margin-field`。

以下 smoke test **真的會送出一口虛擬限價單並嘗試撤單**，只能在 GUI 已確認正確競賽、帳戶空倉且 bot 已停止後，由操作員明確授權執行：

```powershell
py -3.13 ttb_order_probe.py `
  --symbol TMFJ6 `
  --side buy `
  --distance-ticks 2000 `
  --confirm-virtual-order-probe `
  --confirm-flat-account `
  --confirm-bot-stopped
```

保護措施：

1. 若 Supervisor 的停機／待對帳旗標存在則拒絕探測。
2. 對全帳戶**所有商品**連續做兩次持倉及掛單檢查；有部位、掛單、資料不完整或 API 錯誤時，**零送單**。
3. 遠價送出 1 口 `NEWORDER` 後，**只能使用 API 回覆中可信任的 `OrdNo` 撤單**。缺號不會從方向、價格、口數推斷單號或撤銷不明委託，必須人工對帳。
4. 取得 `OrdNo` 才嘗試 `CANCELORDER`，等待終態，再重複查詢全帳戶持倉／工作中委託。此持倉 endpoint 是 `QUERYRESTOREFILLREPORT`，**不是歷史逐筆成交明細**。
5. 僅成功完成探測時輸出 `output/order-probes/order-probe-*.json`；若出現 API 例外或未知委託狀態，請查看 TTB GUI，不能憑程式退出碼假設單已撤銷。

遠價不保證零成交，也不能排除 API 回報延遲或撤單與成交競態。實際 TTB 回覆資料尚未驗證，**Issue #2 不會因此關閉**。

## Azure 常駐執行

先建立不提交到 Git 的實際設定：

```powershell
Copy-Item automation\config.example.json automation\config.json
```

`config.example.json` 預設為 shadow。完成委託探測、確認帳戶空倉後，才可將 `config.json` 改為：

```json
{
  "symbol": "TMFJ6",
  "mode": "ttb",
  "strategy": "market-maker",
  "confirmVirtualOrders": true,
  "confirmFlatAccount": true,
  "flattenOnShutdown": false,
  "extraArgs": [
    "--base-qty", "1", "--max-order-qty", "1", "--max-inventory", "1",
    "--max-notional", "<VERIFIED_NOTIONAL_LIMIT>",
    "--available-margin-field", "<VERIFIED_TTB_FIELD>",
    "--min-available-margin", "<VERIFIED_MARGIN_HEADROOM>"
  ]
}
```

啟動前先跑 release validator 與 preflight，再啟動與檢查：

```powershell
powershell -ExecutionPolicy Bypass -File automation\validate_release.ps1
powershell -ExecutionPolicy Bypass -File automation\preflight_ttb.ps1
powershell -ExecutionPolicy Bypass -File automation\run_ttb_automation.ps1
powershell -ExecutionPolicy Bypass -File automation\check_ttb_health.ps1
Invoke-RestMethod http://127.0.0.1:8765/status
```

安裝 Azure Windows 排程（需在系統管理員 PowerShell 執行）：

```powershell
powershell -ExecutionPolicy Bypass -File automation\install_azure_tasks.ps1 -RunAsUser azureuser
```

排程會在 `azureuser` 登入與每日 08:35 啟動，且忽略重複實例；另外建立 `TTB Health Watchdog` 每 5 分鐘健康檢查。supervisor 記錄於 `output/supervisor/supervisor.log`；bot 詳細 log 寫入會自動輪替的 `output/supervisor/bot.log`（10 MB × 最多 6 份），stdout/stderr 只保留少量主控台輸出。shadow 異常可由 supervisor 重啟；`ttb` 模式若非正常結束，會建立 `output/supervisor/manual-reconciliation-required.flag` 並停止，避免在未知持倉下自動重送委託。若 VM 使用者環境設定 `TTB_ALERT_WEBHOOK_URL`，watchdog 可在故障與恢復時送 webhook。

quote/decision CSV 依台北日期每日輪替；`cleanup_runtime.ps1` 預設保留 14 天 runtime CSV／order-probe 檔，supervisor log 超過 10 MB 也會輪替。`flattenOnShutdown=true` 只會在 `ttb` 模式、交易時段、且 bot 有有效行情時，於正常停止流程嘗試 IOC flatten；預設為 `false`，避免未經確認自動平倉。

TTB 登入並選定競賽後，程式會透過官方 `QUOTEDATA()` 註冊 `--symbol` 的行情，再從本機 ZMQ `51141` 接收 tick。若 shadow 已顯示 `subscribed`、但 quote CSV 仍是空檔，請先在 TTB 確認該商品代碼有效、競賽/重播模式正在送行情，且行情畫面有更新；這不是下單功能失敗。

### Azure 驗證紀錄（2026-09-24）

`ttb-sim-01` 已以 `TMFJ6 --mode shadow --strategy market-maker` 啟動最新版。健康腳本確認 TTB/Python 程序、8080/51141、健康心跳及即時 tick 正常，當時部位 0、沒有活動委託。策略後續因 shadow 模擬損失上限進入 `paused`，但監測程序沒有退出。這個結果驗證了行情鏈路、常駐監督及 fail-closed 風控，不代表策略具有正 alpha。`ttb_order_probe.py` 尚未對帳戶執行；必須先在 TTB GUI 人工確認空倉及沒有未成交單。

### TTB 商品代碼格式

網頁交易畫面的顯示代碼與 TTB Python API 的 `Symbol` 可能不同。TTB 隨附手冊使用 `TXF` 加上月份字母與年份尾碼：`A` 至 `L` 分別對應 1 月至 12 月。因此網頁顯示的 `TXF.2610`（2026 年 10 月）在 TTB API 應使用 `TXFJ6`，而不是常見海外期貨格式的 `TXFZ6`。一律以 TTB 的「報價設定」可成功新增的 `Symbol` 為準。

## 撰寫自己的策略

策略只需實作 `decide(quote)`，並回傳 `StrategyDecision`。下單不要直接呼叫 `NEWORDER`；回傳 `OrderIntent` 後，`BotEngine` 會依啟動模式送到 `ShadowGateway` 或 `TTBOrderGateway`，同一份策略便能先模擬、再連虛擬帳戶。

```python
from strategy_core import OrderIntent, StrategyDecision

class MyStrategy:
    def __init__(self, feature_engine):
        self.features = feature_engine

    def decide(self, quote):
        features = self.features.update(quote)
        if features.score > 0.3:
            order = OrderIntent("buy", quote.bid, 1, "entry", "my signal")
            return StrategyDecision((order,), features, False, "buy")
        return StrategyDecision((), features, False, "wait")
```

可直接複製 [strategies/moving_average.py](strategies/moving_average.py) 再修改。若 class 建構子接受 `(config, ledger)` 並實作 `decide(quote)`，不必修改 runner，可直接用 `--strategy-module your_module:YourStrategy` 載入；Azure `automation/config.json` 也可設定 `"strategyModule": "your_module:YourStrategy"`。內建策略仍可用 `--strategy market-maker|moving-average`。

若是人工測試單一 API，而不是自動策略，可使用型別化介面：

```python
from ttb import TTBApi, TTBOrderRequest

api = TTBApi.connect("http://localhost:8080", 51141)
response = api.new_order(TTBOrderRequest("TXFJ6", "buy", 22000, quantity=1))
api.replace_price(response["OrdNo"], 21999)
api.replace_quantity(response["OrdNo"], 1)
api.cancel_order(response["OrdNo"])
```

上述呼叫會真的作用在目前登入的 TTB 虛擬帳戶；策略開發仍應先使用 `--mode shadow`。

## 離線回放

`sim_hft_bot.py` 會記錄 quote CSV，可用相同策略重播：

```powershell
py -3.13 replay_market_maker.py output/runs/quotes-TIMESTAMP.csv --symbol <ACTIVE_SYMBOL>
```

比較 TTB 樂觀成交、機率成交與 queue-aware 壓力模型：

```powershell
py -3.13 monte_carlo_market_maker.py output/runs/quotes-TIMESTAMP.csv `
  --symbol <ACTIVE_SYMBOL> --trials 1000 --fill-model all `
  --output output/research/monte-carlo.json
```

只針對 TTB 虛擬競賽最大化費後總獲利：

```powershell
py -3.13 optimize_virtual_competition.py "output/runs/quotes-*.csv" `
  --symbol <ACTIVE_SYMBOL> --objective profit --initial-capital 2000000 `
  --output output/research/virtual-max-search.json
```

優化器會比較貼近最佳價的高成交頻率版本，以及固定、有限馬丁、反馬丁倉位模式；同時列出日夏普、成交日、商品類別數與已知競賽資格。

## 文件索引

- [MARKET_MAKER.md](MARKET_MAKER.md)：策略規則、撮合假設、風險限制與驗收計畫
- [DYNAMIC_MARKET_MAKING_PLAN.md](DYNAMIC_MARKET_MAKING_PLAN.md)：動態報價、三成交模型、蒙地卡羅與候選策略
- [STRATEGY.md](STRATEGY.md)：原有策略說明
- [Azure 部署指南](../../azure/README.md)：Windows VM、TTB 啟動與 24/7 監控
- [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md)：停機鎖定、全帳戶對帳、人工授權安全恢復

所有績效結論都必須使用扣除費用與稅、按日樣本外切分的實際回放或虛擬成交資料驗證；synthetic 測試只驗證程式邏輯。
