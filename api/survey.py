"""調査エンドポイント — ブラウザの「調査」ボタンとSerpApiの間に立つ薄い中継.

なぜサーバーが要るのか:
  APIキーをHTMLに埋めると、ページを開いた全員がキーを読める。従量課金なので
  漏れれば請求がそのまま被害になる。またOTA/Googleはブラウザからの直接取得を
  CORSと対ボット防御で遮断するため、そもそも成立しない。
  よってキーを持つ側は必ずサーバーに置く。ここがその1枚である。

2つの動かし方を持つ:
  ① Vercel サーバーレス関数     POST /api/survey    （class handler）
  ② 単体のHTTPサーバー          python3 api/survey.py --serve 8000
     社内Mac・小さなVPS・手元での検証。Vercelに縛られないようにするため。

判断・検証・課金制御はすべて engine/kanoya_rm/survey_api.py にある。
ここはHTTPの入出力だけを担当し、ロジックを持たない（テストしやすくするため）。

必要な環境変数:
  SERPAPI_API_KEY            競合価格の取得キー（RM_SURVEY_SOURCE=fixture なら不要）
  RM_SURVEY_TOKEN            画面から渡す合言葉。未設定なら503で拒否（フェイルクローズ）
  RM_SURVEY_ALLOWED_ORIGINS  CORS許可オリジン（カンマ区切り）
  RM_SURVEY_SOURCE           serpapi（既定） / fixture（キー無しでの経路検証用）
  RM_SURVEY_MAX_DAYS         1回の要求で調査できる宿泊日数（既定31）
  RM_SURVEY_DAILY_MAX        1日のリクエスト上限（既定200）
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from kanoya_rm import survey_api  # noqa: E402

MAX_BODY_BYTES = 8 * 1024


def _cors_headers(origin: str | None, allowed: list[str]) -> dict[str, str]:
    """許可されたオリジンにだけ CORS を開く.

    `*` を返すと、どのサイトからでも課金される経路を叩けてしまう。
    未設定のときは CORS ヘッダを付けない（同一オリジンからのみ使える）。
    """
    headers = {
        "Vary": "Origin",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Survey-Token",
        "Access-Control-Max-Age": "600",
    }
    if origin and (origin in allowed or "*" in allowed):
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def dispatch(raw_body: bytes, *, origin: str | None = None,
             header_token: str | None = None,
             today: date | None = None) -> tuple[int, dict, dict[str, str]]:
    """本体。(ステータス, JSON, 追加ヘッダ) を返す."""
    config = survey_api.Config.from_env()
    headers = _cors_headers(origin, config.allowed_origins)

    if len(raw_body) > MAX_BODY_BYTES:
        return 413, {"error": "要求が大きすぎます。"}, headers
    try:
        body = json.loads(raw_body or b"{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return 400, {"error": "JSONとして読めませんでした。"}, headers

    # トークンはヘッダ優先。URLやログに残りにくいため
    if header_token:
        body = {**body, "token": header_token}

    try:
        return 200, survey_api.handle(body, root=ENGINE, config=config,
                                      today=today), headers
    except survey_api.SurveyDenied as denied:
        return denied.status, {"error": denied.message}, headers
    except Exception:
        # 想定外の例外はメッセージを外に出さない（キーや内部パスが混ざり得る）
        return 500, {"error": "サーバー内部でエラーが発生しました。"}, headers


class handler(BaseHTTPRequestHandler):          # noqa: N801  Vercel の規約名
    server_version = "kanoya-rm"

    def _send(self, status: int, payload: dict, headers: dict[str, str]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:               # noqa: N802
        config = survey_api.Config.from_env()
        headers = _cors_headers(self.headers.get("Origin"), config.allowed_origins)
        self.send_response(204)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()

    def do_POST(self) -> None:                  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(min(length, MAX_BODY_BYTES + 1)) if length else b"{}"
        status, payload, headers = dispatch(
            raw,
            origin=self.headers.get("Origin"),
            header_token=self.headers.get("X-Survey-Token"),
        )
        self._send(status, payload, headers)

    def do_GET(self) -> None:                   # noqa: N802
        """疎通確認用。設定状況だけを返し、価格は取りに行かない（課金しない）."""
        config = survey_api.Config.from_env()
        self._send(200, {
            "ok": True,
            "source": config.source_name,
            "tokenConfigured": bool(config.token),
            "serpapiKeyConfigured": bool(os.environ.get("SERPAPI_API_KEY")),
            "maxDays": config.max_days,
            "dailyMax": config.daily_max,
            "spentToday": survey_api.Ledger(
                config.ledger_path, config.daily_max
            ).spent_today(datetime.now(timezone.utc).astimezone().date()),
        }, _cors_headers(self.headers.get("Origin"), config.allowed_origins))

    def log_message(self, fmt: str, *args) -> None:
        # 既定の実装はクエリ文字列ごと出力する。トークンが載る経路を作らない
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def serve(port: int) -> None:
    from http.server import ThreadingHTTPServer

    class Router(handler):
        def _routed(self, method: str) -> bool:
            if self.path.split("?")[0].rstrip("/") in ("/api/survey", "/survey", ""):
                return True
            self._send(404, {"error": "見つかりません。"}, {})
            return False

        def do_POST(self) -> None:              # noqa: N802
            if self._routed("POST"):
                handler.do_POST(self)

        def do_GET(self) -> None:               # noqa: N802
            if self._routed("GET"):
                handler.do_GET(self)

    config = survey_api.Config.from_env()
    print(f"調査サーバー起動   http://127.0.0.1:{port}/api/survey")
    print(f"  データ源         {config.source_name}")
    print(f"  トークン         {'設定あり' if config.token else '未設定（503で拒否します）'}")
    print(f"  SerpApiキー      "
          f"{'設定あり' if os.environ.get('SERPAPI_API_KEY') else '未設定'}")
    print(f"  1回の上限        {config.max_days}日 ／ 1日の上限 {config.daily_max}リクエスト")
    ThreadingHTTPServer(("127.0.0.1", port), Router).serve_forever()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--serve":
        serve(int(args[1]) if len(args) > 1 else 8000)
    else:
        print(__doc__)
