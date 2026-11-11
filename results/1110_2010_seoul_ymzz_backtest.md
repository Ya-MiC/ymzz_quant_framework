# 晏銘湛箴真實資料回測紀錄

- 撰寫時間：2026-11-10T20:10:28.546263+09:00（Asia/Seoul）
- 檔名戳記：1110_2010

參數在程式裡先寫好才跑：RSI 6/12/24、均線 5/20、MACD 12/26/9、止盈 10%、止損 5%、滑點 5bp。沒有在看到報酬之後改這些數字。

## 回測

### BTC 現貨日線，RSI 三線 6/12/24，1 倍，本金 10 USDT

- 請求：`crypto_spot` / `okx` / `BTC/USDT` / `1d`
- 策略：`rsi_three` 參數 {'rsi_short': 6, 'rsi_mid': 12, 'rsi_long': 24}
- 本金 10，槓桿 1.0，工具 spot
- K 線 313 根，最後收盤 75524.4（2026-11-08T16:00:00+00:00）
- 期末權益 9.0187，報酬 -9.81%，最大回撤 19.73%
- 手續費 0.7314，資金費 0.0000，清算 0，成交 74
- 未平倉數量 0.0000。未平倉只是最後收盤的市值，不是額外成交。
- 來源 https://www.okx.com/api/v5/market/history-candles ，抓取 2026-11-10T20:10:32.051434+09:00 ，sha256 `5067228338415360`
- 剔除未收盤 1。OKX 日線以 UTC+8 0 點切開，和幣安／Kraken 的 UTC 日線不同。confirm=0 的未收盤 K 已剔除。數量後續用幣價換算，不用合約張數當持倉。

### 600519 日線，均線 5/20，1 倍，本金 20 萬元

- 請求：`ashare` / `sina` / `600519` / `1d`
- 策略：`ma_cross` 參數 {'fast': 5, 'slow': 20}
- 本金 200000，槓桿 1.0，工具 spot
- K 線 205 根，最後收盤 1439.0（2026-11-10T01:30:00+00:00）
- 期末權益 195300.8403，報酬 -2.35%，最大回撤 12.66%
- 手續費 859.1307，資金費 0.0000，清算 0，成交 13
- 未平倉數量 100.0000。未平倉只是最後收盤的市值，不是額外成交。
- 來源 https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData ，抓取 2026-11-10T20:10:33.655190+09:00 ，sha256 `ffbd80a121cf3993`
- 剔除未收盤 0。新浪日線沒有復權參數，不把這份資料稱作前復權。成交量單位是股，沒有成交額。
- 警告：未平倉以最後一根收盤做市值，帳本上仍是持倉，沒有假造成交。

### BTC 永續日線，MACD 12/26/9，2 倍，本金 10 USDT

- 請求：`crypto_perp` / `okx` / `BTC/USDT` / `1d`
- 策略：`macd` 參數 {'fast': 12, 'slow': 26, 'signal': 9}
- 本金 10，槓桿 2.0，工具 perpetual
- K 線 313 根，最後收盤 75492.9（2026-11-08T16:00:00+00:00）
- 期末權益 10.8974，報酬 8.97%，最大回撤 21.66%
- 手續費 0.3487，資金費 0.0914，清算 0，成交 44
- 未平倉數量 0.0000。未平倉只是最後收盤的市值，不是額外成交。
- 來源 https://www.okx.com/api/v5/market/history-candles ，抓取 2026-11-10T20:10:37.173826+09:00 ，sha256 `94c9e3cd5de6c8df`
- 剔除未收盤 1。OKX 日線以 UTC+8 0 點切開，和幣安／Kraken 的 UTC 日線不同。confirm=0 的未收盤 K 已剔除。數量後續用幣價換算，不用合約張數當持倉。 資金費歷史比 K 線短，較早的持倉沒有被填 0，只是沒有事件可扣。
- 資金費事件 276 筆。

### Polymarket 559651 的 Yes 價格點，RSI 14，1 倍，本金 10 USDC，手續費參數 0

- 請求：`polymarket` / `polymarket` / `559651` / `1d`
- 策略：`rsi_single` 參數 {'period': 14, 'oversold': 30, 'overbought': 70}
- 本金 10，槓桿 1.0，工具 spot
- K 線 278 根，最後收盤 0.0175（2026-11-10T11:07:18+00:00）
- 期末權益 5.8253，報酬 -41.75%，最大回撤 78.80%
- 手續費 0.0000，資金費 0.0000，清算 0，成交 84
- 未平倉數量 0.0000。未平倉只是最後收盤的市值，不是額外成交。
- 來源 https://clob.polymarket.com/prices-history ，抓取 2026-11-10T20:10:38.178172+09:00 ，sha256 `aee19f1da1495a96`
- 剔除未收盤 0。這是價格點，高=低=收，沒有日內高低。止盈止損只能看點是否穿過，不能假裝有 K 線影線。市值不是事件結算賠付。 標的是結果「Yes」的 token，不是 No。

