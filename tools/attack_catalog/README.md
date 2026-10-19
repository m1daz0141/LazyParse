# Attack IDの名前辞書

`skills.csv`を手動編集できます。「名前,10進ID,種別または注記,説明,…」のUTF-8 CSVです。同じIDの行を書き換えると名前を上書きし、新しいIDも追加できます。IDはunsigned 32-bitの完全一致で照合し、近いIDから推測しません。カンマを含む値はCSVとして引用してください。

GUIの「解析→攻撃名CSVを開く」で開き、保存後「攻撃名CSVを再読込」または再起動で反映します。同一IDの矛盾する重複やCSV不正では編集ファイル全体を適用せず、固定辞書へ戻して診断へエラーを表示します。行の削除・ファイル欠落も固定辞書の名前へ戻ります。

固定原本は`data/skills_ja.csv`、出典・固定commit・SHA-256・ライセンス確認状況は`data/source.json`。原本はハッシュ検査し、手動編集ファイルには固定ハッシュを要求しません。使用した編集版のハッシュと行番号をレポートへ保持します。辞書の欠落や不正は取得・数値集計を止めません。

名前辞書は人物の帰属・ダメージ/回復分類・JA/Crit判定には使用しません。未登録IDはそのまま残し、GUIでは不明と表示します。上流データの全IDを現行クライアントで検証済みとは扱いません。

上流: [Remon-7L/Remon-7L.github.io](https://github.com/Remon-7L/Remon-7L.github.io)、固定commitはsource.jsonを参照。上流データへこのリポジトリのMITライセンスを付け替えていません。

```powershell
py -3 -m unittest tools.attack_catalog.test_catalog -v
py -3 -m tools.attack_catalog ./my-recording/capture.opcap --output ./audit.json --csv ./audit.csv
```
