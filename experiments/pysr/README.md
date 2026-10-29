# PySR の合成データ実験

PySR の記号回帰を、既知の式から作った合成データだけで試す独立した実験です。本体アプリや個人の記録は使いません。実行には Python の依存関係に加え、初回に Julia と関連パッケージのダウンロードが必要です。

```bash
# リポジトリルートで、隔離したDocker環境を作って実行・片付ける
python3 scripts/check_pysr.py
python3 scripts/check_pysr.py --audit  # Python依存関係の脆弱性検査
```

手動実行の場合は Python 3.13 の仮想環境に `pip install --require-hashes --only-binary=:all: -r experiments/pysr/requirements.lock` で固定依存を導入します。依存を更新する際は `requirements.in` を変更し、`uv pip compile experiments/pysr/requirements.in --python-version 3.13 --generate-hashes -o experiments/pysr/requirements.lock` で更新して再検証します。Julia側のパッケージはPySRが初回に取得するため、PythonのlockだけでJulia環境の完全な再現性は保証されません。依存監査もPythonパッケージのみで、Julia側は対象外です。

60日分の合成データに、時刻・前回の食事からの時間・週末を使う式とノイズを加えています。`--seed` と `--niterations` を指定できます。表示する R² は**学習に使った同じデータへの当てはまり**であり、未知データへの予測精度ではありません。式・モデルの一時ファイルは OS の一時フォルダに作成し、通常終了時に削除します。

## 実記録への適用について

この実験は `records.json` や健康 CSV を読み込まず、メメの記録を分析しません。任意に記録したデータでは、記録のない時間に行動や嘔吐がなかったと判断できず、嘔吐回数の未入力も 1 回ではありません。記録件数は活動や鳴き声の回数とも限りません。食事・水・トイレは嘔吐後に記録された可能性もあります。

実記録に使うには、何をどの期間観察できたか、欠測とゼロ、時系列、評価方法を先に設計する必要があります。件数だけで適用可能とは判断できません。得られた式から意図、原因、健康状態を断定しません。
