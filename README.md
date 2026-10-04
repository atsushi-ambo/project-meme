# めめのノート — 初版の開発準備

めめの声・行動・体調を記録し、めめとの理解を育てる、家族用のブラウザアプリ。

これは実装前の設計資料です。アプリ、クラウド、AI解析はまだ作成・接続していません。

- [初版の設計](docs/design.md)
- [MacBookのCodexに渡す開発指示](docs/codex-brief.md)

まずはサンプルデータを使う画面を実装し、iPhoneで記録操作を確認します。次にMacBook上のSQLiteとメディア保存、ログインを接続し、iPhoneのカメラ動画の取り込みを実機で検証します。

Python学習を目的にFastAPIとJinja2を使います。自宅Wi-Fi経由でMacBookにアクセスする構成で、インターネットへの公開は不要です。

## MacBookで開発を始める

```sh
git clone https://github.com/atsushi-ambo/meme-notebook.git
cd meme-notebook
```

MacBookのCodexでこのフォルダを開き、`docs/codex-brief.md`の指示を渡してください。非公開リポジトリなので、cloneにはGitHubの認証が必要です。

現時点では設計資料のみで、起動できるアプリはありません。初版の実装後にPythonの環境構築と起動方法を追加します。

## 保存するもの

コードと設計資料をGitで管理します。めめの動画・写真・日記DB・認証情報はコミットしません。データはMacBook側に保存し、バックアップはGitとは別に用意します。
