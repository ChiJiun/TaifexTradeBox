# TTB Python 策略程式

這個目錄包含 TTB 虛擬交易的研究策略、離線回放器與測試。策略目標是研究「見價見量」成交規則下的 microprice / order-flow market making；目前不宣稱已取得 alpha，也不保證獲利。

## 快速開始

需要 Windows、Python 3.13，以及已啟動並登入的 TTB。先安裝相依套件：

```powershell
py -3.13 -m pip install pyzmq requests
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

只有在確認 TTB 帳戶空倉、沒有未成交單，並人工檢查過 shadow 紀錄後，才可使用虛擬下單模式：

```powershell
py -3.13 sim_hft_bot.py `
  --symbol <ACTIVE_SYMBOL> --mode ttb `
  --confirm-virtual-orders --confirm-flat-account
```

異常退出、網路中斷或重啟後，必須先在 TTB 檢查實際掛單與部位，再重啟程式。不可把此程式視為保證斷線平倉的無人值守系統。

TTB 登入並選定競賽後，程式會透過官方 `QUOTEDATA()` 註冊 `--symbol` 的行情，再從本機 ZMQ `51141` 接收 tick。若 shadow 已顯示 `subscribed`、但 quote CSV 仍是空檔，請先在 TTB 確認該商品代碼有效、競賽/重播模式正在送行情，且行情畫面有更新；這不是下單功能失敗。

### TTB 商品代碼格式

網頁交易畫面的顯示代碼與 TTB Python API 的 `Symbol` 可能不同。TTB 隨附手冊使用 `TXF` 加上月份字母與年份尾碼：`A` 至 `L` 分別對應 1 月至 12 月。因此網頁顯示的 `TXF.2610`（2026 年 10 月）在 TTB API 應使用 `TXFJ6`，而不是常見海外期貨格式的 `TXFZ6`。一律以 TTB 的「報價設定」可成功新增的 `Symbol` 為準。

## 離線回放

`sim_hft_bot.py` 會記錄 quote CSV，可用相同策略重播：

```powershell
py -3.13 replay_market_maker.py output/runs/quotes-TIMESTAMP.csv --symbol <ACTIVE_SYMBOL>
```

## 文件索引

- [MARKET_MAKER.md](MARKET_MAKER.md)：策略規則、撮合假設、風險限制與驗收計畫
- [STRATEGY.md](STRATEGY.md)：原有策略說明
- [Azure 部署指南](../../azure/README.md)：Windows VM、TTB 啟動與 24/7 監控

所有績效結論都必須使用扣除費用與稅、按日樣本外切分的實際回放或虛擬成交資料驗證；synthetic 測試只驗證程式邏輯。
