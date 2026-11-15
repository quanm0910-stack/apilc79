import os
import json
import time
import threading
import requests
from flask import Flask, Response, request
from datetime import datetime

app = Flask(__name__)

SOURCE_API = "https://wtxmd52.tele68.com/v1/txmd5/sessions?cp=R&cl=R&pf=web&at=897ccdd1391cdbb50091fe7cd3283dd7"
HISTORY_FILE = "history.json"
CREDIT = "chủ nguồn api @Duykhanhios"

lock = threading.Lock()

# ===== STATE TOÀN CỤC =====
GLOBAL_HISTORY = []
MAX_GLOBAL_HISTORY = 2000
latest_prediction = None
prediction_version = 0
api_error_msg = None 
is_fetching = False

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Referer": "https://tele68.com/",
    "Origin": "https://tele68.com",
    "Cache-Control": "no-store",
    "Connection": "keep-alive"
}

# ================== THUẬT TOÁN DỰ ĐOÁN V3 ==================
def _recent_weighted(recent, decay=1.0):
    tai_w = xiu_w = 0.0
    n = len(recent)
    for i, s in enumerate(recent):
        w = (i + 1) * (decay ** (n - 1 - i))
        if s == "Tài": tai_w += w
        else: xiu_w += w
    return tai_w, xiu_w

def _streak_info(seq):
    if not seq: return None, 0
    last = seq[-1]
    s = 1
    i = len(seq) - 2
    while i >= 0 and seq[i] == last:
        s += 1
        i -= 1
    return last, s

def _momentum(seq, short=5, long=15):
    n = len(seq)
    if n < long: return 0.0
    return (seq[-short:].count("Tài") / short) - (seq[-long:].count("Tài") / long)

def _markov(seq, order=2, min_samples=3):
    n = len(seq)
    if n < order + min_samples: return 0.5
    ctx = seq[-order:]
    ct = cx = 0
    for i in range(n - order):
        if seq[i:i + order] == ctx:
            if seq[i + order] == "Tài": ct += 1
            else: cx += 1
    tot = ct + cx
    return (ct + 0.5) / (tot + 1) if tot >= min_samples else 0.5

def prediction(history):
    if not history or len(history) < 8:
        return {"side": "Đang chờ", "conf": 0, "detail": {}}
    
    seq = [h["side"] for h in history]
    recent12 = seq[-12:]
    
    tai_w, xiu_w = _recent_weighted(recent12)
    tot_w = tai_w + xiu_w
    p_weighted = (tai_w / tot_w) if tot_w else 0.5
    edge = abs(tai_w - xiu_w) / tot_w if tot_w else 0

    last_side, streak = _streak_info(seq)
    if streak <= 2: p_streak = 0.62 if last_side == "Tài" else 0.38
    elif streak >= 7: p_streak = 0.30 if last_side == "Tài" else 0.70
    else: p_streak = 0.52 if last_side == "Tài" else 0.48

    m = _momentum(seq, 5, 15)
    p_momentum = 0.5 + max(-0.18, min(0.18, m))
    p_markov = (_markov(seq, 2, 3) * 0.6) + (_markov(seq, 3, 3) * 0.4)

    last20 = seq[-20:]
    p20 = last20.count("Tài") / len(last20) if last20 else 0.5
    p_mr = 0.36 if p20 >= 0.68 else (0.64 if p20 <= 0.32 else 0.5)

    p_tai = (p_weighted * 0.30) + (p_streak * 0.18) + (p_momentum * 0.14) + (p_markov * 0.28) + (p_mr * 0.10)
    side = "Tài" if p_tai >= 0.5 else "Xỉu"
    
    conf_raw = max(p_tai, 1 - p_tai) * 100
    sample_factor = min(len(recent12) / 12, 1)
    conf = round(min(82, max(50, conf_raw + edge * 6 * sample_factor)), 1)

    return {
        "side": side, "conf": conf,
        "detail": {
            "p_tai": round(p_tai * 100, 2), "p_weighted": round(p_weighted * 100, 2),
            "p_streak": round(p_streak * 100, 2), "p_momentum": round(p_momentum * 100, 2),
            "p_markov": round(p_markov * 100, 2), "p_mr": round(p_mr * 100, 2),
            "streak": streak, "last_side": last_side,
        }
    }

