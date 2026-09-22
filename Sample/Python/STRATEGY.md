# TTB 獨立見價見量策略原型

這個原型專門針對期交所虛擬交易所的「獨立成交」見價見量模式，不應把 shadow
績效解讀成實盤可得績效。若競賽使用「機率分配成交」或逐筆撮合，必須另行調整成交模型。

## 策略邏輯

- 使用最佳買賣量不平衡、microprice 與最近成交方向建立短期分數。
- 空手時只在分數超過門檻後，將限價進場單掛在最佳買價或最佳賣價。
- 成交後取消進場邏輯，改掛距平均成本 `target_ticks` 的出場單。
- 達到停損或持倉逾時時，將出場價移到當前最佳價並使用 IOC。
- 虧損後只回收一部分回撤，且受 `max_order_qty`、`max_inventory` 與每日停損限制；不採倍數馬丁。

## 先跑 shadow 模式

先登入並開啟 `TTB.exe`，選擇確認為「獨立成交」的競賽，再於本資料夾執行：

```powershell
python sim_hft_bot.py --symbol TXFA7 --mode shadow --point-value 200 `
  --fee-per-fill 100 --tax-rate 0.00002 --target-ticks 4
```

請將 `TXFA7` 換成 TTB 當下顯示的近月代碼，並依競賽規則填入每次成交手續費。
Shadow 模式不送出委託，而是把後續真實 tick 對既有模擬掛單做見價見量成交。每次執行結果會寫入
`output/runs/ttb-shadow-*.csv`。

常用商品參數：

| 商品 | `point-value` | `tick-size` |
|---|---:|---:|
| 臺股期貨 TXF | 200 | 1 |
| 小型臺指 MTX | 50 | 1 |
| 微型臺指 TMF | 10 | 1 |

先用輸出檔比較不同 `target-ticks`、`signal-threshold`、`max-hold-seconds`。至少應觀察完整日盤與夜盤，
不要只用單一高波動時段選參數。

## 切換 TTB 虛擬送單

只有在 shadow 結果穩定、競賽帳戶與商品代碼確認正確後才執行：

```powershell
python sim_hft_bot.py --symbol TXFA7 --mode ttb --confirm-virtual-orders `
  --point-value 200 --fee-per-fill 100 --tax-rate 0.00002 --target-ticks 4
```

`--mode ttb` 會真的向目前登入的 TTB 虛擬帳戶送單。程式預設每個方向只保留一筆有效委託，
關閉時會嘗試撤銷仍由程式追蹤的委託。若 TTB 回報格式與範例不同，程式會停止該次操作並留下錯誤紀錄，
不會在無法確認訂單狀態時持續加單。

## 驗證

```powershell
python -m unittest -v test_strategy_core.py
python sim_hft_bot.py --help
```
