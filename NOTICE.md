# 第三方授權與致謝

本倉庫程式（回測引擎、指標、策略規則、資料適配器、網頁）是晏銘湛箴的原創實作，授權為 MIT。撰寫時沒有把下列專案的原始碼複製進這個倉庫。

下面兩次核對都用 GitHub commits API。2026-11-10 那張表標明當時允許依賴的對象。2026-11-11 14:13 KST 又對過一輪，數字在文末。這不是把那些專案的程式賣進本倉庫。

## 這版實際依賴

| 專案 | 位址 | 授權 | 角色 | 核對到的提交 |
| --- | --- | --- | --- | --- |
| aiogram/aiogram | https://github.com/aiogram/aiogram | MIT | Telegram 紙上訊號機器人 | `dev-3.x` `e164475d00dc`（2026-11-01T18:45:33Z） |

其餘執行期依賴是 NumPy、pandas、httpx、FastAPI、Uvicorn、Pydantic、pytest，授權為 BSD 或 MIT，與本專案的 MIT 授權相容。

紙上單不把 kraken-cli 的原始碼包進來。本機若有 `kraken`（[krakenfx/kraken-cli](https://github.com/krakenfx/kraken-cli)，MIT），紙上步驟會呼叫 `kraken paper buy`、`kraken paper sell`、`kraken paper status`。沒有這個指令時，用紙上行情與手續費、滑點自己算成交，實盤不會改走這條模擬。

## 允許依賴、但這版沒有匯入它們的原始碼

直接呼叫已核對的公開 REST／RPC，測試才不必在離線時拉整包交易所 SDK。

| 專案 | 位址 | 授權 | 核對到的提交 |
| --- | --- | --- | --- |
| ccxt/ccxt | https://github.com/ccxt/ccxt | MIT | `master` `e1579cb6bf55`（2026-11-10T06:57:28Z） |
| akfamily/akshare | https://github.com/akfamily/akshare | MIT | `main` `1aef113ff143`（2026-11-09T16:29:58Z） |
| xgboosted/pandas-ta-classic | https://github.com/xgboosted/pandas-ta-classic | MIT | `main` `ee71ae7b044d`（2026-10-31T09:32:09Z） |
| bukosabino/ta | https://github.com/bukosabino/ta | MIT | `master` `a890410710a6`（2026-03-18T13:04:55Z） |
| michaelhly/solana-py | https://github.com/michaelhly/solana-py | MIT | `master` `41846dd34886`（2026-11-10T02:51:59Z） |
| Polymarket/py-sdk | https://github.com/Polymarket/py-sdk | MIT | `main` `0661c24b0ad5`（2026-11-09T16:44:30Z） |
| jesse-ai/jesse | https://github.com/jesse-ai/jesse | MIT | `master` `9afc357a5675`（2026-11-10T09:50:14Z） |

指標公式來自公開文獻，不是從 pandas-ta 抄來的程式：

- RSI：J. Welles Wilder Jr., *New Concepts in Technical Trading Systems*（1978）
- MACD：Gerald Appel
- 布林通道：John Bollinger。標準差用母體（ddof = 0）

## 只參考、沒有依賴、也沒有複製

這些授權不能放進這個 MIT 倉庫。策略規則是依公開定義自行寫的，不是把它們的策略檔貼過來。

- freqtrade：GPL-3.0，https://github.com/freqtrade/freqtrade
- backtesting.py：AGPL-3.0，https://github.com/kernc/backtesting.py
- vectorbt、pybroker：Commons Clause
- python-telegram-bot：LGPL-3.0-only。Telegram 改走 aiogram
- nautilus_trader：LGPL-3.0。不內嵌，也不當依賴。研究用的 K 線與訊號可以匯出 CSV，在另一個環境交給 Nautilus 自己讀。
- backtrader：GPL。不採用。

Apache-2.0 的 hummingbot、QuantConnect Lean 只作概念對照。Lean 可以在另一個環境讀同一份 CSV，這個倉庫不內嵌它。

可選額外依賴（不在預設 `pip install`）：`polymarket-client`（官方 py-sdk，MIT，上游標 beta）與 `ccxt`（MIT）。沒裝時，Polymarket 退回既有 httpx，ccxt 這個來源會直接說明沒有安裝，不會改接 okx、binance 或 kraken。

## 2026-11-11 14:13 KST 再核對

同一天的取捨寫在 `docs/1111_1413_seoul_開源層級比較與取捨.md`。這裡只留和授權邊界有關的結論：AKQuant、akshare、VeighNa、Jesse、Condor、Kelows/million、CabalSpy 的 Python SDK 都是 MIT，這版都沒有把它們的原始碼複製進來，也沒有把它們加進預設依賴。freqtrade 是 GPL-3.0，backtesting.py 是 AGPL-3.0，vectorbt 是 Apache-2.0 加 Commons Clause，只作對照。Polymarket 舊的 `py-clob-client` 倉庫已封存。
