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
python3 scripts/discover_compset.py --source places --radius 5000

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

`adr_matrix.py` は端末一覧に加えて `out/adr_matrix.csv`、`out/adr_matrix.md`、
`out/adr_matrix.html` を出力します。

**HTML版は、ページ上で調べたい日程を変えられます。** 収集済みの全期間のデータを
ページ内に埋め込んであるため、日付を変えるたびに Python を再実行したり
HTMLを書き換えたりする必要はありません。

- 開始日 / 終了日の日付入力（収集済みの範囲外は選べません）
- **「この期間で表示」ボタン** — 押せば必ず反映されます。日付ピッカーの
  change がいつ飛ぶかは環境差が大きく（iOSでは閉じるまで来ない）、
  自動更新だけだと「入れたのに何も起きない」状態になるため
- 「月でまとめて選ぶ」セレクタ
- クイック選択（基準日から7日 / 14日 / 30日 / 90日 / 全期間）
- 選択に応じて、対象日数・自社平均・市場中央値・対中央値・売止セル数を再計算

### 自社の価格設定（内訳）

同じページで **宿泊単価／人・夕食単価／人・朝食単価／人**（税サ込）を入力できます。

マトリクスの単位は「1室2名1泊2食・税サ込」の総額ですが、OTAに実際に打ち込むのは
1名単価です。内訳を持たせると、この2つを機械的に行き来できます。

| 入力すると | 表に出るもの |
|---|---|
| 合計 × 2名 | `└ 設定価格（入力）` 行。全日フラットなので、動く市場を横切る様子が読める |
| 推奨総額 − 食事 × 2名 | `└ 推奨 宿泊単価／人` 行。**OTAプランにそのまま入れる数字** |

**夕食・朝食は原価にほぼ固定されるため、値付けで動かせるのは宿泊単価だけです。**
鹿のやの設定では食事が総額の48%を占めるので、総額を+10%動かすには
宿泊単価を約+19%動かす必要があります。この増幅率が「宿泊単価 設定→推奨」に出ます。

貢献利益フロア（`guardrails.floor_room_rate`）を下回る設定、上限を超える設定、
食事代が総額を食い尽くす設定には、その場で警告が出ます。

初期値は `config/property.json` の `rate_components` から読みます。
**合計 × `standard_occupancy` が `base.anchor_room_rate` と一致するように保ってください**
（ずれると自社行と推奨価格が別基準になります。テストで担保）。
画面での入力は端末に保存され、次に開いたときも残ります。

### 「この期間を調査」ボタン（オンデマンド収集）

画面から Google Hotels の実勢価格を取りに行き、その場で表を更新します。
取得したセルは緑の枠で囲まれます。

```bash
# 手元で経路確認（APIキー不要）
RM_SURVEY_SOURCE=fixture RM_SURVEY_TOKEN=test-token-123 \
RM_SURVEY_ALLOWED_ORIGINS=http://localhost:8899 \
  python3 ../api/survey.py --serve 8000

python3 scripts/adr_matrix.py --survey-endpoint http://127.0.0.1:8000/api/survey
```

APIキーは**ページには入りません**。`api/survey.py`（調査サーバー）が持ちます。
ページに埋めると開いた全員が読めるうえ、Google／OTA はブラウザからの直接取得を
遮断するため、そもそも成立しません。

**押すたびに課金されます**（1宿泊日＝1リクエスト＝約2.3円）。
そのため認証と上限は動作の前提条件として組み込んであります。

| 段 | 環境変数 | 既定 |
|---|---|---|
| トークン照合 | `RM_SURVEY_TOKEN` | **未設定なら503で全拒否**（フェイルクローズ） |
| 期間の上限 | `RM_SURVEY_MAX_DAYS` | 31日 |
| 日次の上限 | `RM_SURVEY_DAILY_MAX` | 200リクエスト |
| CORS | `RM_SURVEY_ALLOWED_ORIGINS` | 未設定なら同一オリジンのみ |

更新されるのは**競合価格・売止・市場中央値**です。エンジン推奨は予約進捗（OTB）を
使うため、夜間バッチが算出したままになります。

`sources.json` の `survey_api.endpoint` が空のときは、ボタンは**無効の状態で表示され**、
何を設定すれば有効になるかが画面に出ます（黙って消えるより運用しやすいため）。

設置手順は [../docs/06_調査サーバー構築手順.md](../docs/06_調査サーバー構築手順.md)。

