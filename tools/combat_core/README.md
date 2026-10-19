# 集計・再生

OPCAP/OPMETAを読み、人物・所有者・敵の通知とダメージをQPC順に照合します。パケットの数値・符号・元ID・帰属根拠を保持し、未確定の発生元を本人へ推測加算しません。

```powershell
py -3 -m tools.combat_core ./my-recording/capture.opcap --output ./report.json --events ./events.jsonl
py -3 -m tools.combat_core.quest_replay ./my-recording/capture.opcap --output ./quest-report.json
```

出力先は新規ファイルを指定してください。`--follow`で追記を読み取れます。

負数は回復候補としてダメージ集計前に除外しますが、符号付き記録を保持します。攻撃ID0の正値は品質フラグ付きで含めます。ペットは確認できた所有者へ帰属し、JA判定は所有者帰属より前にペットを対象外にします。Critは所有者へ加算したペットのヒットも含みます。

GUIのDPS方式は連続時間と正値イベント区間の時間合計から選べます。クエスト自動区切りは限定版・限定通知で試験中です。開始worldの変化、クリア候補の通知対、帰還結果を別々に扱います。未確定・終了後データを別枠へ保持し、全件数の一致を検査します。途切れ時刻を特定できる場合は影響区間だけを閉じ、それ以前の完了周回を保持します。

```powershell
py -3 -m unittest tools.combat_core.test_combat_core tools.combat_core.test_live tools.combat_core.test_hit_flags tools.combat_core.test_hit_rates tools.combat_core.test_quest_probe tools.combat_core.test_quests tools.combat_core.test_shared_files -v
```

配布していない実機fixtureとビルド前のnativeテストホストはskipします。合成パケットによる分類・区切り・保存処理の試験は実行できます。公開ソース内のfixture名・人物名・人物IDは匿名のplaceholderです。


## 独立したJA除外設定

旧OverParseの除外CSVを読みません。標準ルールは`data/ja_exclusions.csv`で、独自に定義したタクト通常攻撃1 IDのみです。ペットは発生元の種別でJA対象外にします。標準セットを旧129 IDと同じ検証範囲とは扱いません。その他の攻撃は既存のbit候補判定を維持し、全攻撃の適用範囲は未検証です。

ユーザー設定が必要な場合は`ja_exclusions.local.csv`を作成します。形式は`attack_id,reason`のCSV（UTF-8）、または1行に1つの10進IDです。ローカル設定があれば標準を置き換え、そのパス・ハッシュ・件数をレポートに残します。内容不正ではJAを未判定にし、Critやダメージ値は維持します。このローカル設定はGit管理外です。

旧API名`read_legacy_ignore`は互換用の別名として残りますが、旧アプリのファイルを開く処理はありません。ネイティブDLLのOverParse名は内部ABIの互換名で、旧WPFソースへの依存ではありません。
