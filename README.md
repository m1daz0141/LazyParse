## 免責事項

本ソフトウェアの正確性、安全性、信頼性、完全性、特定目的への適合性および正常な動作について、明示または黙示を問わず、いかなる保証も行いません。

本ソフトウェアの使用、使用不能、不具合その他本ソフトウェアに関連して発生したデータの消失、システムの障害、金銭的損失、第三者との紛争その他一切の損害について、開発者は、法律上認められる最大限の範囲において、一切の責任を負いません。

本ソフトウェアの使用は、利用者自身の判断と責任において行うものとします。

また、本ソフトウェアは現在『PSO2』の利用規約に違反しています。  
使用することはアカウント停止（BAN）の対象となり、法的措置が講じられる可能性もあります。  
本ソフトウェア（およびその他のサードパーティ製解析ツール）から得られた情報を悪用しないでください。

## んでこれは何

PSO2 (Classic) 用ダメージパーサー、つまるところ OverParse リスペクトです  
AI-slop なんで Lazy  

## 動作範囲

JP 版のみで動作確認済  
仕組み上 Epic Games 版は使用できません。Steam 版は知らん  
Due to technical limitations, the Epic Games version cannot be used. I don't know about the Steam version.

## 使用方法

[LazyParse 0.1.2（Windows x64）をダウンロード](https://github.com/m1daz0141/LazyParse/releases/tag/v0.1.2)

※かならず PSO2 を起動する前にこっちを起動すること

1. 展開して好きなところに置いておく  
   exeと同じフォルダに config（設定ファイル）、logs（戦闘ログ）ができるので置き場所注意

2. ダブルクリックで起動  
   （初回 or アップデート時のみ）PSO2 内の「pso2_bin/sub」ゲームフォルダを選択する  
   「eos_gaming.dll」が配置される

3. PSO2 を通常通り起動

4. 「記録」ボタンを押すとDPS計測開始

「解析」メニューで「DPS：クエスト全体表示」と「DPS：総ダメージ基準表示」を切り替え可能

「設定」メニューで「クエスト毎に自動計測」と「計測終了後にTXTを自動出力」を切り替え可能

あとはメニューみて  

## その他

クエスト検知用データ、攻撃用データは以下  
https://github.com/m1daz0141/LazyParse-Data  
「解析」メニューの「クエスト名CSVを開く」または「攻撃名CSVを開く」で編集し、対応する「再読込」で反映

## Thanks

ライセンス  
https://github.com/m1daz0141/LazyParse/blob/main/License  
https://github.com/m1daz0141/LazyParse/blob/main/COPYRIGHT

Remon-7L.github.io - Skills CSVデータ
https://github.com/Remon-7L/Remon-7L.github.io

Miss Mysty's OverParse - 元ネタ  
https://github.com/mysterious64/OverParse
