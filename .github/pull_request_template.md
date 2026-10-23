## 変更内容

## 確認（マージ前にすべて緑であること）

- [ ] Tests
- [ ] Security（Python依存の監査）
- [ ] Container security
- [ ] secret-scan
- [ ] Quality（ruff / mypy / coverage）
- [ ] DBを変更した場合、隔離環境でのダンプ→復元確認（Testsに含まれる）まで成功

## ロールバック方針

問題があった場合の戻し方（旧イメージ・バックアップ・マイグレーションの扱い）を書く。
