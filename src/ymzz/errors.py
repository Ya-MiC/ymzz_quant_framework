"""框架裡會顯示給使用者的錯誤。訊息用繁體中文。"""


class YmzzError(Exception):
    """所有可對使用者說明的錯誤。"""

    error_code = "YMZZ"

    def __init__(self, message_zh: str, **extra):
        super().__init__(message_zh)
        self.message_zh = message_zh
        self.extra = extra


class LeverageError(YmzzError):
    error_code = "LEVERAGE"


class DataSourceError(YmzzError):
    error_code = "DATA_SOURCE"


class DataGapError(DataSourceError):
    error_code = "DATA_GAP"


class CacheMissError(DataSourceError):
    error_code = "CACHE_MISS"


class StockAppUnidentified(YmzzError):
    error_code = "STOCK_APP_UNIDENTIFIED"


class WalletError(YmzzError):
    error_code = "WALLET"


class ConfigError(YmzzError):
    error_code = "CONFIG"
