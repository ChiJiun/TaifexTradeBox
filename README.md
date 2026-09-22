# TaifexTradeBox：見價見量虛擬交易研究專案

這是一個以 TaifexTradeBox（TTB）虛擬交易資料與介面為基礎的研究專案，研究在「見價見量」成交規則下，結合 microprice、買賣盤不平衡與 order-flow signals 的高頻類 market-making 策略。

> 本專案是研究與模擬交易工具，不是獲利保證，也不是未經驗證即可連接真實市場的自動交易系統。所有績效必須扣除交易費、稅、滑價與成交限制後，使用樣本外資料驗證。

## 專案內容

```text
Sample/Python/
  strategy_core.py          策略、microprice、OFI 與模擬成交核心
  sim_hft_bot.py            shadow / TTB 執行器
  replay_market_maker.py    quote CSV 離線回放器
  test_market_maker.py      market maker 單元測試
  test_strategy_core.py     原有策略單元測試
  README.md                 Python 程式使用說明
  MARKET_MAKER.md           策略規則、假設與驗收計畫
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
py -3.13 -m pip install pyzmq requests
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

```powershell
py -3.13 sim_hft_bot.py `
  --symbol <ACTIVE_SYMBOL> `
  --mode ttb `
  --confirm-virtual-orders `
  --confirm-flat-account
```

程式中斷、斷線或 VM 重啟後，必須先回 TTB 檢查實際持倉和委託，再重新啟動。程式不能保證在網路中斷時自動平倉。

## 策略概要

- 以 best bid / ask 數量估算 microprice。
- 結合 Level-1 order-flow imbalance 與近期成交方向。
- 中性訊號時提供雙邊報價；方向訊號時限制入口方向。
- 首筆成交後只允許減倉，避免無限制馬丁格爾加碼。
- 固定口數、固定停利停損、時間出場、日損失與最大回撤鎖定。
- 行情中斷、未知委託回報或交易時段結束時停止開倉。

詳細規則與限制請閱讀 [Sample/Python/MARKET_MAKER.md](Sample/Python/MARKET_MAKER.md)。

## Azure 24/7 部署

TTB 需要 Windows GUI 與本機 API，因此建議使用單台 Windows Azure VM，而不是 Linux container 或 serverless：

- 建議 `Standard_B2as_v2`、64 GiB Standard SSD。
- RDP 只允許固定管理 IP；不要公開 `8080` 或 `51141`。
- 使用 Tailscale 做後續安全連線。
- 先 shadow，再設定 Task Scheduler 啟動 TTB 與策略。
- 建立 Azure Budget，並在非交易時段停止/釋放 VM 以降低成本。

完整命令與安全設定請閱讀 [azure/README.md](azure/README.md)。

### 目前 Azure 實例

目前已建立一台用於 TTB 研究的 Windows VM：

```text
資源群組：a112502533_rg_3957
VM：ttb-sim-01
區域：East Asia
規格：Standard_B2as_v2
系統：Windows Server 2022
固定公網 IP：20.189.93.22
RDP 來源限制：49.216.172.184/32
```

RDP 連線：

```powershell
mstsc /v:20.189.93.22
```

VM 已建立但尚未完成 TTB、Python 與策略程式安裝。登入 VM 後，依照 [azure/README.md](azure/README.md) 完成環境安裝，再先執行 shadow。管理員密碼不會儲存在 repo 或 README；登入後請立即變更密碼。

非使用期間可釋放 VM 以停止計算費用：

```powershell
az vm deallocate --resource-group a112502533_rg_3957 --name ttb-sim-01
```

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
- [原有策略說明](Sample/Python/STRATEGY.md)
- [Azure 部署說明](azure/README.md)
