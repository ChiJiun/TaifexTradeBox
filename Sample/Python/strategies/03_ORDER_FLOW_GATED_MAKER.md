# 策略三：訂單流過濾的單邊被動做市

## 目標

保留見價見量的被動成交優勢，但在短期價格具有明顯方向性時，取消容易遭受逆選擇的一側。

例子：市場快速下跌時，最佳買價的買單很容易成交，但成交後價格可能繼續下跌。此時策略不應因為「容易成交」就繼續接多，而應先判斷成交後價格漂移是否有利。

## 特徵

### 最佳檔量不平衡

```text
I_t = (BidQty_t - AskQty_t) / (BidQty_t + AskQty_t)
```

`I_t > 0` 表示最佳買量相對較大，但單獨使用可能受掛單撤除影響。

### Microprice

```text
microprice_t =
    (Ask_t * BidQty_t + Bid_t * AskQty_t)
    / (BidQty_t + AskQty_t)
```

與中間價比較：

```text
micro_bias_ticks = (microprice_t - mid_t) / tau
```

### Level-1 Order Flow Imbalance

可用相鄰快照最佳價量變化近似：

```text
OFI_t = bid_contribution_t - ask_contribution_t
```

當買價上移或同價買量增加時，買方貢獻為正；當賣價下移或同價賣量增加時，賣方壓力增加。實作時需將 OFI 除以近期總顯示量，使不同商品可比較。

### 近期主動成交流

利用成交價相對於買賣價判斷方向：

```text
signed_volume = +last_qty  if trade near ask
                -last_qty  if trade near bid
```

在短時間窗內做指數衰減加總：

```text
TFI_t = EWMA(signed_volume)
```

## 綜合方向分數

```text
z_t = w_I * I_t
    + w_M * micro_bias_ticks_t
    + w_O * normalized_OFI_t
    + w_T * normalized_TFI_t
```

權重不能只用單一交易日最佳化。應以過去交易日估計特徵對成交後 1、2、5、10 秒價格變化的預測力，再固定權重到下一個未見交易日。

## 報價規則

| 狀態 | 入口報價 |
|---|---|
| `z_t >= theta` | 只保留買進入口，取消賣出入口 |
| `z_t <= -theta` | 只保留賣出入口，取消買進入口 |
| `abs(z_t) < theta` 且低波動 | 可保留雙邊入口 |
| 高波動、價差過大或訊號跳變 | 取消全部新入口 |

這裡的「正向只保留買進」是為了避免在快速上漲時賣在容易繼續上漲的位置；負向則對稱。

## 毒性判斷

對每次成交定義方向 `s`：多單為 `+1`、空單為 `-1`。成交後 `h` 秒漂移：

```text
drift_h = s * (mid_{t+h} - fill_price)
```

若條件平均：

```text
E[drift_h | feature bucket] < -cost_buffer
```

代表這一類成交具有毒性，即使表面上能取得一跳價差，實際上也可能是在承接資訊較強的一方。該特徵區間應停止入口或提高門檻。

## 波動狀態

計算短窗及長窗中間價波動：

```text
vol_ratio = sigma_short / sigma_long
```

候選規則：

- `vol_ratio < v_low`：盤整，可雙邊報價。
- `v_low <= vol_ratio < v_high`：只依方向分數保留單邊。
- `vol_ratio >= v_high`：停止新入口。

亦可加入：

- 最近 `N` 筆同方向價格變動比例。
- 一秒或五秒價格移動跳數。
- 買賣價差是否突然擴大。
- 累計量是否中斷、重置或行情是否重複。

## 與有限攤平的關係

攤平的判斷門檻應比首筆入口更嚴格：

```text
entry_long: z_t >= theta_entry
add_long:   z_t >= theta_add
theta_add >= theta_entry
```

若多單已虧損且 `z_t` 仍強烈為負，不得因價格到達階梯位置就自動加碼。只有方向分數回到中性或轉正，且波動狀態允許時才加碼。

## 評估方法

應同時比較：

- 未過濾版本的總成交數與費後總獲利。
- 過濾版本的總成交數與費後總獲利。
- 每次成交後漂移。
- 完整循環率。
- 最大持倉時間。
- 單日最差損益。
- 最大回撤與日夏普。

過濾後成交數下降是預期現象；只要損失尾部下降幅度大於獲利減少，就可能提高競賽夏普與可放大的安全口數。

## 失效條件

- Level-1 資料無法可靠反映真正訂單流。
- TTB 行情時間戳或快照順序不足以計算 OFI。
- 權重在同一批資料上過度最佳化。
- 訊號只在低延遲環境有效，但策略送單生效延遲過大。
- 過濾太嚴使成交日數不足或總獲利顯著下降。
