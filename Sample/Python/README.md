# TTB Python 策略程式

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

本機監控端點預設綁定 `127.0.0.1:8765`：`/healthz` 表示程序存活、`/readyz` 在需要人工對帳時回 503、`/status` 回傳完整 health snapshot。可用 `--status-port 0` 停用。

也可以用同一個行情、委託和帳本模組執行範例均線策略：

```powershell
py -3.13 sim_hft_bot.py --symbol <ACTIVE_SYMBOL> --mode shadow `
  --strategy moving-average --short-window 5 --long-window 20 --entry-ticks 0.5
```

只有在確認 TTB 帳戶空倉、沒有未成交單，並人工檢查過 shadow 紀錄後，才可使用虛擬下單模式：

```powershell
py -3.13 sim_hft_bot.py `
  --symbol <ACTIVE_SYMBOL> --mode ttb --strategy moving-average `
  --confirm-virtual-orders --confirm-flat-account
```

異常退出、網路中斷或重啟後，必須先在 TTB 檢查實際掛單與部位，再重啟程式。不可把此程式視為保證斷線平倉的無人值守系統。

**委託所有權防護（#11）**：TTB gateway 會拒絕執行期間新出現、且沒有被 bot 以委託編號識別的 working/filled order（包含手動委託）。即使該委託的方向、價格和數量剛好符合 pending intent，也**不能**據此當成 bot 自己的單。若 `NEWORDER` 成功但沒有回傳可驗證的委託編號，bot 同樣會停機，將 pending 狀態交由人工確認。此限制可能使目前的 TTB API 無法直接無人值守下單；須完成真實回覆格式驗證後再設計安全的 ID correlation，不應直接移除防護。錯誤時健康狀態會包含 `active_orders.unknown_orders` 和 `active_orders.unsafe_reason`。

## 虛擬帳戶委託／撤單 smoke test

在策略 live 模式之前先執行：

```powershell
py -3.13 ttb_order_probe.py `
  --symbol TMFJ6 `
  --side buy `
  --distance-ticks 2000 `
  --confirm-virtual-order-probe `
  --confirm-flat-account
```

流程如下：

1. 查詢現有委託；若發現相同商品仍有 working order，拒絕測試。
2. 等待 `TMFJ6` 最新行情。
3. 以最佳買價下方 2,000 ticks 的 1 口限價送出 `NEWORDER`。
4. 從 API 回覆或委託回報取得委託編號。
5. 立即呼叫 `CANCELORDER`，輪詢至終態，再查詢成交回報。
6. 將結果寫到 `output/order-probes/order-probe-*.json`。

遠價不等於絕對不會成交。若輸出中的 `unexpected_fill_quantity` 大於零，程式會以非零狀態碼結束，必須人工核對／平倉，不能繼續啟動策略。

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
  "extraArgs": ["--base-qty", "1", "--max-order-qty", "1", "--max-inventory", "1"]
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

所有績效結論都必須使用扣除費用與稅、按日樣本外切分的實際回放或虛擬成交資料驗證；synthetic 測試只驗證程式邏輯。
