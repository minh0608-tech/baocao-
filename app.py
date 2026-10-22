"""
Web dashboard báo cháy ESP32 - BẢN CHẠY TRÊN INTERNET (Render, Railway...).

ESP32 gửi HTTPS POST JSON tới /api/data, kèm header X-API-Key.
Khóa nằm trong biến môi trường API_KEY của server (đặt trên trang Render).

Lệnh khởi động trên Render:
    gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT app:app
(Phải dùng đúng 1 worker, vì dữ liệu được giữ trong bộ nhớ của tiến trình.)

Thử trên máy tính:
    python app.py --demo
"""

import argparse
import hmac
import math
import os
import random
import re
import threading
import time
from collections import deque

from flask import Flask, jsonify, render_template, request

# Ngưỡng giống firmware (chỉ để hiển thị vạch ngưỡng trên biểu đồ)
TEMP_ON, TEMP_OFF = 40.0, 37.0
GAS_ON, GAS_OFF = 1200, 1000

app = Flask(__name__)

# Khóa bảo vệ API nhận dữ liệu. Để trống = ai cũng gửi được (chỉ dùng khi thử trên máy).
API_KEY = os.environ.get("API_KEY", "")
VALID_STATES = {"BINH THUONG", "CANH BAO: NHIET", "CANH BAO: GAS", "BAO CHAY!"}

lock = threading.Lock()
history = deque(maxlen=300)  # ~5 phút nếu 1 mẫu/giây
latest = None
events = deque(maxlen=50)  # nhật ký thay đổi trạng thái

LINE_RE = re.compile(
    r"T=(?P<t>[-\d.]+|nan|-nan)\s*C;\s*"
    r"GAS_ADC=(?P<gas>\d+);\s*"
    r"CO_NHIET=(?P<tf>[01]);\s*"
    r"CO_GAS=(?P<gf>[01]);\s*"
    r"STATE=(?P<state>.+)"
)


def classify(temp_alarm: bool, gas_alarm: bool) -> str:
    if temp_alarm and gas_alarm:
        return "BAO CHAY!"
    if temp_alarm:
        return "CANH BAO: NHIET"
    if gas_alarm:
        return "CANH BAO: GAS"
    return "BINH THUONG"


def parse_line(line: str):
    """Trả về dict hoặc None nếu dòng không phải dữ liệu."""
    m = LINE_RE.search(line)
    if not m:
        return None
    try:
        temp = float(m["t"])
        if math.isnan(temp):
            temp = None
    except ValueError:
        temp = None
    return {
        "temp": temp,
        "gas": int(m["gas"]),
        "temp_alarm": m["tf"] == "1",
        "gas_alarm": m["gf"] == "1",
        "state": m["state"].strip(),
    }


def push(sample: dict, source: str):
    global latest
    now = time.time()
    sample = dict(sample)
    sample["ts"] = now
    sample["source"] = source
    sample["buzzer"] = sample["temp_alarm"] or sample["gas_alarm"]
    with lock:
        prev = latest["state"] if latest else None
        latest = sample
        history.append(sample)
        if prev != sample["state"]:
            events.appendleft({"ts": now, "state": sample["state"]})


# ---------------- Nguồn dữ liệu ----------------
def serial_worker(port: str, baud: int):
    import serial  # import muộn để --no-serial không cần pyserial

    while True:
        try:
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = baud
            ser.timeout = 2
            # Tắt DTR/RTS trước khi mở: nếu không, ESP32 bị giữ ở trạng thái reset
            ser.dtr = False
            ser.rts = False
            ser.open()
            with ser:
                print(f"[serial] đã mở {port} @ {baud}")
                shown = 0
                while True:
                    raw = ser.readline().decode("utf-8", errors="ignore").strip()
                    if not raw:
                        continue
                    sample = parse_line(raw)
                    if sample:
                        push(sample, "serial")
                    elif shown < 10:
                        # In vài dòng không đọc được để dễ kiểm tra khi có lỗi
                        print(f"[serial] bỏ qua dòng: {raw}")
                        shown += 1
        except Exception as e:  # rút cáp, sai cổng, ...
            print(f"[serial] lỗi: {e} - thử lại sau 3 giây")
            time.sleep(3)


def demo_worker():
    """Dữ liệu giả: chu kỳ ~60 giây đi qua đủ 4 trạng thái."""
    t0 = time.time()
    temp_alarm = gas_alarm = False
    while True:
        phase = (time.time() - t0) % 60
        temp = 28 + random.uniform(-0.3, 0.3)
        gas = 600 + random.randint(-40, 40)
        if 15 <= phase < 30:
            temp += (phase - 15) * 1.1
        if 30 <= phase < 45:
            gas += int((phase - 30) * 70)
        if 45 <= phase < 55:
            temp += 14
            gas += 800
        if temp >= TEMP_ON:
            temp_alarm = True
        elif temp <= TEMP_OFF:
            temp_alarm = False
        if gas >= GAS_ON:
            gas_alarm = True
        elif gas <= GAS_OFF:
            gas_alarm = False
        push(
            {
                "temp": round(temp, 1),
                "gas": gas,
                "temp_alarm": temp_alarm,
                "gas_alarm": gas_alarm,
                "state": classify(temp_alarm, gas_alarm),
            },
            "demo",
        )
        time.sleep(1)


# ---------------- Routes ----------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        temp_on=TEMP_ON,
        temp_off=TEMP_OFF,
        gas_on=GAS_ON,
        gas_off=GAS_OFF,
    )


@app.route("/api/latest")
def api_latest():
    with lock:
        return jsonify({"latest": latest, "server_time": time.time()})


@app.route("/api/history")
def api_history():
    with lock:
        return jsonify(
            {
                "history": list(history),
                "events": list(events),
                "server_time": time.time(),
            }
        )


@app.route("/api/data", methods=["POST"])
def api_data():
    """ESP32 gửi JSON: {"temp": 30.5, "gas": 850, "temp_alarm": false, "gas_alarm": false}"""
    if API_KEY:
        sent = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(sent.encode(), API_KEY.encode()):
            return jsonify({"ok": False, "error": "sai khóa API"}), 401
    body = request.get_json(silent=True) or {}
    try:
        gas = int(body["gas"])
        temp = body.get("temp")
        temp = None if temp is None else float(temp)
        if temp is not None and math.isnan(temp):
            temp = None
        ta = bool(body.get("temp_alarm", False))
        ga = bool(body.get("gas_alarm", False))
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "JSON không hợp lệ"}), 400
    state = body.get("state")
    if state not in VALID_STATES:  # chỉ nhận 4 trạng thái hợp lệ
        state = classify(ta, ga)
    push(
        {
            "temp": temp,
            "gas": gas,
            "temp_alarm": ta,
            "gas_alarm": ga,
            "state": state,
        },
        "wifi",
    )
    return jsonify({"ok": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="cổng Serial, ví dụ COM3 hoặc /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--no-serial", action="store_true")
    ap.add_argument("--demo", action="store_true", help="dữ liệu giả để thử giao diện")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--web-port", type=int, default=5000)
    args = ap.parse_args()

    if args.demo:
        threading.Thread(target=demo_worker, daemon=True).start()
    elif args.port and not args.no_serial:
        threading.Thread(
            target=serial_worker, args=(args.port, args.baud), daemon=True
        ).start()
    elif not args.no_serial:
        print("Chưa có --port. Chạy ở chế độ chỉ nhận WiFi (hoặc dùng --demo).")

    app.run(host=args.host, port=args.web_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