# ================== CALL API & UPDATE ==================
def fetch_and_update():
    global GLOBAL_HISTORY, latest_prediction, prediction_version, api_error_msg, is_fetching
    
    if is_fetching: return False
    is_fetching = True
    
    try:
        url = f"{SOURCE_API}&_t={int(time.time() * 1000)}"
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
        
        if r.status_code != 200:
            api_error_msg = f"Lỗi HTTP {r.status_code} (Bị chặn IP?)"
            print(f"[API ERROR] HTTP {r.status_code}")
            is_fetching = False
            return False

        data = r.json()
        if "list" not in data:
            api_error_msg = "API không trả về danh sách phiên."
            print(f"[API ERROR] Không có list: {str(data)[:100]}")
            is_fetching = False
            return False

        sessions = []
        for s in data.get("list", []):
            res = (s.get("resultTruyenThong") or "").upper()
            if res in ("TAI", "XIU"):
                sessions.append({
                    "session": s.get("id"), "dice": s.get("dices", []),
                    "total": s.get("point", 0), "side": "Tài" if res == "TAI" else "Xỉu",
                })
        sessions.sort(key=lambda x: x["session"] or 0)
        
        if not sessions:
            api_error_msg = "Không có dữ liệu TAI/XIU."
            is_fetching = False
            return False
            
        api_error_msg = None
        GLOBAL_HISTORY = sessions[-MAX_GLOBAL_HISTORY:]
        newest = sessions[-1]
        
        if not latest_prediction or latest_prediction["session"] != newest["session"]:
            pred = prediction(GLOBAL_HISTORY)
            if pred["side"] in ("Tài", "Xỉu"):
                latest_prediction = {
                    "session": newest["session"], "predict_for_session": newest["session"] + 1,
                    "du_doan": "TAI" if pred["side"] == "Tài" else "XIU", "side": pred["side"],
                    "do_tin_cay": pred["conf"], "detail": pred["detail"],
                    "last_result": newest["side"], "last_dice": newest["dice"], "last_total": newest["total"],
                    "history_seq": [h["side"] for h in GLOBAL_HISTORY[-20:]],
                    "thoi_gian": datetime.now().strftime("%H:%M:%S • %d/%m/%Y"),
                }
                prediction_version += 1
                
                # Ghi file lịch sử
                hist = []
                if os.path.exists(HISTORY_FILE):
                    try:
                        with open(HISTORY_FILE, "r", encoding="utf-8") as f: hist = json.load(f)
                    except: pass
                    
                if not any(h.get("phien") == newest["session"] for h in hist):
                    hist.append({
                        "phien": newest["session"], "du_doan": latest_prediction["du_doan"],
                        "do_tin_cay": f"{pred['conf']:.1f}%", "ket_qua_phien": newest["side"],
                        "thoi_gian": datetime.now().isoformat(timespec="seconds"),
                    })
                    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                        json.dump(hist, f, ensure_ascii=False, indent=2)
                
                print(f"[NEW SESSION] Phiên {newest['session']} -> Dự đoán: {latest_prediction['du_doan']}")
        
        is_fetching = False
        return True
    except Exception as e:
        api_error_msg = f"Lỗi mạng: {str(e)[:50]}"
        print(f"[EXCEPTION] {str(e)}")
        is_fetching = False
        return False

# ================== WORKER CHẠY NGẦM ==================
def realtime_worker():
    print("=== STARTING WORKER ===")
    while True:
        try:
            fetch_and_update()
        except Exception as e:
            print(f"[WORKER CRASH] {e}")
        time.sleep(1.5)

