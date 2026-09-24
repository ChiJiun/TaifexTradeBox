# Microprice market maker — research implementation

本版目標為 TTB 虛擬交易績效。沒有經過 TTB 成交樣本校準，沒有已驗證的報酬率或勝率。
之前討論的 63%、70% 完成率均為假設／研究門檻，不是量測結果。

## 規則

- 中性訊號雙邊掛單；方向訊號只留同方向入口。
- 入口報價具有黏著性：預設中心價移動 4 ticks 或掛單滿 5 秒才重報，避免遠端報價每 tick 追價而永遠無法成交。
- 公平價使用加權中間價，加上啟發式訂單流分數。這不是完整估計的 Stoikov micro-price，也不是校準的上漲機率。
- 分數含最優檔量不平衡、成交方向與 Level-1 OFI。
- 半寬至少 4 ticks，並依成本與最近 30 秒價格標準差擴大。
- 首筆成交後只允許減倉；預設從成交均價算 8 ticks 固定出口。
- 8 ticks 停損、15 秒時間出場、逆向訊號出場。
- 當期含浮損及預估平倉費用虧損 10,000 元或回撤 40,000 元，鎖定停止開倉並嘗試減倉。
- 固定 1 口；不啟用虧損加碼。CLI recovery 參數只為保留舊介面，本策略不使用。
- 即時執行以台北電腦時間週一至週五 08:55–13:20 開倉，13:35 嘗試平倉。尚無假日與到期日行事曆；延遲／重播行情請使用離線 replay，不套用即時時間限制。

## 執行

在本目錄，先登入 TTB，再以當期有效合約代號替換 PLACEHOLDER：

```powershell
py -3.13 sim_hft_bot.py --symbol PLACEHOLDER --mode shadow
py -3.13 -m unittest discover -v
py -3.13 replay_market_maker.py output/runs/quotes-TIMESTAMP.csv --symbol PLACEHOLDER
```

預設成本為 TX 每次每口 100 元、契約金額稅率 0.00002。其他競賽或商品必須覆寫成本與乘數。
raw quotes CSV 記錄每筆處理的行情，另有決策 CSV。離線 replay 使用相同策略，但不套用牆鐘交易時段。
回放結束的部位按可平倉方向報價估值，不假裝已成交；報表列出未平倉數量。

可用 `--fill-model independent|probabilistic|queue` 切換成交假設，並用
`monte_carlo_market_maker.py` 做區塊拔靴蒙地卡羅。完整設計、命令與驗收門檻見
[DYNAMIC_MARKET_MAKING_PLAN.md](DYNAMIC_MARKET_MAKING_PLAN.md)。

若研究目標只限虛擬競賽，可加 `--virtual-max` 直接在最佳價提供入口，並選擇
`fixed`、`martingale` 或 `anti_martingale` 倉位模式。使用
`optimize_virtual_competition.py` 可按費後總獲利或競賽資格／夏普排序參數。

## 撮合假設

shadow 僅在送單之後、累計量增加的事件成交；同一 tick 的量在本帳戶委託間共享。
成交價採委託限價，不提供穿價改善。首筆快照建立基線，重複快照和累計量重置不成交。
這是研究近似，沒有宣稱等同官方撮合器；尚未建模網路延遲、排隊與委買賣量觸發成交。

## TTB 整合限制

預設 shadow 不送單。TTB 模式需要 --confirm-virtual-orders 及 --confirm-flat-account。
啟動前必須人工確認帳戶空倉、無掛單，運行中帳戶專用。不能帶倉重啟。
撤單 API 接受後會等待回報確認，才允許替換掛單；未知委託回報逾時會停止。
TTB 回報欄位、部分成交價格語義仍待實際帳戶驗證，尚未完成端到端驗收。
資料中斷超過 2 秒會停止並嘗試撤單；不能在斷線時保證平倉。
Ctrl+C 只撤已辨識的掛單，不保證平倉。所有異常退出後必須在 TTB 檢查掛單與部位。
目前損失上限為程序生命週期，沒有跨重啟持久化；完整無人值守部署尚待對帳與恢復工作完成。

## 後續驗收

先取得 5–10 個交易日行情與單口虛擬成交樣本，確認 tick 與回報次序、部分成交、取消後成交。
用按日切分的樣本外測試評估費後損益、最大回撤、成交後漂移、循環完成率與持倉尾部時間。
synthetic 測試只驗證規則，不表示策略有 alpha。自動放大口數與軟式恢復需在這些資料通過後另行評估。
