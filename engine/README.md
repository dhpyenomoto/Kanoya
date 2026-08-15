# プライシングエンジン 参照実装

依存パッケージなし（Python 3.11+ 標準ライブラリのみ）。

## 使い方

```bash
# 1. 検証用データ生成（本番では レートショッパー / PMS コネクタの出力に置換）
python3 scripts/make_sample_data.py

# 2. 基準価格の校正レポート（四半期に1回）
python3 scripts/calibrate_base.py --position 1.15 --revpar 62000 --occ 0.72

# 3. 推奨価格の算出（日次）
python3 -m kanoya_rm.cli --days 120 --explain 2026-11-21

# 4. 回帰テスト（CIゲート条件）
python3 -m unittest discover -s tests -v
```

出力は `out/recommendations.csv`（Excelでそのまま開けるBOM付きUTF-8）。

## 価格算定式

```
log P = log(P_base) + b_pace·z_pace + b_comp·z_comp
                    + b_event·z_event + b_lead·z_lead + b_remain·z_remain
```

| 項 | 内容 | z の定義 |
|---|---|---|
| P_base | シーズン × 曜日 × 連休前夜 | — |
| z_pace | 予約進捗 | (実OTB − 期待OTB) ÷ 1.5室 |
| z_comp | 競合ポジション | log(競合NAR中央値 ÷ 基準価格) ÷ 0.30 ＋ 0.5×市場逼迫度 |
| z_event | 需要イベント | 登録スコアと自動検知スコアの最大値 |
| z_lead | リードタイム | 区分別固定補正 ÷ b_lead |
| z_remain | 残室希少性 | (2×販売済率 − 1) × リードタイム減衰 |

各項の対数寄与は ±0.28 にクリップ。

## ガードレール適用順序

**モデル出力 → 日次変動幅(±15%) → 丸め(1,000円) → 絶対境界(フロア/天井)**

絶対境界を最後に効かせることで、他のルールがフロア・天井を越えられないようにしています
（順序を逆にすると変動幅の緩和が天井を突破します。テストで担保）。

判定：`±12%以内 → AUTO_APPLY` / `±12〜35% → APPROVAL_REQUIRED` / `±35%超 → REJECTED_ANOMALY`

## モジュール

| ファイル | 役割 |
|---|---|
| `config.py` | 設定読込、シーズン／イベント／祝日の解決、日カテゴリ算定 |
| `normalize.py` | 競合価格 → NAR（1室2名1泊2食・税サ込）正規化 |
| `compset.py` | 重み付き中央値、市場逼迫度、**イベント自動検知** |
| `pace.py` | 日カテゴリ別ブッキングカーブに対する進捗評価 |
| `calibrate.py` | **基準価格の自動校正**（市場整合 × 予算整合） |
| `pricing.py` | 対数加法モデル、ウォーターフォール分解、ガードレール |
| `restrictions.py` | MLOS、gap night 検知 |
| `channels.py` | チャネル別 Net ADR、直販シフト効果試算 |
| `report.py` | CSV／説明文／サマリ出力 |
| `cli.py` | 日次実行エントリポイント |

## 設定ファイル

エンジン本体は**施設非依存**です。以下3ファイルの差し替えのみで別施設に適用できます。

| ファイル | 内容 |
|---|---|
| `config/property.json` | 施設情報、基準価格、ガードレール、係数、リードタイム曲線、チャネル手数料 |
| `config/compset.json` | コンペセット（ティア・重み・課金方式・食事uplift） |
| `config/calendar.json` | シーズン区分、イベント、祝日、ペースベンチマーク |

## 本番化にあたって差し替えるもの

| 現在 | 本番 |
|---|---|
| `scripts/make_sample_data.py` の擬似データ | レートショッパー／Google Hotels API／PMS の各コネクタ |
| CSV 入出力 | BigQuery（`fact_comp_rate` / `fact_otb` / `mart_daily_recommendation`） |
| 標準出力 | Slack通知（承認キュー）＋ Looker Studio |
| なし | サイトコントローラーAPIへの配信、`log_decision` への記録 |

詳細は [../docs/03_システムアーキテクチャ.md](../docs/03_システムアーキテクチャ.md)。
