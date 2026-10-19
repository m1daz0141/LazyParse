# PSO2 Classic x64取得部

Windows x64、PowerShell 7、Gitを使用します。対応版はソース中のSHA-256と受信構造の照合で限定します。ゲームDLL・取得済みパケット・生成されたDLL/EXEは同梱しません。

## ビルド

```powershell
./tools/native_capture/build.ps1 -Bootstrap
./tools/native_capture/build_autostart.ps1
```

Bootstrapは固定版のZigとMinHookをインターネットから取得します。ツールチェーンと生成物は`vendor/`・`bin/`、模擬試験の記録は`captures/`へ置き、Git管理外とします。MinHookは元のライセンスを保持します。

## 配置と取得

ゲームを通常終了した状態で、実際のインストール先の`pso2_bin/sub`を指定してください。

```powershell
$gameSub = 'C:\Games\PHANTASYSTARONLINE2\pso2_bin\sub'
./tools/native_capture/autostart.ps1 -Action Install -GameBin $gameSub
```

配置するのは追加入口`eos_gaming.dll`の1個だけです。同名ファイルがある場合は上書きしません。既存ゲームDLLを置き換えず、対応版のハッシュと配置台帳を照合します。ビルド時の絶対パスが入口へ埋め込まれるため、ビルド後にフォルダーを移動した場合は、入口撤去後に再ビルドしてください。

通常のランチャーから起動し、Classicロビー到達後、GUIで記録開始します。CLIも使えます。

```powershell
./tools/native_capture/autostart.ps1 -Action Status
./tools/native_capture/autostart.ps1 -Action Start
./tools/native_capture/autostart.ps1 -Action Stop
./tools/native_capture/autostart.ps1 -Action Shutdown
```

記録を停止して取得を終了し、PSO2を通常終了した後、同じGameBinを指定して撤去します。

```powershell
./tools/native_capture/autostart.ps1 -Action Remove -GameBin $gameSub
```

台帳とハッシュが一致する追加入口だけを削除します。稼働中や不一致の場合は拒否します。

## 仕組み

追加入口は別の取得DLLをワークスペースから読み込みます。取得DLLは検証済みの登録済み受信コールバックのslotを使用し、必要な通知をコピーして、元コールバックを同じ引数で1回呼びます。戻り値も元処理のものを返し、パケットの書換えや送信は行いません。保存は別スレッドです。新しいslot方式ではゲームのコード・vtable・メモリー保護属性を変更しません。過去の比較試験用コードも含まれますが、自動的な別経路へのfallbackは行いません。

OPCAPはダメージ、OPMETAは限定した識別通知を保持します。人物名・IDを含むため記録の公開前には別途確認が必要です。停止中も識別監視を続け、Shutdownで終了します。各起動で新しいrun、各Startで新しいsegmentを作り、前の操作を引き継ぎません。

## 模擬試験

```powershell
./tools/native_capture/test.ps1
```

ビルド済みの独立テストホストを使用します。ゲームの起動は不要です。
