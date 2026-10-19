# LazyParse

PSO2 Classic x64用の取得・集計・コンパクトGUI。旧OverParseの配置を踏襲したPython/Tk製の新しいGUIです。NGSは対象外です。

## 主な機能

- 人物・攻撃別の与ダメージ、被ダメージ、DPS、JA、Crit、最大ヒット。
- ペットの所有者への帰属。負数の回復候補をダメージ集計から除外。
- 連続時間の「クエスト中」と従来の区間合算「ダメージ計測」を切替。
- クエストごとの自動計測・クリア候補での時計固定・履歴のJSON保存（試験中）。
- 手動編集できる攻撃名CSV、Timeの小数表示切替。

## 必要環境

Windows x64、Python 3.13とTk、PowerShell 7、Git。GUIと集計はPython標準ライブラリのみを使います。取得DLLをソースから作る場合は、ビルド手順で固定版のZigとMinHookを取得します。

```powershell
./Start-LazyParse.ps1
```

GUI起動だけではDLLの配置やゲーム起動を行いません。[取得部のビルド・配置](tools/native_capture/README.md)を先に確認してください。取得部は対応版のSHA-256を照合し、未対応版では動作を保留します。

保存済みデータを開く場合:

```powershell
./Start-LazyParse.ps1 -Capture ./my-recording/capture.opcap
```

## 説明

- [GUIの操作](tools/gui/README.md)
- [取得DLL](tools/native_capture/README.md)
- [集計・再生](tools/combat_core/README.md)
- [攻撃名CSV](tools/attack_catalog/README.md)
- [開発状況・残る確認](docs/DEVELOPMENT.md)

## 記録と通信

取得・集計・GUIはローカルファイルを使います。LazyParseによるテレメトリーや外部送信はありません。開発用の依存取得は明示的なビルド操作時だけ行います。

ゲーム内の人物名・ID等は記録ファイルに含まれます。画面の匿名表示は保存ファイルの匿名化ではありません。この公開ソースには実機ログ・スクリーンショット・個人設定・ローカル引き継ぎ文書を含めていません。

## 検証範囲

特定版のJP Classicとソロの森林探索で確認しています。クエスト種別全般、失敗・リトライ、パーティーの挙動は検証中です。JAには対象外・未判定の区別があり、ペットはJAの対象外です。実機データを使う一部の回帰試験は、非公開のfixtureがない環境ではskipします。

## ライセンス

LazyParse独自コード・文書は **WTFPL version 2**。Copyright (C) 2026 m1daz0141。[License](License)、[COPYRIGHT](COPYRIGHT)を参照してください。

第三者由来のデータやビルド依存物は元の条件を保持します。[Third-party notices](THIRD_PARTY_NOTICES.md)に適用範囲を分けて記載しています。指定のRemon攻撃名CSVは保持していますが、上流のライセンス表示は未確認であり、MITやWTFPLをこちらから付与していません。

旧OverParseのWPFソース・画像・プロジェクト・除外CSVは同梱しません。新GUIは独立実装です。JA除外はLazyParseの独立した設定ファイルで扱い、旧フォルダーがなくても起動・集計できます。UIの参考元: [mysterious64/OverParse](https://github.com/mysterious64/OverParse)。
