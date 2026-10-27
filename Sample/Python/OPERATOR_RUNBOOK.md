# TTB Operator Reconciliation Runbook

> 此頁只處理**事故中的人工對帳與恢復**，其餘操作步驟請以 [OPERATIONS.md](OPERATIONS.md) 作為唯一入口；不可依此事故手冊推論 live 下單已通過驗收。

本文件是發生 TTB 虛擬帳戶異常停止後的操作流程。此流程**不保證自動辨識登入競賽/帳戶**，也不會直接對帳戶發出下單、撤單或平倉指令。

## 何時必須執行

當 `output/supervisor/manual-reconciliation-required.flag` 存在、`TTB Automation` 異常退出或 bot health 為 `manual_reconciliation_required` 時，不要手動刪除旗標，也不要直接重啟 live bot。

`run_ttb_automation.ps1` 與 `preflight_ttb.ps1` 會拒絕在這個旗標存在時啟動 live 模式。唯一例外是 [OPERATIONS.md §8](OPERATIONS.md#8-競賽全自動模式opt-in) 的競賽全自動模式：帳戶設定為 bot 專用時，supervisor 先執行有每日次數上限的 `auto_recover_ttb.py`；它拒絕處理（exit 2）或用完當日次數時，旗標保留，仍依本手冊人工處理。

## 1. 在 TTB GUI 核對實際帳戶

1. 確認 TTB 已登入**正確虛擬競賽及帳戶**，不是別的模擬模式。
2. 停止 bot/supervisor，確認沒有其他自動交易程序在操作該帳戶。
3. 在 GUI 中檢查**所有商品**的未成交委託與持倉，而非僅檢查 `config.json` 中的 symbol。
4. 若有持倉、工作中委託或來源不明的單，**在 GUI 人工處理並核對回報**。不應推測訂單是由 bot 發出。
5. 特別核對斷線前後的部分成交、刪單後成交，以及日損失停機狀態。

## 2. 執行唯讀查詢

在 VM 的互動式 PowerShell（工作目錄 `C:\TTB\Sample\Python`）：

```powershell
cd C:\TTB\Sample\Python
py -3.13 reconcile_ttb.py
```

若不使用 automation config，可唯讀檢查：

```powershell
py -3.13 reconcile_ttb.py --symbol <ACTIVE_SYMBOL>
```

工具會透過 `QUERYRESTOREREPORT` 和 `QUERYRESTOREFILLREPORT` 檢查整個登入帳戶的工作中委託和持倉，另嘗試 `QUERYMARGIN`。所有查詢均為唯讀。所有工作中委託的所有權都顯示為 `unverified`：目前尚無跨 crash 的可靠 bot 訂單 ID 記錄。

輸出與稽核紀錄僅包含摘要，不會保存原始帳戶回覆。每次執行的 JSON 稽核檔在 `output/reconciliation/`，**不屬於 runtime 14 天清除範圍**。

如果 API 不成功、Data 欄位缺失、回覆內容不符合預期或仍有掛單/部位，流程會拒絕清除旗標。不要用手動刪除旗標或修改 JSON 來繞過此檢查。

## 3. 僅在人工核對完成後清除鎖定旗標

必須確認 GUI 與 API 都顯示整個帳戶空倉、零工作中委託，並確認 bot 已停止：

```powershell
py -3.13 reconcile_ttb.py --clear-flag `
  --confirm-virtual-account `
  --confirm-flat-account `
  --confirm-no-working-orders `
  --confirm-bot-stopped
```

清除模式有額外防護：

- 僅接受 `automation/config.json` 的 `mode=ttb` 且 live confirmation flags 已啟用。
- TTB API host 必須是 localhost／loopback。
- 要求四個明確人工確認旗標。
- 連續兩次取得完整的全帳戶空倉、無掛單快照。
- 若 `manual-reconciliation-required.flag` 在檢查期間被替換或修改則拒絕清除。
- **先寫入並 flush/fsync 稽核檔，再刪除本機旗標。** 無法寫入稽核時不會刪除。
- 不發出任何 `NEWORDER`、`CANCELORDER` 或其他帳戶變更。

**限制**：TTB 沒有供此工具可靠確認目前登入競賽/帳戶身分的已驗證 API，且訂單與持倉快照不是交易所層級的原子查詢。兩次查詢可以降低但不能消除檢查與清除間狀態變化的風險。因此 CLI 的四個確認是人工責任，而非程式已驗證身分的證明。

## 4. 重新驗收後啟動

```powershell
powershell -ExecutionPolicy Bypass -File automation\preflight_ttb.ps1
powershell -ExecutionPolicy Bypass -File automation\check_ttb_health.ps1
```

確認 preflight、風控紀錄與 TTB GUI 後，再以既有 supervisor 啟動。若還留有 `output/supervisor/stop.requested`，先依正常停止流程確認系統停止，不要在不了解原因時直接刪除。

## 下一步

- #2：使用 TTB 真實虛擬帳戶回覆校準欄位、部分成交與帳戶/競賽識別。
- #5：durable event journal 已實作；但僅靠事件記錄仍不能推定缺失訂單號的委託所有權。
- #4：保證金欄位必須依 #2 的去識別化實際回覆加以確認；未確認前 live 新進場一律拒絕。
- #2：可先執行 `py -3.13 ttb_readonly_capture.py` 擷取僅含欄位結構的 API 樣本，絕不自動下單。正式虛擬一口 smoke probe 須 GUI 人工驗證後執行。

目前**沒有自動撤單/平倉功能**：在缺乏可信任 ownership 證據時，保留由 GUI 人工操作更安全。
