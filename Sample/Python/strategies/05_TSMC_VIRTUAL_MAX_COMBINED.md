# 策略五：台積電期貨 Virtual-Max 組合策略

## 目標

把黏著一跳做市、訂單流過濾、有限成本攤平與競賽每日損益塑形整合成單一可執行狀態機，在滿足競賽夏普與資格限制後最大化總獲利。

本策略只針對見價見量虛擬競賽，不宣稱可移植到真實市場。

## 策略優先順序

1. 帳務、委託與部位正確性。
2. 強制平倉與每日損失限制。
3. 訂單流及波動過濾。
4. 一跳被動循環。
5. 有限攤平。
6. 隨權益放大口數。

低優先級條件不得覆蓋高優先級風控。

## 交易時段

候選時段：

- 08:45 至 08:50：只觀察或使用縮小口數，建立當日波動基線。
- 08:50 至 13:30：允許正常入口。
- 13:30 後：禁止啟動新的攤平循環。
- 13:35 後：禁止新入口，只處理既有部位。
- 13:42 後：使用可成交方向積極減倉。

時間門檻需依商品最後交易日、競賽日盤規則及實際成交延遲校準。

## 即時資料

每個行情事件至少需要：

- 商品代碼。
- 行情時間與本機接收時間。
- 最佳買價、買量、賣價、賣量。
- 最新成交價、單量及累計量。
- 委託回報、成交回報與有效剩餘量。
- 帳戶權益、可用餘額及未平倉部位。

累計量未增加、行情重複、時間倒退或商品代碼不一致時，不可觸發新的模擬成交判斷。

## 狀態

### `WARMUP`

- 收集短長波動、量不平衡、OFI 與成交流。
- 不送入口單。
- 確認帳戶空倉且無未成交單。

### `FLAT_QUOTING`

- 計算方向分數 `z_t`。
- 中性低波動：最佳買賣價雙邊黏著報價。
- 正向：只留買進入口。
- 負向：只留賣出入口。
- 每方向最多一張入口單。

### `LEVEL_0_POSITION`

- 首筆成交後取消其他入口。
- 依真實成交量及成交價建立平均成本。
- 計算費後退出價並掛出減倉單。
- 若很快成交，完成循環並回到 `FLAT_QUOTING`。

### `LEVEL_1_POSITION`、`LEVEL_2_POSITION`

- 價格達到階梯門檻時，先檢查波動及 `z_t`。
- 只有毒性消退時才允許加碼。
- 以成本公式計算最小口數。
- 若所需口數超出庫存、保證金或單循環損失限制，禁止加碼並進入風險退出。
- 每次部分成交都重新計算均價與出口，不假設整張單完全成交。

### `RISK_EXIT`

觸發條件包括：

- 最終停損。
- 持倉逾時。
- 訂單流持續強烈逆向。
- 短期波動超標。
- 日損失或最大回撤達標。
- 接近停止交易時間。
- TTB 回報、部位或連線狀態不一致。

此狀態只能減倉，直到空手。

### `DAILY_HALTED`

- 取消所有入口。
- 若有部位，先完成退出。
- 停止狀態寫入持久化檔案，程式重新啟動不得自動解除。

## 核心決策流程

```text
on_quote(q):
    validate_quote_sequence(q)
    reconcile_orders_and_positions()
    update_features(q)
    update_marked_equity(q)

    if account_or_connection_inconsistent:
        cancel_entries()
        enter_risk_exit()

    if daily_profit_target_reached:
        cancel_entries()
        flatten_if_needed()
        latch_daily_halt()

    if daily_loss_or_drawdown_reached:
        cancel_entries()
        flatten_if_needed()
        latch_daily_halt()

    if position == 0:
        if session_allows_entry and regime_is_safe:
            maintain_sticky_quotes(flow_score)
    else:
        cancel_all_entry_orders()
        recompute_average_cost_and_exit()
        if exit_condition_reached:
            maintain_reduce_order()
        elif next_ladder_level_reached and add_is_safe:
            submit_cost_adjusted_add()
        elif stop_timeout_or_toxic:
            enter_risk_exit()
```

## 口數與保證金限制

基礎口數：

```text
q_base = floor(k_margin * equity / margin_per_contract)
```

再取下列限制的最小值：

```text
q_allowed = min(
    q_base,
    max_order_qty,
    max_inventory_from_cycle_loss,
    max_inventory_from_margin,
    max_inventory_from_observed_fill_volume
)
```

其中：

```text
max_inventory_from_cycle_loss
```

必須由階梯全部成交後到最終停損的費後損失反推，不能只看原始保證金允許幾口。

## 每日目標

候選風控值應用權益比例表示：

```text
daily_profit_target = h * previous_equity
daily_loss_limit    = d * previous_equity
max_daily_drawdown  = m * previous_equity
cycle_loss_budget   = c * previous_equity
```

初期只掃描少量、可解釋的 `h、d、m、c` 組合。選擇鄰近參數也維持正報酬的平臺，不選單一歷史最高點。

## 回測目標函數

由於不知道其他參賽者的第 30 名夏普，參數搜尋應分兩步：

```text
eligible(theta) =
    total_pnl(theta) > 0
    and active_days(theta) >= 28
    and product_categories >= 6
    and daily_sharpe(theta) >= internal_sharpe_buffer
```

再從 `eligible(theta)` 中最大化：

```text
total_net_pnl(theta)
```

不要直接把夏普由高到低排序後再看獲利，否則可能選到過度保守、無法競爭總獲利的參數。

## 必要測試

- 同價觸及、穿價、部分成交及累計量重置。
- 一側成交後，另一側入口確實取消。
- 入口與出口不會同時造成意外反手。
- 4、2、5 與成本調整口數在不同價格下的目標價正確。
- 所需最後一層口數超標時拒絕加碼。
- 停損、逾時、波動與毒性訊號能鎖定退出。
- 每日停機跨程式重啟仍然有效。
- 13:30、13:35、13:42 等時段切換。
- 斷線重連後，以 TTB 部位和有效委託為唯一真相來源。

## 驗收門檻

- 至少 5 至 10 個完整交易日的 TTB 真實虛擬成交樣本。
- 實際成交與本地 independent 模型差異可解釋。
- 至少 100 個完整費後循環。
- 按日 walk-forward 留存測試總損益為正。
- 不是由單一日或少數極端循環貢獻大部分獲利。
- 最差日、最大回撤與全部階梯成交損失皆低於事前預算。
- 完成六項商品及成交日資格的獨立檢查。

## 不應使用的行為

- 無上限馬丁格爾。
- 虧損後跨循環加倍。
- 大量無意成交掛單。
- 每個 tick 無差別撤掛。
- 未收到成交回報就自行認定成交。
- 為補資格而在不熟悉商品重壓。
- 將模擬競賽績效描述為真實市場可複製績效。
