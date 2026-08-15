"""HTTPクライアント — リトライ・レート制限・生データキャッシュ.

外部APIは有償かつ不安定である。ここでの設計方針:

  1. 生レスポンスは必ずディスクへ保存する（再取得しない＝コストを二重に払わない）
  2. キャッシュヒットはネットワークに出ない（開発中の反復実行が無料になる）
  3. レート制限はクライアント側で守る（429を食らってから直すのでは遅い）
  4. 失敗は指数バックオフで再試行し、それでも駄目なら例外を上げて止める
     （壊れたデータで価格を配信するより、止まるほうが安全）
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


class SourceError(RuntimeError):
    """コネクタが復旧不能な失敗をした場合に送出する."""


class MissingCredentials(SourceError):
    """APIキー未設定."""


@dataclass
class RateLimiter:
    """トークンバケット方式のレート制限."""

    per_second: float = 2.0
    burst: int = 4
    _tokens: float = field(default=0.0, init=False)
    _last: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)
        self._last = time.monotonic()

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self._tokens = min(
                float(self.burst), self._tokens + (now - self._last) * self.per_second
            )
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            time.sleep((1.0 - self._tokens) / self.per_second)


@dataclass
class HttpClient:
    source: str
    raw_dir: Path
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)
    timeout: float = 30.0
    max_retries: int = 4
    user_agent: str = "kanoya-rm/0.1 (revenue management data collector)"
    offline: bool = False
    _calls: int = field(default=0, init=False)
    _cache_hits: int = field(default=0, init=False)

    # ---- 統計（コスト管理に使う） ----------------------------------

    @property
    def network_calls(self) -> int:
        return self._calls

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    # ---- キャッシュ -----------------------------------------------

    def _cache_path(self, key_material: str, run_date: date) -> Path:
        digest = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:24]
        return self.raw_dir / self.source / run_date.isoformat() / f"{digest}.json"

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)["response"]
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def _write_cache(self, path: Path, request_meta: dict[str, Any],
                     response: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": self.source,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request": request_meta,
            "response": response,
        }
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        tmp.replace(path)

    # ---- SSL（社内プロキシ／CAバンドルに対応） ----------------------

    def _ssl_context(self) -> ssl.SSLContext:
        for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            bundle = os.environ.get(var)
            if bundle and Path(bundle).exists():
                return ssl.create_default_context(cafile=bundle)
        return ssl.create_default_context()

    # ---- 本体 -----------------------------------------------------

    def request_json(
        self,
        url: str,
        *,
        run_date: date,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_key_extra: str = "",
        redact: tuple[str, ...] = ("api_key", "key", "X-Goog-Api-Key", "Authorization"),
    ) -> Any:
        params = params or {}
        headers = dict(headers or {})
        headers.setdefault("User-Agent", self.user_agent)
        headers.setdefault("Accept", "application/json")

        full_url = url
        if params:
            full_url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        # キャッシュキーには秘匿情報を含めない
        safe_params = {k: v for k, v in params.items() if k not in redact}
        key_material = json.dumps(
            [method, url, safe_params, body, cache_key_extra],
            sort_keys=True, ensure_ascii=False,
        )
        cache_path = self._cache_path(key_material, run_date)

        cached = self._read_cache(cache_path)
        if cached is not None:
            self._cache_hits += 1
            return cached

        if self.offline:
            raise SourceError(
                f"オフラインモードでキャッシュミス: {self.source} {url}\n"
                f"  期待したキャッシュ: {cache_path}\n"
                f"  先に実接続で取得するか、--source fixture で実行してください。"
            )

        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.rate_limiter.acquire()
            req = urllib.request.Request(full_url, data=data, headers=headers, method=method)
            try:
                self._calls += 1
                with urllib.request.urlopen(
                    req, timeout=self.timeout, context=self._ssl_context()
                ) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                safe_headers = {k: v for k, v in headers.items() if k not in redact}
                self._write_cache(
                    cache_path,
                    {"method": method, "url": url, "params": safe_params,
                     "body": body, "headers": safe_headers},
                    payload,
                )
                return payload
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                # 4xx は再試行しても直らない（429 を除く）
                if exc.code != 429 and 400 <= exc.code < 500:
                    raise SourceError(
                        f"{self.source}: HTTP {exc.code} {exc.reason}\n  {detail}"
                    ) from exc
                last_error = SourceError(f"HTTP {exc.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc

            if attempt < self.max_retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s

        raise SourceError(
            f"{self.source}: {self.max_retries}回の試行後も失敗しました: {last_error}"
        )


def env_credential(*names: str) -> str:
    """環境変数からAPIキーを取得する（コードに直書きしない）."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise MissingCredentials(
        f"APIキーが未設定です。次のいずれかを環境変数に設定してください: {', '.join(names)}\n"
        f"  例) export {names[0]}='...'\n"
        f"  キーが無い場合は --source fixture でオフライン実行できます。"
    )
