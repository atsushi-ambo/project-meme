# PythonとDBの処理を追う

## 最初に読む順番

1. `app/main.py` の `entry_save`：HTTPの入口。
2. `app/schemas.py` の `ObservationInput`：受け付ける入力の型。
3. `app/services.py` の `save_entry`：DBへ保存する処理。
4. `app/models.py` の `Observation`：テーブルの構造。
5. `app/templates/detail.html`：保存したデータの表示。

## 一件の観察を保存するとき

ブラウザがフォームをPOSTすると、`read_form()`がCSRFトークンを照合します。`ObservationInput.model_validate()`は日時・文字数・タグを検証します。日本時間の入力はUTCへ変換されます。入力不正なら保存せず、入力を残したフォームを返します。

`transaction()`はSQLAlchemyのSessionを開き、成功時はcommit、例外時はrollbackします。`save_entry()`は入力をObservationへ入れ、`db.add()`で追加対象にし、`db.flush()`でSQLを実行します。flushの時点ではトランザクション全体の確定前です。

概念的には次のSQLです。値をSQL文字列へ連結せず、ドライバーへ別に渡します。

```sql
INSERT INTO observations
    (cat_id, occurred_at, timezone, location, description,
     submission_key, submission_hash, created_at, updated_at)
VALUES
    (:cat_id, :occurred_at, :timezone, :location, :description,
     :submission_key, :submission_hash, :created_at, :updated_at)
RETURNING id;
```

保存した後は303リダイレクトで詳細画面へ移動します。更新直後にブラウザを再読み込みしても、同じPOSTを送らずGETします。通信切断などの再送にはsubmission_keyを使い、同じ入力なら保存済みの結果を返します。入力のハッシュを残すことで、同じキーで異なる入力を送った場合は拒否します。

## なぜテーブルを分けるか

観察には追記が複数、タグも複数あります。観察をタグの数だけ複製すると、本文を編集するときにすべて直す必要があります。そのため観察本体と関連テーブルを分けます。

`observation_tags`の主キーは`(observation_id, tag_id)`の組です。同じタグを一つの観察へ二度関連づけられません。外部キーは、存在しない観察やタグを参照しないように守ります。

`dictionary_evidence`も同様です。「ごはん要求かも」という仮説を、観察Aでは支持、観察Bでは不一致と評価できます。こちらからの合図は`interactions.cue_item_id`で別に関連づけます。

## 添付は二段階で保存する

本文を保存した後、ブラウザはJSONで容量予約を作り、次にファイルをPUTで送ります。ファイルのリクエスト本文を分割して読み、全体をメモリへ載せません。通常フォームにはpython-multipartを使いますが、ファイル本体は余計な一時コピーを作らないraw stream方式です。

`app/media.py`は予約量と実サイズ、ファイル内容を検証します。動画はコンテナ内のffprobeでコーデックを調べ、HEVC/H.264を許可します。画像はPillowで検証します。検証コマンドにシェルは使わず、ローカルファイル以外を参照する形式は拒否します。

ファイルの移動とPostgreSQLのcommitは一つのトランザクションにはできません。そこでpending → readyという状態を保存します。途中停止したpendingはジョブが回収します。ファイルを削除できない場合は容量予約を保持し、成功するまで容量を使っているものとして扱います。

DBは同期処理なので、asyncルートからは`anyio.to_thread.run_sync()`で呼び、イベントループを塞がないようにします。Sessionを複数スレッドから同時には使いません。

## ロックと期限

通常処理は共有の保守ロック、出力・期限整理は排他の保守ロックを取得します。小規模な初版で処理の整合性を追いやすくする設計です。競合時は409で再試行を案内します。アップロード中は期限整理が次の周期へ延期されます。

期限は画面で非表示にするだけでなく、配信ルートでも確認します。「残す」と削除はDBロックで同時変更を防ぎます。削除要求をDBへ残すので、停止しても再開できます。保守ジョブは一つのWebプロセスから約60秒ごとに実行します。worker数を増やす段階ではジョブ分離を再設計してください。

## DB構造を変更するとき

`models.py`だけの変更では、既存DBのテーブルは変わりません。Alembicのマイグレーションを作り、生成結果を確認して適用します。最初の10テーブルの作成手順は`migrations/versions/`にあります。

```sh
# 開発時のみ、ソースをマウントして変更ファイルを生成
# 既存データがある環境では先にバックアップを取得する
 docker compose run --rm -v "$PWD:/app" migrate alembic revision --autogenerate -m change
 docker compose up -d --build
```

## 実装の検証記録

- PostgreSQLを使う11件の統合テストが成功。
- 生成したHEVC・120fpsの小さな動画を保存し、Rangeによる部分配信を確認。
- MacBookのChromeでデスクトップと390px幅の画面を確認。ブラウザで記録作成・写真添付・削除も成功。
- DBダンプとメディアのバックアップから、独立したDockerプロジェクトへ復元しhealthcheck成功を確認。
- テスト用HTTPXに関するStarletteの非推奨警告が1件あるが、現行テストは成功。テストクライアントの次回更新時に移行を検討する。

実際のiPhone由来4K・120fps/HDR、1GB上限付近の動画、iPhone/iPad実機は未確認です。スマートフォン幅のデスクトップChrome検証は実機検証の代用にはなりません。