# ================== GIAO DIỆN HTML ==================
HTML_PAGE = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🎯 TXMD5 Predictor — Live</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  :root {{ --tai:#ff3d68; --xiu:#00e5a0; --purple:#7b5cff; --blue:#00b4ff; }}
  body {{
    font-family:'Inter',sans-serif; min-height:100vh; background:#0a0e27;
    background-image: radial-gradient(at 20% 10%, rgba(123,92,255,.25) 0%, transparent 50%),
                      radial-gradient(at 80% 20%, rgba(0,180,255,.20) 0%, transparent 50%),
                      radial-gradient(at 50% 90%, rgba(255,61,104,.18) 0%, transparent 50%);
    display:flex; align-items:center; justify-content:center; padding:20px; color:#fff;
  }}
  .card {{
    width:100%; max-width:440px; background:rgba(20,25,55,.65); backdrop-filter:blur(24px);
    border-radius:28px; padding:30px 26px; border:1px solid rgba(255,255,255,.12);
    box-shadow:0 30px 80px rgba(0,0,0,.5), 0 0 60px rgba(123,92,255,.15);
  }}
  .header {{ text-align:center; margin-bottom:22px; }}
  .logo {{
    display:inline-flex; align-items:center; gap:8px; padding:7px 16px; background:rgba(255,255,255,.06);
    border:1px solid rgba(255,255,255,.1); border-radius:100px; font-size:12px; font-weight:600; letter-spacing:1px; color:#b8c4ff; margin-bottom:10px;
  }}
  .logo .dot {{ width:8px; height:8px; border-radius:50%; background:var(--xiu); box-shadow:0 0 12px var(--xiu); animation:pulse 1.4s infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1;transform:scale(1)}} 50%{{opacity:.4;transform:scale(.8)}} }}
  h1 {{ font-size:24px; font-weight:900; background:linear-gradient(135deg,#fff,#b8c4ff 60%,#7b5cff); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }}
  .prediction {{ text-align:center; padding:28px 18px 24px; border-radius:22px; margin-bottom:18px; transition:all .35s; position:relative; overflow:hidden; }}
  .prediction.tai {{ background:linear-gradient(135deg,rgba(255,61,104,.18),rgba(255,61,104,.05)); border:1.5px solid rgba(255,61,104,.4); box-shadow:0 0 40px rgba(255,61,104,.25); }}
  .prediction.xiu {{ background:linear-gradient(135deg,rgba(0,229,160,.18),rgba(0,229,160,.05)); border:1.5px solid rgba(0,229,160,.4); box-shadow:0 0 40px rgba(0,229,160,.25); }}
  .pred-label {{ font-size:11px; font-weight:700; letter-spacing:3px; opacity:.7; margin-bottom:8px; }}
  .pred-value {{ font-size:52px; font-weight:900; letter-spacing:4px; line-height:1; margin-bottom:8px; animation:pop .5s cubic-bezier(.3,1.5,.5,1); }}
  @keyframes pop {{ 0%{{transform:scale(.5);opacity:0}} 100%{{transform:scale(1);opacity:1}} }}
  .prediction.tai .pred-value {{ color:var(--tai); text-shadow:0 0 30px rgba(255,61,104,.6); }}
  .prediction.xiu .pred-value {{ color:var(--xiu); text-shadow:0 0 30px rgba(0,229,160,.6); }}
  .conf-badge {{ display:inline-flex; align-items:center; gap:6px; padding:6px 14px; background:rgba(255,255,255,.1); border-radius:100px; font-size:13px; font-weight:700; }}
  .live-tag {{ display:inline-block; margin-left:8px; padding:3px 9px; background:rgba(0,229,160,.2); border:1px solid rgba(0,229,160,.5); color:#4dffc8; border-radius:100px; font-size:10px; font-weight:800; letter-spacing:1px; }}
  .info-grid {{ display:grid; gap:9px; margin-bottom:16px; }}
  .info-row {{ display:flex; justify-content:space-between; align-items:center; padding:12px 15px; background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.06); border-radius:13px; font-size:13px; }}
  .info-row .label {{ color:#8b96c9; display:flex; align-items:center; gap:7px; font-weight:500; }}
  .info-row .value {{ font-weight:700; }}
  .tag {{ display:inline-block; padding:3px 9px; border-radius:100px; font-size:11px; font-weight:700; letter-spacing:.5px; }}
  .tag-version {{ background:linear-gradient(135deg,#7b5cff,#b84dff); color:#fff; }}
  .bars {{ display:grid; gap:10px; padding:14px; background:rgba(0,0,0,.2); border-radius:14px; margin-bottom:16px; }}
  .bar-head {{ display:flex; justify-content:space-between; font-size:11px; font-weight:600; margin-bottom:5px; color:#a8b3dd; }}
  .bar-track {{ height:7px; background:rgba(255,255,255,.08); border-radius:100px; overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:100px; transition:width .8s cubic-bezier(.2,.8,.2,1); }}
  .bar-tai {{ background:linear-gradient(90deg,#ff3d68,#ff9a3d); }}
  .bar-v1 {{ background:linear-gradient(90deg,#7b5cff,#b84dff); }}
  .bar-v2 {{ background:linear-gradient(90deg,#00b4ff,#00e5a0); }}
  .history-chips {{ display:flex; flex-wrap:wrap; gap:5px; padding:12px 14px; background:rgba(0,0,0,.2); border-radius:14px; margin-bottom:14px; }}
  .chip {{ width:30px; height:30px; display:flex; align-items:center; justify-content:center; border-radius:9px; font-size:10px; font-weight:800; }}
  .chip.t {{ background:rgba(255,61,104,.2); color:#ff7b9a; border:1px solid rgba(255,61,104,.4); }}
  .chip.x {{ background:rgba(0,229,160,.2); color:#4dffc8; border:1px solid rgba(0,229,160,.4); }}
  .credit {{ text-align:center; margin-top:16px; padding-top:14px; border-top:1px solid rgba(255,255,255,.08); font-size:11px; color:#6b76a8; }}
  .credit strong {{ background:linear-gradient(135deg,#7b5cff,#ff3d68); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }}
  .time {{ text-align:center; font-size:11px; color:#4d5a8a; margin-top:10px; }}
  .error {{ text-align:center; padding:40px 20px; color:#ff7b9a; font-weight:600; }}
  .error .emoji {{ font-size:40px; display:block; margin-bottom:10px; }}
</style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="logo"><span class="dot"></span> AI PREDICTOR · LIVE v4</div>
      <h1>TXMD5 Predictor</h1>
    </div>
    <div id="app-content">{content}</div>
    <div class="credit">Powered by <strong>{credit}</strong></div>
  </div>
<script>
let currentVersion = {version};

async function refresh() {{
  try {{
    const r = await fetch('/api/live?since=' + currentVersion, {{cache:'no-store'}});
    const j = await r.json();
    if (j.version > currentVersion) {{
      currentVersion = j.version;
      document.getElementById('app-content').innerHTML = j.html;
    }}
  }} catch(e) {{}}
  setTimeout(refresh, 1000);
}}
refresh();
</script>
</body>
</html>
"""

def render_content():
    global latest_prediction, api_error_msg
    if api_error_msg:
        return f'''
        <div class="error">
          <span class="emoji">⚠️</span>
          Lỗi kết nối API<br><small style="opacity:.7">{api_error_msg}</small>
          <div style="margin-top:20px; font-size:12px; color:#8b96c9">Hãy thử tải lại trang (F5)</div>
        </div>'''
        
    p = latest_prediction
    if not p:
        return '''
        <div class="error">
          <span class="emoji">⏳</span>
          Đang kết nối tới API...<br>
          <small style="opacity:.7">Vui lòng đợi trong giây lát (Render có thể mất 50s để khởi động)</small>
        </div>'''

    side = p["side"]
    cls = "tai" if side == "Tài" else "xiu"
    conf = p["do_tin_cay"]
    d = p["detail"]
    chips = "".join(f'<div class="chip {"t" if s=="Tài" else "x"}">{s[0]}</div>' for s in p["history_seq"][-20:])
    res_text = "TAI" if p["last_result"] == "Tài" else "XIU"

    return f'''
    <div class="prediction {cls}">
      <div class="pred-label">Dự đoán phiên {p["predict_for_session"]}</div>
      <div class="pred-value">{p["du_doan"]}</div>
      <div class="conf-badge">🎯 {conf:.1f}% <span class="live-tag">● LIVE</span></div>
    </div>
    <div class="info-grid">
      <div class="info-row"><span class="label">🔢 Phiên vừa xong</span><span class="value">{p["session"]}</span></div>
      <div class="info-row"><span class="label">🎲 Kết quả</span><span class="value">{res_text} {p["last_dice"]} ({p["last_total"]}đ)</span></div>
      <div class="info-row"><span class="label">🧠 Thuật toán</span><span class="value"><span class="tag tag-version">NEW V3</span></span></div>
    </div>
    <div class="bars">
      <div class="bar-head"><span>🎲 P(TÀI)</span><span>{d["p_tai"]:.1f}%</span></div>
      <div class="bar-track"><div class="bar-fill bar-tai" style="width:{d["p_tai"]:.1f}%"></div></div>
      <div class="bar-head" style="margin-top:8px"><span>📊 Weighted 12</span><span>{d["p_weighted"]:.1f}%</span></div>
      <div class="bar-track"><div class="bar-fill bar-v1" style="width:{d["p_weighted"]:.1f}%"></div></div>
      <div class="bar-head" style="margin-top:8px"><span>🔗 Markov</span><span>{d["p_markov"]:.1f}%</span></div>
      <div class="bar-track"><div class="bar-fill bar-v2" style="width:{d["p_markov"]:.1f}%"></div></div>
    </div>
    <div class="history-chips">{chips}</div>
    <div class="time">Cập nhật: {p["thoi_gian"]}</div>
    '''

def render_html():
    return HTML_PAGE.format(content=render_content(), credit=CREDIT, version=prediction_version)

# ================== ROUTES ==================
@app.after_request
def add_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp

@app.route("/")
@app.route("/predict")
def predict_page():
    fetch_and_update()
    return Response(render_html(), mimetype="text/html; charset=utf-8")

@app.route("/api/live")
def api_live():
    if not latest_prediction:
        fetch_and_update()
    return Response(json.dumps({
        "version": prediction_version,
        "html": render_content(),
        "latest": latest_prediction
    }, ensure_ascii=False), mimetype="application/json; charset=utf-8")

@app.route("/dudoan")
def dudoan():
    if not latest_prediction:
        fetch_and_update()
    if not latest_prediction:
        return Response(json.dumps({"status": "waiting", "message": "Chưa có dữ liệu"}, ensure_ascii=False), mimetype="application/json; charset=utf-8")
    
    p = latest_prediction
    
    # 1. Chuẩn bị dữ liệu Xúc xắc (Ví dụ: 6-5-2)
    dice_list = p.get("last_dice", [])
    chi_tiet_str = "-".join(map(str, dice_list)) if dice_list else "N/A"
    
    # 2. Chuẩn bị dữ liệu Cầu Tài Xỉu (Dạng chuỗi: TXXTT...)
    history_seq = p.get("history_seq", [])
    cau_str = "".join("T" if s == "Tài" else "X" for s in history_seq)
    
    # 3. Tính Đúng / Sai của ván trước (Dựa vào file history.json)
    prev_status = "N/A"
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                hist = json.load(f)
                if len(hist) >= 1:
                    last_record = hist[-1]
                    # Nếu last_record là phiên vừa xong thì so sánh
                    if last_record.get("phien") == p["session"]:
                        # so sánh dự đoán của phiên trước đó với kết quả thực tế của phiên vừa xong
                        if last_record.get("du_doan") == ("TAI" if p["last_result"] == "Tài" else "XIU"):
                            prev_status = "ĐÚNG"
                        else:
                            prev_status = "SAI"
        except Exception:
            pass

    # 4. Trả về JSON chuẩn cho Bot
    return Response(json.dumps({
        "status": "ok", 
        "phien": p["predict_for_session"],
        "ket_qua": "TÀI" if p["last_result"] == "Tài" else "XỈU",
        "chi_tiet": chi_tiet_str,
        "tong": p["last_total"],
        "du_doan": p["du_doan"],
        "ti_le": round(p["do_tin_cay"], 1),
        "cau": cau_str,
        "van_truoc": prev_status
    }, ensure_ascii=False, indent=2), mimetype="application/json; charset=utf-8")

# ================== START ==================
if __name__ == "__main__":
    threading.Thread(target=realtime_worker, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
