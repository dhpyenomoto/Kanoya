#!/usr/bin/env bash
# 発見 → 収集 → ADR判断 の一気通貫デモ（APIキー不要・オフライン）。
#
#   ./scripts/run_pipeline.sh
#
# 調査条件は config/survey_request.json で指定します。
# その場で上書きしたい場合は環境変数で渡せます:
#   AS_OF=2026-08-15 FROM=2026-11-01 TO=2026-11-30 ./scripts/run_pipeline.sh
#
# 実データで動かす場合:
#   export GOOGLE_PLACES_API_KEY='...'
#   export SERPAPI_API_KEY='...'
#   SOURCE=serpapi ./scripts/run_pipeline.sh
set -euo pipefail
cd "$(dirname "$0")/.."

SOURCE="${SOURCE:-fixture}"
AS_OF="${AS_OF:-2026-08-15}"
PLACE_SOURCE=$([ "$SOURCE" = "fixture" ] && echo fixture || echo places)

# 対象期間は指定があれば渡す。無指定なら survey_request.json の設定に従う。
WINDOW=()
[ -n "${FROM:-}" ] && WINDOW+=(--from "$FROM")
[ -n "${TO:-}" ]   && WINDOW+=(--to "$TO")

echo "▶ 0/4 フィクスチャ生成"
if [ "$SOURCE" = "fixture" ]; then
  python3 scripts/make_fixtures.py
fi

echo
echo "▶ 1/4 近隣宿泊施設の発見とコンペセット候補の生成"
python3 scripts/discover_compset.py --source "$PLACE_SOURCE" --as-of "$AS_OF"

echo
echo "▶ 2/4 競合レートの収集（階層化スケジュール）"
rm -f data/comp_rates_collected.csv
# 実運用では日次1回。ここでは階層化スケジュールの累積効果を見るため
# 直近5回分の実行を再現する。
for offset in 7 5 3 1 0; do
  d=$(python3 -c "import datetime;print(datetime.date.fromisoformat('$AS_OF')-datetime.timedelta(days=$offset))")
  python3 scripts/collect_rates.py --source "$SOURCE" --as-of "$d" "${WINDOW[@]+"${WINDOW[@]}"}" | tail -3
done

echo
echo "▶ 3/4 マーケットポジション調査とADR判断"
python3 scripts/survey_report.py --as-of "$AS_OF" "${WINDOW[@]+"${WINDOW[@]}"}" --explain 2026-11-21

echo
echo "▶ 4/4 回帰テスト"
python3 -m unittest discover -s tests 2>&1 | tail -3
