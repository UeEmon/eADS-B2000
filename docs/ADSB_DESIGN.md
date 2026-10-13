# eADSB2000 設計書：ADS-B受信システム＆シミュレータ

作成日：2026-10-13（日本時間）  
対象：[eNMEA2000](https://github.com/UeEmon/eNMEA2000)（NMEA0183/AIS）の構成を流用したADS-B（1090ES）受信・模擬環境。本リポジトリ eADS-B2000 は独立して動作し、eNMEA2000本体へはTCPで連接します。  
状態：**第2段階**（第1段階のコア実装に加え、`$PADSB` によるeNMEA2000でのネイティブ表示とDocker Compose化）。

## 1. 目的

- 航空機のADS-B（Mode S 拡張スキッタ DF17/18、1090 MHz）を受信・解析し、eNMEA2000の保存・GIS表示基盤へ載せる。
- 実機SDRがなくても試験できるよう、航空機トラフィックを生成するシミュレータと、PPM変調I/Qを作る送信模擬を持つ。
- コーディング補助にローカルLLM（Ollama）を使う。生成コードは必ず人がレビューし、テストで確認してから取り込む。

## 2. eNMEA2000との対応

AIS系の各構成に、ADS-B側の同じ役割の部品を置きます。

| eNMEA2000（AIS） | eADSB2000（ADS-B） | 実装 |
|---|---|---|
| eNMEA2000 `app/parser.py` NMEA/AISデコーダ | Mode S/1090ESデコーダ（CRC-24、DF17/18、CPR） | `adsb/modes.py` |
| AIS分割再構成・MMSI単位の情報統合 | ICAO単位の航空機状態（偶奇CPRペア、呼出符号、速度） | `adsb/tracker.py` |
| `app/store.py` のイベント行 | 同じ行形式（`sentence_type='ADSB'`、`icao`付き） | `adsb/mapping.py` `event_row` |
| NMEAエミュレータ（AIS船舶生成） | 航空機トラフィックシミュレータ | `adsb/simulator.py` |
| SDR送信模擬部（GMSK、9600bps） | PPM変調（2 Msps、CU8 I/Q） | `adsb/ppm.py` |
| SDR受信部（AIS-catcher） | 受信部（AVR入力 または I/Q復調） | `adsb/receiver.py` |
| — | ローカルLLMコーディング補助 | `adsb/ollama_assist.py` |

## 3. データ経路

```mermaid
flowchart TD
 S["ADS-Bシミュレータ<br/>adsb.simulator"] -->|AVR TCP 30002| R["ADS-B受信部<br/>adsb.receiver"]
 S -->|CU8 I/Q 2Msps| R
 D["dump1090 / readsb<br/>（実機RTL-SDR 1090MHz）"] -->|AVR TCP 30002| R
 R -->|JSON行（eNMEA2000イベント形式）| J["保存・解析"]
 R -->|$PADSB TCP 10111（既定）／AIS Type 9（旧版向け）| A["eNMEA2000本体"]
 A --> G["Cesium GIS / PostgreSQL / iPad"]
```

- 実機受信は、実績のある dump1090／readsb に復調を任せ、その **AVR生フレーム出力（TCP 30002、`*8D...;`形式）** を受け取る構成を基本とします。AIS側でAIS-catcherに復調を任せたのと同じ考え方です。
- `adsb/ppm.py` の復調器は試験・ループバック用の最小実装です。実電波の性能は dump1090／readsb が上です。

## 4. 対応メッセージ（第1段階）

| DF/TC | 内容 | 解析 | 生成 |
|---|---|---|---|
| CRC-24 | パリティ検査（生成多項式 0x1FFF409） | ○ | ○ |
| DF17/18 TC1–4 | 航空機識別（呼出符号・カテゴリ） | ○ | ○ |
| DF17/18 TC9–18 | 空中位置（気圧高度、CPR偶奇） | ○（Q=1の25ft単位。Gillham符号は未対応） | ○ |
| DF17/18 TC20–22 | 空中位置（GNSS高度） | ○ | — |
| DF17/18 TC19 | 対地速度・針路・昇降率（サブタイプ1/2） | ○ | ○（サブタイプ1） |
| TC5–8 | 地上位置 | 未対応（第2段階） | — |
| DF4/5/20/21/11 | 応答・全呼出 | 未対応（アドレスパリティのため別処理） | — |

位置はCPRで符号化されており、1通だけでは位置が確定しません。

- **グローバル復号**：偶・奇フレームが10秒以内にそろったとき、新しい方の形式で緯度経度を確定します。
- **ローカル復号**：以後は前回位置（または受信局位置 `--ref`）を基準に1通で更新します（基準から180NM以内が前提）。
- 経度帯数NLは緯度0で59（規格の特例）です。

## 5. eNMEA2000への写像

### 5.1 イベント行（`event_row`）

`app/parser.Decoder.parse` と同じキーを持ちます。既存の `Store.add` へそのまま保存できる形です。

| キー | 値 |
|---|---|
| `sentence_type` | `ADSB` |
| `raw` | 112ビットフレームの16進文字列 |
| `status` / `checksum_valid` | CRC合格のみ `ok` / `true` |
| `latitude` / `longitude` | 航空機状態の最新位置（未確定なら `null`） |
| `mmsi` | `null`（航空機はICAOで識別） |
| `icao` | 24ビットICAOアドレス（16進6桁） |
| `decoded` | そのフレームの解析結果＋`aircraft`（呼出符号・高度・速度等の統合状態） |

`events` テーブルに `icao` 列はまだありません。第2段階でDB列・API・画面を追加します。

### 5.2 `$PADSB` 独自センテンス（`padsb`、既定）

eNMEA2000本体が航空機として直接解釈する独自センテンス（`$P` で始まるメーカー独自形式）です。受信部の既定の転送形式です。

```
$PADSB,ICAO,呼出符号,緯度,経度,気圧高度ft,対地速度kt,対地航跡度,昇降率ft/min,カテゴリ*hh
$PADSB,86D3A1,JAL123,35.549400,139.779800,12500,420.0,287.0,-1472,3*hh
```

- 緯度経度は符号付きの度（6桁）。不明な項目は空欄。NMEAと同じXORチェックサムです。
- eNMEA2000本体はICAOを `events.icao` 列に保存し、地図に航空機シンボル（「呼出符号 FL高度」）を描き、右側の航跡詳細に高度・速度・航跡・昇降率を表示します（eNMEA2000側の対応が必要です）。
- 位置が確定していない航空機は送信しません。1機あたり1秒に1回までです。

### 5.3 AIS Type 9 ブリッジ（`ais_type9`、`--format ais9`）

`$PADSB` に未対応のeNMEA2000やiPad版で航空機を表示するため、航空機状態を AIS Type 9（捜索救難航空機位置通報）の `!AIVDM` に変換し、本体のTCP 10111へ送ります。

- MMSIは疑似値 `100000000 + ICAO(10進)`。実在のMMSIと区別できないため、**試験用途に限定**します。
- 高度はAISの上限で0〜4094 mに丸めます。速度はノット（0〜1022）、針路は0.1度単位です。
- 1機あたり1秒に1回まで送信します。
- 生成した文は pyais（eNMEA2000本体と同じ3.2.3）でType 9として復号できることをテストで確認しています。eNMEA2000の `app/parser.py` での受理も確認済みです（2026-10-13、eNMEA2000側で実行）。

## 6. シミュレータ

- 中心（既定：羽田 RJTT 35.5494, 139.7798）から半径60NM内に指定機数を配置し、大圏航法で移動、範囲外に出ると毎秒3度で中心方向へ旋回します。
- ICAOは日本の割当ブロック（0x840000〜0x87FFFF）から乱数で採番、呼出符号は国内外航空会社の3レターコード＋便名です。
- 0.5秒ごとに位置（偶奇交互）と速度、5秒ごとに識別を送ります（実機の送信間隔に近い値）。
- 出力：AVRテキスト（標準出力、またはdump1090互換のTCPサーバ）、またはCU8 I/Qファイル（雑音・周波数ずれ指定可）。

**I/Qはソフトウェア試験専用です。1090 MHzでの電波発射は電波法上許されません。** eNMEA2000のSDR送信模擬部と同様、RF送信機・アンテナへは出力しません。

## 7. 使い方

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q

# 1) シミュレータ → 受信部（AVR/TCP）
python -m adsb.simulator --count 20 --tcp 30002 &
python -m adsb.receiver --avr-tcp localhost:30002

# 2) 受信部 → eNMEA2000本体（$PADSB）。本体を起動しておく
python -m adsb.receiver --avr-tcp localhost:30002 --forward localhost:10111 --quiet
#    $PADSB未対応の本体・iPad向け: --format ais9

# 3) I/Qで変復調まで試験
python -m adsb.simulator --count 8 --seconds 30 --iq traffic.cu8 --noise 0.03
python -m adsb.receiver --iq traffic.cu8

# 4) 実機：RTL-SDR + readsb/dump1090 の生出力を受信
python -m adsb.receiver --avr-tcp <受信機のIP>:30002 --ref 35.55,139.78 --forward localhost:10111
```

### 7.1 Docker Compose

```bash
# 先にeNMEA2000本体を起動（TCP 10111）。その後:
docker compose up -d --build --wait
# 受信元を実機のdump1090/readsbにする場合（シミュレータは不要）
ADSB_SOURCE=192.168.1.50:30002 docker compose up -d --build --no-deps receiver
```

| 変数 | 既定値 | 内容 |
|---|---|---|
| `ADSB_COUNT` / `ADSB_LAT` / `ADSB_LON` / `ADSB_RADIUS_NM` | 20 / 35.5494 / 139.7798 / 60 | シミュレータの機数・中心・半径 |
| `ADSB_BIND` | 127.0.0.1 | シミュレータのAVR出力（30002）の公開先 |
| `ADSB_SOURCE` | simulator:30002 | 受信部の入力（AVR生フレーム） |
| `ENMEA_TARGET` | host.docker.internal:10111 | 転送先（eNMEA2000本体のTCP） |
| `ADSB_FORMAT` | padsb | `padsb` または `ais9` |

eNMEA2000の `bash Loop-Back.sh` は、eNMEA2000と同じ階層に本リポジトリ（`eADS-B2000`）がある場合、または `ADSB_DIR` を指定した場合に、このComposeも起動します。

受信部は入力元が切断されると5秒ごとに再接続し、転送先へは送信失敗のたびに接続し直します。

## 8. ローカルLLM（Ollama）による開発補助

### 8.1 方針

- Ollamaはユーザー端末（Mac、`http://localhost:11434`）で動かします。クラウドへソースを送りません（`:cloud` 付きモデルは外部サーバで動くため除外）。
- LLMの出力は**提案**として扱い、人がレビューし、既知の試験ベクトルで検証してから取り込みます。リポジトリへ自動書き込みはしません。
- 推奨モデル（端末に導入済み）：
  - `gpt-oss:20b` …… 既定。コード生成・レビュー。
  - `gemma4:26b-a4b-it-qat` …… 長い文脈（複数ファイル）を読ませる場合。
  - `gemma4:12b` …… 軽量・高速な下書き。

### 8.2 実例（本段階で実施）

CPRエンコーダ `cpr_encode` は `gpt-oss:20b` に下書きさせました。レビューで、経度帯数 `nl()` が赤道（緯度0）で60を返す誤りを見つけ、規格の特例（59）を加えて修正しています。修正は `test_nl_equator` と往復テストで確認しています。LLMの出力をそのまま信用しない理由の具体例です。

### 8.3 CLI

```bash
python -m adsb.ollama_assist "TC5-8の地上位置デコードを追加して" --file adsb/modes.py --model gpt-oss:20b > suggestion.md
```

`OLLAMA_HOST`（既定 `http://localhost:11434`）、`OLLAMA_MODEL`（既定 `gpt-oss:20b`）で変更できます。コンテナ内からは `http://host.docker.internal:11434` を指定します。

## 9. 段階計画

| 段階 | 内容 |
|---|---|
| 1（完了） | デコーダ／エンコーダ、航空機状態、写像、シミュレータ、PPM変復調、受信CLI、Type 9ブリッジ、Ollama補助、テスト |
| 2（本PR） | `$PADSB`、Docker Compose化、eNMEA2000の `Loop-Back.sh` への組込み、eNMEA2000のDBへ `icao` 列と `?icao=` 絞り込み、本体GISの航空機シンボル・高度表示・航跡詳細 |
| 3 | シミュレータWeb画面（機数・航路・緊急スクォーク）、RTL-TCP配信（dump1090へI/Qを直接供給）、TC5–8地上位置、TC28/29/31、DF4/5/20/21 |
| 4 | 実機検証（RTL-SDR + readsb、受信局位置、MLATなし）、AWS配備、iPad版への航空機表示 |

## 10. 検証状況

- `tests/test_adsb.py`：公開資料の既知フレーム（KLM1023、52.2572N/3.91937E、159kt/182.88°/−832fpm）、CRC誤り検出、世界4地点のCPR往復、PPM変復調（雑音・20kHzずれ）、シミュレータ→受信→Type 9（pyais復号）、TCP転送、Ollama API呼出し（模擬サーバ）。
- `$PADSB` の書式・チェックサム、受信部から`$PADSB`／Type 9 のTCP転送。
- Docker Compose（シミュレータ→受信部→ホストのTCP 10111）で `$PADSB` が届くこと（GitHub Actionsの `docker` ジョブ）。
- eNMEA2000本体と連接し、地図に航空機と「呼出符号 FL高度」、航跡詳細が表示されること（eNMEA2000側のブラウザ試験）。
- 実機RTL-SDR、dump1090/readsbとの接続、Docker Desktop（Mac）、端末上のOllama CLI実行は**未確認**です。

## 参照

- Junzi Sun, *The 1090 Megahertz Riddle*（第2版） https://mode-s.org/1090mhz/
- RTCA DO-260B / EUROCAE ED-102A（1090ES MOPS）
- ITU-R M.1371-6（AIS Type 9）
- Ollama API https://github.com/ollama/ollama/blob/main/docs/api.md