---

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
python3 scripts/make_fixtures.py

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
log P = log(P_base) + b_demand·z_demand + b_comp·z_comp
                    + b_event·z_event + b_lead·z_lead
```

| 項 | 内容 | z の定義 |
|---|---|---|
| P_base | シーズン × 曜日 × 連休前夜 | — |
| z_demand | 内部需要（予約ペース×残室希少性） | (実OTB − 期待OTB) ÷ 飽和点 × リードタイム減衰 |
| z_comp | 競合ポジション | log(競合NAR中央値 ÷ 基準価格) ÷ 0.30 ＋ 0.5×市場逼迫度 |
| z_event | 需要イベント | 登録スコアと自動検知スコアの最大値 |
| z_lead | リードタイム | 区分別固定補正 ÷ b_lead |

各項の対数寄与は ±0.28 にクリップ。

**内部需要は1項にまとめてあります。** 以前は「予約ペース(0.42)」と
「残室希少性(0.34)」を別項に持っていましたが、どちらも OTB室数の線形関数で
符号も同じ、つまり同一の変数に係数が二重にかかっていました。実効重み0.76 は
競合ポジション(0.32)の2倍以上で、5室では予約1件でモデル出力が3倍動いていました
（実測 2.74〜3.06倍 → 統合後 1.49〜1.75倍）。

## ガードレール適用順序

**モデル出力 → 日次変動幅(±15%) → 丸め(1,000円) → 絶対境界(フロア/天井)**

絶対境界を最後に効かせることで、他のルールがフロア・天井を越えられないようにしています
（順序を逆にすると変動幅の緩和が天井を突破します。テストで担保）。

判定：`±12%以内 → AUTO_APPLY` / `±12〜35% → APPROVAL_REQUIRED` / `±35%超 → REJECTED_ANOMALY`

## 感度分析（係数設計の検証）

係数を変えたときに推奨価格がどう動くかを見る**読み取り専用の診断ツール**です。
これが無いとキャリブレーションは「たぶんこのくらい」の議論にしかなりません。

```bash
python3 scripts/sensitivity.py --sweep otb   --date 2026-11-21   # OTB 0室〜満室
python3 scripts/sensitivity.py --sweep uplift --date 2026-11-21  # 食事uplift 0.5〜2.0倍
python3 scripts/sensitivity.py --sweep lead  --date 2026-11-21   # リードタイム 0〜120日
python3 scripts/sensitivity.py --sweep coef=b_demand --date 2026-11-21
python3 scripts/sensitivity.py --sweep otb --days 30             # 複数日サマリ
python3 scripts/sensitivity.py --sweep otb --date 2026-11-21 --csv ../out/sens.csv
```

「基準価格 / 各項のz / モデル出力 / ガードレール適用後の推奨 / 判定」を1行ずつ並べ、
最後に **「N/M 行でガードレールがモデル出力を上書き」** を集計します。

上書きの判定には `guardrail_notes` を使います。丸め（1,000円単位）による差は
設計どおりの挙動であってガードレールの上書きではないためです。

**エンジン本体は変更しません。** 設定を複製して差し替え、公開されている
`build_context` / `recommend` を呼ぶだけです。診断のために本番経路へ分岐を足すと、
その分岐自体が次の不具合になります。

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
| `pace.py` | 内部需要シグナル（進捗評価 × リードタイム減衰） |
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
| `config/compset.json` | **人が承認した確定コンペセット**。`discover_compset.py --write` で昇格する |
| `config/calendar.json` | シーズン区分、イベント、祝日、ペースベンチマーク |
| `config/sources.json` | データ源、探索半径、スコア重み、収集階層、予算上限、レート制限 |
| `config/compset_overrides.json` | **人が確定した施設属性**（客室数・課金方式・食事条件・uplift）。再発見しても上書きされない |

## 本番化にあたって差し替えるもの

| 現在 | 本番 |
|---|---|
| `scripts/make_fixtures.py` の擬似データ | レートショッパー／Google Hotels API／PMS の各コネクタ |
| CSV 入出力 | BigQuery（`fact_comp_rate` / `fact_otb` / `mart_daily_recommendation`） |
| 標準出力 | Slack通知（承認キュー）＋ Looker Studio |
| なし | サイトコントローラーAPIへの配信、`log_decision` への記録 |

詳細は [../docs/03_システムアーキテクチャ.md](../docs/03_システムアーキテクチャ.md)。
