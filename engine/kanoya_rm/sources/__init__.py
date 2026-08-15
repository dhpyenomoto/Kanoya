"""外部データソース・コネクタ.

  places            Google Places API (New) — 近隣宿泊施設の発見・評判データ
  serpapi_hotels    SerpApi google_hotels エンジン — 競合の実勢価格
  dataforseo_hotels DataForSEO Business Data — 競合の実勢価格（代替経路）
  fixture           記録済みレスポンスの再生（APIキー不要・オフライン実行）

すべてのコネクタは HttpClient を経由し、生レスポンスを
data/raw/{source}/{取得日}/{key}.json へ不変保存する。
正規化・集計は必ず下流で行い、生データには手を入れない。
"""

from .base import RateSource, PlaceSource  # noqa: F401
