# eADS-B2000

[eNMEA2000](https://github.com/UeEmon/eNMEA2000)（NMEA0183/AIS受信システム）の構成を流用した、ADS-B（1090ES）受信システムと航空機トラフィックシミュレータです。コーディング補助にローカルLLM（Ollama）を使います。

設計・使い方・段階計画は [docs/ADSB_DESIGN.md](docs/ADSB_DESIGN.md) を参照してください。

## すぐ試す

Python 3.9以上（macOS標準の `python3` でも動きます）：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q

# シミュレータ → 受信部
python -m adsb.simulator --count 20 --tcp 30002 &
python -m adsb.receiver --avr-tcp localhost:30002

# eNMEA2000本体（TCP 10111）の地図へ航空機を表示（$PADSB）
python -m adsb.receiver --avr-tcp localhost:30002 --forward localhost:10111 --quiet
```

シミュレータのWeb画面（`--web 8091`、Dockerでは http://localhost:8091 ）で、複数機の諸元を表やCSVで一括設定できます。位置は時間の経過に合わせて推測位置で更新します（設計書6.2節）。

Dockerの場合は、eNMEA2000本体を起動してから `docker compose up -d --build --wait` を実行します。シミュレータ → SDR送信模擬部（rtl_tcp 1236）→ 受信部 → 本体（TCP 10111）の順につながります。eNMEA2000の `bash Loop-Back.sh` は、同じ階層にある本リポジトリも起動します。設定は設計書の7.1節を参照してください。

実機RTL-SDRは、Macなら端末で `rtl_tcp -a 0.0.0.0 -p 1234 -s 2000000 -f 1090000000` を動かし、`ADSB_SOURCE=host.docker.internal:1234 docker compose up -d --build --no-deps receiver` で切り替えます。Linuxは `compose.usb.yaml` でUSBを直結できます（設計書7.2節）。AISと同時に受けるにはドングルが2本必要です。

出力先は複数指定できます。例：`ADSB_OUTPUTS="tcp://host.docker.internal:10111,udp://192.168.1.30:10110"` で、本体とiPadへ同時に `$PADSB` を送ります（設計書7.3節）。ADS-BはAISとは別に扱い、AIS Type 9には変換しません。

## 構成

| モジュール | 役割 |
|---|---|
| `adsb/modes.py` | CRC-24、DF17/18の解析・生成、CPR符号化／復号 |
| `adsb/tracker.py` | ICAO単位の航空機状態 |
| `adsb/mapping.py` | `$PADSB`、eNMEA2000イベント行 |
| `adsb/simulator.py` | 航空機トラフィック生成（AVR/TCP 30002、CU8 I/Q）、推測航法 |
| `adsb/simweb.py`、`adsb/static/sim.html` | シミュレータWeb画面・API（8091） |
| `adsb/ppm.py` | 1090 MHz PPM 変調・最小復調（2 Msps） |
| `adsb/sdrtx.py` | SDR送信模擬部（AVR → PPM I/Q → rtl_tcp 1236） |
| `adsb/rtltcp.py` | rtl_tcpクライアント、USB RTL-SDR（`rtl_sdr`）入力 |
| `adsb/receiver.py` | AVR/I/Q/rtl_tcp/RTL-SDR入力 → JSON行・eNMEA2000本体へ転送 |
| `adsb/ollama_assist.py` | ローカルOllamaによるコーディング補助 |

```bash
python -m adsb.ollama_assist "TC5-8の地上位置デコードを追加して" --file adsb/modes.py --model gpt-oss:20b
```

I/Q出力はソフトウェア試験専用です。1090 MHzでの電波発射は行いません。
