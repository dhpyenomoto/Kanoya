# プライシングエンジン 参照実装

依存パッケージなし（Python 3.11+ 標準ライブラリのみ）。

## 調査条件の入力

「**いつ時点で**（本日）」「**どの宿泊日を**（対象期間）」調べるかは、
`config/survey_request.json` の1か所で指定します。全スクリプトがここを読みます。

```json
{
  "as_of": "today",
  "target": { "from": "today", "to": "+120d" }
}
```

この2つは**独立した別概念**です。「今日の時点で、11月の紅葉期だけを調べる」は正当な要求です。

| 書き方 | 意味 |
|---|---|
| `today` / `本日` | 調査基準日そのもの |
| `2026-11-01` | 絶対日付 |
| `2026-11` | 月指定。`from` に書けば**その月まるごと** |
| `+90d` `+3m` `+1y` | 基準日からの相対（d=日 w=週 m=月 y=年） |
| `-7d` | 過去方向。`as_of` に書けば1週間前の時点を再現 |

CLI引数が常に優先されるので、設定ファイルを書き換えずに一度だけ別期間も見られます。

```bash
python3 scripts/survey_report.py --from 2026-11-01 --to 2026-11-30
python3 scripts/survey_report.py --from 2026-11            # 11月まるごと
python3 scripts/survey_report.py --from +30d --days 14     # 30日後から2週間
python3 scripts/collect_rates.py --as-of -7d --plan-only   # 1週間前の時点で計画を確認
```

引数名は3スクリプトで共通（`--as-of` / `--from` / `--to` / `--days`）。
解決後の条件は各コマンドの冒頭に必ず表示されます。

**62日以下の期間を明示指定すると、階層化せず全宿泊日を取得します**
（「11月を調べたい」に週1スロットで4日しか返さないのは要求に応えていないため）。
`--full` / `--tiered` で上書きできます。

## 収集パイプライン（発見 → 収集 → ADR判断）

近隣宿泊施設の列挙、OTA掲出価格の収集、ADR判断までを一気通貫で実行します。

```bash
./scripts/run_pipeline.sh                              # APIキー不要（フィクスチャ再生）
FROM=2026-11-01 TO=2026-11-30 ./scripts/run_pipeline.sh # 期間を絞る
```

個別に実行する場合:

```bash
# ① 近隣宿泊施設の発見とコンペセット候補生成（四半期に1回）
python3 scripts/discover_compset.py --source places --radius 2500

# ② 施設属性を config/compset_overrides.json で人が確定 → 昇格
python3 scripts/discover_compset.py --source places --write

# ③ 競合レート収集（日次）。まず計画とコストを確認
python3 scripts/collect_rates.py --plan-only
python3 scripts/collect_rates.py --source serpapi

# ④ マーケットポジション調査とADR判断（日次）
python3 scripts/survey_report.py --explain 2026-11-21

# ⑤ 施設×日付のADRマトリクス（可視化）
python3 scripts/adr_matrix.py --from 2026-11-14 --to 2026-11-30 --open
```

`adr_matrix.py` は端末一覧に加えて `out/adr_matrix.csv` と
`out/adr_matrix.html`（ヒートマップ）を出力します。
行は**類似度スコア降順**、自社を最上段に固定し直下に市場中央値を置くので、
縦に読むだけで自社の市場内位置が分かります。売止（満）とデータなし（·）は別記号です。

```
                       11/14 11/15 11/16 ...      平均    類似度
                         SAT   SUN   MON
奈良春日 鹿のや                 132   118   118          122
└ エンジン推奨                 152   131   136          134
------------------------------------------------------------
市場中央値（NAR）               129   100    91          101
------------------------------------------------------------
月日亭                      141   105    91          106   1.00
ふふ奈良                     197   164   136          159   0.94
江戸三                      143   104    98          108   0.92
```

マトリクスに `·` が並ぶときは、収集時に期間を指定していません。
`collect_rates.py --from ... --to ...` で全日取得してから実行してください。

APIキーは環境変数から読みます（コードに書きません）。未設定なら `--source fixture`
で全工程がオフライン実行できます。

```bash
export GOOGLE_PLACES_API_KEY='...'
export SERPAPI_API_KEY='...'
```

運用手順・コスト管理・障害時の挙動は
[../docs/05_収集パイプライン運用手順.md](../docs/05_収集パイプライン運用手順.md)。

## プライシング単体

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

出力は `out/recommendations.csv` と `out/market_survey.csv`
（Excelでそのまま開けるBOM付きUTF-8）。

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
| `sources/http.py` | HTTPクライアント（リトライ・レート制限・生データキャッシュ・キー秘匿） |
| `sources/places.py` | Google Places API (New)。**施設発見とレビュー速度専用**（価格は取得不可） |
| `sources/serpapi_hotels.py` | SerpApi google_hotels。競合の実勢価格（主データ源） |
| `sources/dataforseo_hotels.py` | DataForSEO。代替経路・クロスチェック用 |
| `sources/fixture.py` | 記録済みレスポンスの再生（実コネクタと同じパーサを通す） |
| `discovery.py` | コンペセットの5軸スコアリングとティア分類 |
| `schedule.py` | 階層化収集スケジュール（スロット方式）とコスト見積 |
| `collect.py` | 収集オーケストレーションと施設名の名寄せ |
| `survey.py` | マーケットポジション調査・3シグナル合議によるADR判断 |
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
| `config/sources.json` | データ源、探索半径、スコア重み、収集階層、予算上限、レート制限 |
| `config/compset_overrides.json` | **人が確定した施設属性**（客室数・課金方式・食事条件・uplift）。再発見しても上書きされない |

## 本番化にあたって差し替えるもの

| 現在 | 本番 |
|---|---|
| `scripts/make_sample_data.py` の擬似データ | レートショッパー／Google Hotels API／PMS の各コネクタ |
| CSV 入出力 | BigQuery（`fact_comp_rate` / `fact_otb` / `mart_daily_recommendation`） |
| 標準出力 | Slack通知（承認キュー）＋ Looker Studio |
| なし | サイトコントローラーAPIへの配信、`log_decision` への記録 |

詳細は [../docs/03_システムアーキテクチャ.md](../docs/03_システムアーキテクチャ.md)。
