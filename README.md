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

## 操作と設定

上部メニューは「計測 / DPS / 解析 / 表示 / ログ」。計測から記録開始・停止・保存して次の計測・取得終了を操作します。GUIを閉じるだけでは取得を停止しません。行のダブルクリックで攻撃別・発生元・識別品質を確認できます。

- **DPS→クエスト中**: 最初の与ダメージから終了まで。無攻撃・移動時間を含む連続時間（既定）。
- **DPS→ダメージ計測**: 既定10秒以上の無ダメージで分けた正値区間の最初～最後を合算する従来方式。合計0秒のDPSは「—」。
- **計測→Timeを小数点まで表示**: ONで小数2桁、OFFで整数秒切り捨て。DPSの計算精度は変えません。
- **計測→クエストごとに自動計測（試験中）**: 記録を続けながら周回を区分し、クリア候補で時計を固定。帰還結果は後から紐付け、次の受注で新計測へ切り替えます。未確定・終了後データは別枠に保持します。
- **解析→攻撃名CSVを開く／再読込**: `tools/attack_catalog/skills.csv`をUTF-8で編集します。形式は「名前,10進ID,種別または注記,説明,…」。固定辞書より優先して同一IDを上書き・新IDを追加。不正時は固定辞書へ戻し診断へ理由を表示します。人物・所有者・回復・JA/Critの分類は変えません。

設定は`tools/gui/state/preferences.json`、自動保存は同`state/logs/`。通常終了時に設定を保存します。JA除外の標準は`tools/combat_core/data/ja_exclusions.csv`、追加のユーザー設定はGit管理外の`tools/combat_core/ja_exclusions.local.csv`（`attack_id,reason`のCSV、または1行1 ID）。ペットはJA対象外です。

## 保存データの再生

```powershell
py -3 -m tools.combat_core ./my-recording/capture.opcap --output ./report.json --events ./events.jsonl
py -3 -m tools.combat_core.quest_replay ./my-recording/capture.opcap --output ./quest-report.json
```

出力先は新規ファイルを指定してください。元のOPCAP/OPMETAは変更しません。詳しい[取得部のビルド・配置](tools/native_capture/README.md)と[開発状況・残る確認](docs/DEVELOPMENT.md)も参照できます。

## 記録と通信

取得・集計・GUIはローカルファイルを使います。LazyParseによるテレメトリーや外部送信はありません。開発用の依存取得は明示的なビルド操作時だけ行います。

ゲーム内の人物名・ID等は記録ファイルに含まれます。画面の匿名表示は保存ファイルの匿名化ではありません。この公開ソースには実機ログ・スクリーンショット・個人設定・ローカル引き継ぎ文書を含めていません。

## 検証範囲

特定版のJP Classicとソロの森林探索で確認しています。クエスト種別全般、失敗・リトライ、パーティーの挙動は検証中です。JAには対象外・未判定の区別があり、ペットはJAの対象外です。実機データを使う一部の回帰試験は、非公開のfixtureがない環境ではskipします。

## ライセンス

LazyParse独自コード・文書は **MIT License**。Copyright (C) 2026 m1daz0141。[License](License)、[COPYRIGHT](COPYRIGHT)を参照してください。

第三者由来のデータやビルド依存物は元の条件を保持します。[Third-party notices](THIRD_PARTY_NOTICES.md)に適用範囲を分けて記載しています。指定のRemon攻撃名CSVは保持していますが、上流のライセンス表示は未確認であり、本プロジェクトのMITを付与していません。

旧OverParseのWPFソース・画像・プロジェクト・除外CSVは同梱しません。新GUIは独立実装です。JA除外はLazyParseの独立した設定ファイルで扱い、旧フォルダーがなくても起動・集計できます。UIの参考元: [mysterious64/OverParse](https://github.com/mysterious64/OverParse)。