## 來源探測

| 探測 | 結果 | 說明 |
| --- | --- | --- |
| binance_spot | 失敗 | https://api.binance.com/api/v3/klines 回傳 HTTP 451，已停止。沒有改用其他交易所或假資料。回應片段：{   "code": 0,   "msg": "Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms. Please c |
| kraken_spot | 成功 | `{'bars': 40, 'last': 75253.1, 'sha': '3d3ec47f4ba1'}` |
| eastmoney_80 | 成功 | `{'bars': 24, 'last': 1439.0, 'host': '80.push2his.eastmoney.com', 'quote': 3316078849.0}` |
| tencent_qfq | 成功 | `{'bars': 24, 'last': 1439.0, 'adjust': 'qfq'}` |
| geckoterminal_sol_usdc | 成功 | `{'bars': 70, 'last': 86.72081401359817, 'dropped': 1}` |
| polymarket_559651 | 成功 | `{'bars': 98, 'last': 0.0175, 'question': 'Xi Jinping out before 2027?', 'outcome': 'Yes'}` |
| stock_app | 失敗 | Stock App 打金狗的產品身分還沒有被指認。請提供官方網址、商店頁或 Telegram 機器人名稱。在那之前，這個適配器不會用 yfinance、A 股行情或 GMGN 來頂替，也不會產生假回測。 |
| solana_fee_payer | 成功 | `{'counts': {'transfer': 2, 'fee_only': 1}, 'errors': 0}` |
| solana_pool_inventory | 成功 | `{'counts': {'failed': 1}, 'errors': 0}` |

幣安若是 451，代表這個環境被地理限制擋住。適配器沒有改去 OKX。OKX 的回測是另行指定的來源。
Polymarket 若成功，歷史是價格點，不是 K 線高低，也不是事件結算賠付。
東財主機這次用 `80.push2his.eastmoney.com`。`92.push2his` 與裸的 `push2his` 在寫這份程式時對本環境是空回覆，所以沒有把它們寫成自動備援。
Solana 那個手續費地址只是先前一筆公開交易裡的付款人，不是聰明錢名單。池地址的庫存變化不是個人跟單績效。這次沒有把池子拿去算跟單報酬，避免把庫存變動說成某人的損益。跟單的延遲與滑點由單元測試鎖定。

## 功能矩陣

| 項目 | 狀態 |
| --- | --- |
| 六個策略與參數 | 完成 |
| 槓桿 1 / 1.5 / 2，後端拒絕更高；現貨拒絕大於 1 | 完成 |
| 永續保證金、清算、資金費、不追加 | 完成；範例走 OKX |
| 手續費、滑點、下一根開盤、同根先止損 | 完成 |
| A 股日線 | 完成（新浪回測；騰訊與東財 80 另有探測） |
| BTC 現貨與永續 | 完成（OKX）。幣安端點在，但是否通取決於網路 |
| Kraken 現貨 | 有探測，沒有當成第二套績效 |
| GeckoTerminal 池 K 線 | 見探測 |
| Polymarket Gamma + prices-history | 有一筆 RSI 回測；價格點不是 K 線。失敗就停，不編價格 |
| 公開錢包分類與跟單紙上重播 | 完成程式。這次個人地址若沒有兌換，就不編造跟單報酬 |
| Telegram aiogram 紙上訊號 | 完成骨架，權杖只讀環境變數，這次沒有連線發訊 |
| GMGN 下單 | 缺少，只留會丟出錯誤的介面 |
| Stock App 打金狗 | 缺少，刻意不代換 |
| 網頁與設定存取 | 完成 |
| 實盤 | 缺少，刻意不做 |

## 測試

```
.............................                                            [100%]
=============================== warnings summary ===============================
../home/ubuntu/.local/lib/python3.12/site-packages/fastapi/testclient.py:1
  /home/ubuntu/.local/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
29 passed, 1 warning in 0.36s
```

## 沒有做的事

沒有私鑰、沒有下單、沒有付費 API、沒有用 yfinance 冒充 Stock App、沒有把未收盤 K 線算進回測、沒有在來源失敗時悄悄換所。10 美元買不了貴州茅台一手，所以 A 股範例用 20 萬元，並在資金不足時拒絕零股。
