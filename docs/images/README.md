# Screenshot assets

GitHub公開用のスクリーンショットを管理するディレクトリです。メインREADMEには、公開前確認を完了した画像だけを相対リンクで掲載します。

## 推奨ファイル

| ファイル名 | 内容 | READMEでの推奨位置 |
| --- | --- | --- |
| `dashboard.png` | 応募状況・期限・面接・タスクのダッシュボード | 「Screenshots」の先頭 |
| `gmail-list.png` | Gmail一覧・検索・ページネーション | 「Screenshots」 |
| `ai-analysis.png` | AI解析結果・evidence・3方向への反映導線 | 「Screenshots」の中心画像 |
| `calendar-review.png` | Calendar登録前の確認・編集画面 | 「Screenshots」 |
| `application-detail.png` | 応募先・Checklist・Calendar同期 | 「Screenshots」 |
| `pytest-553.png` | 全pytestの成功結果 | 「テスト」 |

## 撮影前チェック

- 実在する氏名、企業名、メールアドレス、件名、本文を使用しない
- OAuth client ID / secret、API key、token、認可コードを映さない
- Google event ID、Gmail message ID、ローカル絶対パスを映さない
- ブラウザのアカウントアイコン、通知、ブックマーク等も確認する
- EXIF・XMPなど、公開に不要な画像メタデータを残さない
- 画像追加時点の実際のテスト件数に合わせて、pytest画像のファイル名とREADME記載を更新する

READMEへ掲載している画像を差し替える場合も、相対パスとalt属性を維持し、実際のテスト件数や画面内容と説明が一致することを確認してください。
