import os
import json
import time
import threading
import requests
from flask import Flask, Response, request
from datetime import datetime

app = Flask(__name__)

SOURCE_API = "https://chanminhdi.onrender.com/lc79md5"
HISTORY_FILE = "history.json"
CREDIT = "chủ nguồn api @Duykhanhios"

lock = threading.Lock()

# Header giả trình duyệt để không bị nguồn chặn
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Referer": "https://chanminhdi.onrender.com/",
    "Origin": "https://chanminhdi.onrender.com",
}


# ================== LỊCH SỬ ==================
def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(history):
    with lock:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)


# ================== GỌI NGUỒN ==================
def fetch_source():
    try:
        r = requests.get(SOURCE_API, headers=BROWSER_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ================== DỰ ĐOÁN ==================
def predict_next(data):
    try:
        confidence = float(str(data.get("do_tin_cay", "50")).replace(",", ".").replace("%", ""))
    except Exception:
        confidence = 50.0

    chuoi_thua = int(data.get("chuoi_thua", 0))
    auto_flip = bool(data.get("auto_flip", False))
    base = data.get("base_du_doan")
    current = data.get("du_doan", "")

    pred = base if base else current

    if auto_flip and chuoi_thua >= 3:
        pred = "Tài" if str(pred).lower().startswith("x") else "Xỉu"

    return pred, confidence


# ================== HTML TEMPLATE ==================
HTML_PAGE = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dự đoán LC79MD5</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: linear-gradient(135deg, #1e3c72, #2a5298);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    color: #fff;
  }}
  .card {{
    background: rgba(255,255,255,0.08);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 18px;
    padding: 28px 26px;
    width: 100%;
    max-width: 420px;
    box-shadow: 0 20px 50px rgba(0,0,0,0.35);
  }}
  h1 {{
    font-size: 20px;
    text-align: center;
    margin-bottom: 22px;
    letter-spacing: .5px;
  }}
  .row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 11px 0;
    border-bottom: 1px solid rgba(255,255,255,0.12);
    font-size: 15px;
  }}
  .row:last-of-type {{ border-bottom: none; }}
  .label {{ color: #bcd2ff; }}
  .value {{ font-weight: 700; }}
  .du-doan {{
    font-size: 30px;
    text-align: center;
    margin: 18px 0 10px;
    padding: 14px;
    border-radius: 14px;
    background: rgba(0,0,0,0.25);
    letter-spacing: 2px;
  }}
  .tai {{ color: #ff7b7b; text-shadow: 0 0 14px rgba(255,90,90,.6); }}
  .xiu {{ color: #7bffb0; text-shadow: 0 0 14px rgba(90,255,160,.6); }}
  .credit {{
    text-align: center;
    margin-top: 20px;
    padding-top: 14px;
    border-top: 1px dashed rgba(255,255,255,0.25);
    font-size: 13px;
    color: #cfe0ff;
    font-style: italic;
  }}
  .error {{ color: #ffb0b0; text-align: center; }}
  .btn {{
    display: block;
    width: 100%;
    margin-top: 18px;
    padding: 12px;
    border: none;
    border-radius: 12px;
    background: linear-gradient(135deg, #43cea2, #185a9d);
    color: #fff;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    transition: transform .15s;
  }}
  .btn:hover {{ transform: translateY(-2px); }}
  .time {{ text-align:center; font-size:12px; color:#a9c0ea; margin-top:10px; }}
</style>
</head>
<body>
  <div class="card">
    <h1>🎯 Dự đoán LC79MD5</h1>
    {content}
    <div class="credit">{credit}</div>
  </div>
</body>
</html>
"""


def render_html(data):
    if "error" in data:
        content = f'<p class="error">Lỗi nguồn: {data["error"]}</p>'
        return HTML_PAGE.format(content=content, credit=CREDIT)

    pred, conf = predict_next(data)

    cls = "tai" if str(pred).lower().startswith("t") else "xiu"

    content = f"""
    <div class="du-doan {cls}">{pred}</div>
    <div class="row"><span class="label">Độ tin cậy</span><span class="value">{conf:.1f}%</span></div>
    <div class="row"><span class="label">Phiên hiện tại</span><span class="value">{data.get("phien", "-")}</span></div>
    <div class="row"><span class="label">Kết quả phiên trước</span><span class="value">{data.get("ket_qua", "-")}</span></div>
    <div class="row"><span class="label">Chuỗi thua</span><span class="value">{data.get("chuoi_thua", 0)}</span></div>
    <div class="row"><span class="label">Lịch sử</span><span class="value">{data.get("lich_su", "-")}</span></div>
    <div class="time">Cập nhật: {datetime.now().strftime("%H:%M:%S %d/%m/%Y")}</div>
    <button class="btn" onclick="location.reload()">🔄 Làm mới</button>
    """
    return HTML_PAGE.format(content=content, credit=CREDIT)


# ================== WORKER NỀN ==================
def background_worker():
    last_phien = None
    while True:
        try:
            data = fetch_source()
            if "error" in data:
                time.sleep(20)
                continue

            phien = data.get("phien")
            if phien and phien != last_phien:
                history = load_history()
                if not any(h.get("phien") == phien for h in history):
                    pred, conf = predict_next(data)
                    history.append({
                        "phien": phien,
                        "du_doan": pred,
                        "do_tin_cay": f"{conf:.1f}%",
                        "ket_qua_phien_truoc": data.get("ket_qua"),
                        "thoi_gian": datetime.now().isoformat(timespec="seconds"),
                    })
                    save_history(history)
                last_phien = phien
        except Exception:
            pass
        time.sleep(15)


# ================== ROUTES ==================
@app.after_request
def add_headers(resp):
    # Cho phép nhúng iframe + CORS, không bị chặn
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers.pop("X-Frame-Options", None)
    resp.headers["Content-Security-Policy"] = "frame-ancestors *"
    return resp


@app.route("/")
@app.route("/predict")
def predict_page():
    data = fetch_source()
    html = render_html(data)
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/raw")
def raw():
    """Xem JSON gốc từ nguồn (debug)."""
    data = fetch_source()
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8"
    )


@app.route("/history")
def history_page():
    h = load_history()
    rows = "".join(
        f'<div class="row"><span class="label">Phiên {x.get("phien")}</span>'
        f'<span class="value">{x.get("du_doan")} — {x.get("do_tin_cay")}</span></div>'
        for x in reversed(h[-30:])
    ) or '<p class="error">Chưa có lịch sử</p>'
    content = f'<h1 style="font-size:16px;margin-bottom:14px">📜 Lịch sử ({len(h)} phiên)</h1>{rows}'
    return Response(HTML_PAGE.format(content=content, credit=CREDIT), mimetype="text/html; charset=utf-8")


# ================== KHỞI ĐỘNG ==================
if __name__ == "__main__":
    threading.Thread(target=background_worker, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
