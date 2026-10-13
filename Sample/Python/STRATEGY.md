# TTB 獨立見價見量策略原型

> **歷史研究草案（非操作指令）**。此頁保留原型，部分 IOC、soft recovery 與倉位加碼描述並不是目前預設執行行為。唯一操作手冊是 [OPERATIONS.md](OPERATIONS.md)，實作策略請見 [MARKET_MAKER.md](MARKET_MAKER.md)，切勿執行本頁舊版 live 例子。

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

## TTB 虛擬送單（歷史草案，不可直接執行）

`--mode ttb` 會真的向 GUI 當前登入的虛擬帳戶送單。這個舊原型尚未納入現在的 `--max-notional`、已驗證保證金欄位、跨重啟 Journal 及人工對帳防護，因此原本的 live 範例已刪除。請以 [OPERATIONS.md](OPERATIONS.md) 作為唯一操作依據，待 #2/#4 完成後再由操作員啟用。

## 驗證

```powershell
python -m unittest -v test_strategy_core.py
python sim_hft_bot.py --help
```
