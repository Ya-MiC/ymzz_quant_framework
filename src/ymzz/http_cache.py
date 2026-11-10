"""帶出處的 HTTP 快取。即時模式失敗時不會改讀舊快取。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ymzz.errors import CacheMissError, DataSourceError
from ymzz.timeutil import now_utc, stamp_pair

_LAST_HIT: dict[str, float] = {}


def _host(url: str) -> str:
    return urlsplit(url).netloc


def cache_key(method: str, url: str, params: dict | None, body: object | None) -> str:
    payload = json.dumps(
        {"method": method, "url": url, "params": params or {}, "body": body},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DataClient:
    def __init__(
        self,
        cache_dir: str | Path,
        http: httpx.Client | None = None,
        min_interval: float = 0.25,
        sleeper=None,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.http = http or httpx.Client(
            timeout=40.0,
            follow_redirects=True,
            headers={"User-Agent": "YMZZResearch/0.1 (local quant research; keyless)"},
        )
        self.min_interval = min_interval
        self.sleeper = sleeper if sleeper is not None else time.sleep

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: object | None = None,
        headers: dict | None = None,
        mode: str = "live",
        pace: bool = True,
    ) -> dict:
        method = method.upper()
        key = cache_key(method, url, params, json_body)
        path = self._path(key)
        if mode == "cache":
            if not path.exists():
                raise CacheMissError(f"沒有這筆快取，拒絕改打別的網址：{url}")
            return json.loads(path.read_text(encoding="utf-8"))
        if mode != "live":
            raise DataSourceError("資料模式只能是 live 或 cache")
        if pace:
            host = _host(url)
            last = _LAST_HIT.get(host, 0.0)
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                self.sleeper(wait)
            _LAST_HIT[host] = time.monotonic()
        try:
            response = self.http.request(method, url, params=params, json=json_body, headers=headers)
            body = response.content
            status = response.status_code
        except httpx.HTTPError as exc:
            raise DataSourceError(f"連線失敗，沒有改走其他來源：{url}（{exc.__class__.__name__}）") from exc
        fetched = now_utc()
        record = {
            "url": url,
            "method": method,
            "params": params or {},
            "status_code": status,
            "sha256": sha256_bytes(body),
            "body": body.decode("utf-8", errors="replace"),
            **stamp_pair(fetched),
        }
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if status != 200:
            snippet = record["body"][:300].replace("\n", " ")
            raise DataSourceError(
                f"{url} 回傳 HTTP {status}，已停止。沒有改用其他交易所或假資料。回應片段：{snippet}",
                status_code=status,
                source_url=url,
                sha256=record["sha256"],
            )
        return record

    def get_json(self, url: str, params: dict | None = None, **kwargs):
        record = self.request("GET", url, params=params, **kwargs)
        return self._json_or_raise(record)

    def post_json(self, url: str, json_body: object, **kwargs):
        record = self.request("POST", url, json_body=json_body, **kwargs)
        return self._json_or_raise(record)

    @staticmethod
    def _json_or_raise(record: dict):
        try:
            payload = json.loads(record["body"])
        except json.JSONDecodeError as exc:
            raise DataSourceError(
                f"{record['url']} 的正文不是 JSON，沒有改寫成 K 線。",
                source_url=record["url"],
                sha256=record["sha256"],
            ) from exc
        payload_meta = {
            "source_url": record["url"],
            "status_code": record["status_code"],
            "sha256": record["sha256"],
            "fetched_at_utc": record["fetched_at_utc"],
            "fetched_at_kst": record["fetched_at_kst"],
            "params": record.get("params") or {},
        }
        return payload, payload_meta
