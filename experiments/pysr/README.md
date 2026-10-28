# project-meme × PySR 実験

project-meme の記録から、PySR（記号回帰）で「読める数式＝仮説の候補」を発見する実験スクリプトです。通常の機械学習がブラックボックスな予測器を返すのに対し、PySR はデータに合う数式そのものを探索します。project-meme の「観察 → 仮説 → 根拠で検証」という設計と相性が良い、という発想の検証用です。

本体アプリには入れず、分析専用の独立スクリプトにしています（PySR は Julia ランタイムを含むためイメージが重く、API コンテナに入れるべきではないため）。

## セットアップ

```bash
cd project-meme-pysr
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

初回実行時に Julia 本体と SymbolicRegression.jl が自動で入り、プリコンパイルに数分かかります。2回目以降は速いです。

## まずデモで動きを見る

```bash
.venv/bin/python pysr_experiment.py --demo
```

正解のルールを仕込んだ合成データ（60日分・1時間単位）で実行します：

```
y = 1.2 + 0.9 * sin(2*pi*(hour - 6)/24) + 3.0 / (1 + hours_since_food) + 0.3 * is_weekend + ノイズ
```

PySR がこれに近い式を再発見できれば成功です。出力の `equations_` 一覧が「単純な式 → 複雑な式」の順に並び、`selected equation` が自動選択された式、`R^2` が当てはまりの良さです。

## 実データで使う

project-meme 本体のエクスポート機能を使います。

1. アプリからエクスポート ZIP を取得し、`records.json` を取り出す
2. 実行する

```bash
# 活動パターン（1時間あたりの観察数）を説明する式を探す
.venv/bin/python pysr_experiment.py --records records.json --target activity

# 嘔吐（日単位の回数）を説明する式を探す
.venv/bin/python pysr_experiment.py --records records.json --target vomit
```

健康 CSV エクスポートからでも嘔吐ターゲットは実行できます。

```bash
.venv/bin/python pysr_experiment.py --csv health.csv --target vomit
```

### 特徴量の設計メモ

- `activity`：時刻、前回の食事イベントからの経過時間、その時間の食事/水分/トイレ回数、週末フラグ → 1時間あたりの観察数（活動・鳴き声の代理指標）
- `vomit`：その日の食事/水分/トイレ回数、食事間隔の平均 → その日の嘔吐回数
- 嘔吐レコードの `meal_interval_minutes` は「嘔吐時点」で記録される値なので、説明変数に入れると答えのリークになります。意図的に除外しています
- サンプル行（`is_sample`）はデフォルトで除外。含めたい場合は `--include-sample`
- 食欲（appetite）・元気さ（energy）は文字列カテゴリのため、順序尺度へのエンコード方針が決まるまで v1 では未使用

### オプション

- `--niterations N`：探索の反復回数（デフォルト 60。実データでは 100〜300 に上げると式の質が上がる代わりに時間がかかる）
- `--seed N`：乱数シード（再現性のため固定可能）

## 結果の読み方と次のステップ

出てきた式は「確定した法則」ではなく「仮説の候補」です。おすすめの流れ：

1. 単純で loss の小さい式を1つ選ぶ
2. project-meme の辞書に仮説として登録する
3. 以後の観察を根拠（supports / contradicts）として紐づけ、式が保つか観察する
4. データが貯まったら再実行して式がどう変わるか見る

## 注意

- PySR が得意なのは低次元・数値化されたデータです。観察の自由記述テキストはそのままでは入りません（タグの有無を 0/1 特徴量にする拡張は可能）
- 嘔吐のような稀なイベントは、記録が少ないうちは式が安定しません。まず activity で感触を掴むのがおすすめです
- 実行するとカレントディレクトリに `hall_of_fame*.csv` / `*.pkl`（探索結果とモデル状態）が出力されます
