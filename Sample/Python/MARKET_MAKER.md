# Microprice Market Maker：已實作策略說明

> **操作與停機唯一權威文件**：[OPERATIONS.md](OPERATIONS.md)。本頁只描述內建 `market-maker` 策略邏輯；[DYNAMIC_MARKET_MAKING_PLAN.md](DYNAMIC_MARKET_MAKING_PLAN.md) 是研究方案，不能把它視為完整實作或已通過 broker 驗收。

## 已實作與目前預設

策略使用 microprice、Level-1 訂單流與最近成交方向建立啟發式訊號，並非校準的上漲機率。

- 中性時可保留雙邊限價入口；訊號偏向一側時只保留該側。入口有 sticky quote，預設中心價移動 **4 ticks** 或報價滿 **5 秒**才調整。
- 半寬預設 **4 ticks**，另考慮交易成本與最近波動；行情需先累積預設 **30 筆**暖機資料。
- 成交後優先退出，目標 **8 ticks**、停損 **8 ticks**、持倉時間上限 **15 秒**；逆向微結構訊號亦可能觸發退出。
- 預設固定每單 **1 口**、`max_inventory=1`、`sizing_mode=fixed`。`martingale`／`anti_martingale` 僅屬明確啟用的研究配置，不能當作預設。legacy `recovery_fraction` 預設為 0。
- 日損失限額預設 **10,000 NTD**、回撤限額 **40,000 NTD**。同一風控交易日的 snapshot 寫入 `risk-state.json`，成交、委託及風控事件寫入 Durable Journal；損毀／矛盾即 fail-closed。
- TXF／MTX／TMF 契約乘數自動用 **200／50／10 元／點**，但手續費（CLI 預設 100 元／單口成交）、稅率、滑價必須另行依商品／競賽規則驗證，不得把預設值當成官方規格。

策略可能依目前持倉或 stop signal 停止新進場；**一切委託仍受 `TTBOrderGateway` 的額外保證金、庫存、名目曝險、限速與可證實 ownership 保護**。真實 `QUERYMARGIN` 尚待 #2／#4 實測核對；未配置可信欄位時 live 入口會被拒絕。

## 時段、行情中斷與關閉

- CLI 時段預設：日盤 **08:45–13:45**、夜盤 **15:00–05:00**。休市及到期日另受當年度人工核准日曆控管；到期日**13:30** 收盤且該合約無下午夜盤，換月**不得自動帶倉**。
- 有效行情超過 **5 秒**未更新會進入 `stale_market_data`，停止新增入口、處理已知掛單；連續 **120 秒**中斷後 fail-closed 退出。初次啟動有寬限時間，恢復需預設 **3 筆**新鮮報價。實際 CLI 參數可覆寫，詳見 [OPERATIONS.md](OPERATIONS.md)。
- Ctrl+C、SIGTERM 或 Supervisor 停止旗標，會進入既有關機流程。**預設不保證自動平倉**；只在開啟 `--flatten-on-shutdown` 且條件允許時嘗試 IOC 退出。API 失敗或部分成交都需 GUI 人工確認，絕非保證斷線平倉。
- `/healthz` 是存活、`/readyz` 僅 `healthy` 回報 ready、`/status` 提供詳細報告。

## 執行（先 Shadow）

```powershell
cd C:\TTB\Sample\Python
py -3.13 sim_hft_bot.py --symbol <CURRENT_VERIFIED_SYMBOL> --mode shadow
py -3.13 -m unittest discover -v
```

Shadow 必須有 TTB GUI 行情來源，但**不會真的對虛擬帳戶下單**。不要在未確認商品、帳戶和風控時直接切到 `mode=ttb`。

## 離線研究（與 live 分離）

- `replay_market_maker.py`：用後續 tick 模擬限價單成交，提供 `independent`、`probabilistic`、`queue` 模型，且報告未平倉估值、成本與 tail exposure。模型都不等同已校準 TTB 逐筆撮合。
- `monte_carlo_market_maker.py`：block bootstrap 模擬不確定性。
- `walk_forward_oos.py`：嚴格以日期切分、選參數後不碰 final Holdout、執行敏感度與驗收閘門，產出 JSON／Markdown。
- [strategies/](strategies/) 五份策略文件以**候選設計為主**，不表示五種均有獨立可執行模組。要宣稱可用必須提供實際程式碼與測試證據。

本研究仍欠缺 TTB 真實虛擬帳戶成交、撤單競態與保證金欄位的對帳樣本（#2/#4），因此不得宣稱已實現正 alpha 或可 24/7 無人值守。
