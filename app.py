import os
import json
import time
import threading
import requests
from flask import Flask, Response, request
from datetime import datetime
from collections import deque

app = Flask(__name__)

SOURCE_API = "https://wtxmd52.tele68.com/v1/txmd5/sessions?cp=R&cl=R&pf=web&at=897ccdd1391cdbb50091fe7cd3283dd7"
HISTORY_FILE = "history.json"
CREDIT = "chủ nguồn api @Duykhanhios"

lock = threading.Lock()

# ===== STATE TOÀN CỤC (real-time) =====
GLOBAL_HISTORY = []          # list các dict {session, dice, total, side}
MAX_GLOBAL_HISTORY = 2000
latest_api_session_id = None
latest_prediction = None      # dict dự đoán mới nhất
prediction_version = 0        # tăng mỗi khi có dự đoán mới -> SSE client biết có update
last_poll_ts = 0

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Referer": "https://tele68.com/",
    "Origin": "https://tele68.com",
    "Cache-Control": "no-store",
}


# ================== LỊCH SỬ FILE ==================
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


# ================== THUẬT TOÁN MỚI ==================
def _recent_weighted(recent, decay=1.0):
    """Weighted vote trên N phiên gần nhất, phiên càng mới weight càng cao."""
    tai_w = xiu_w = 0.0
    n = len(recent)
    for i, s in enumerate(recent):
        w = (i + 1) * (decay ** (n - 1 - i))  # càng mới càng nặng
        if s == "Tài":
            tai_w += w
        else:
            xiu_w += w
    return tai_w, xiu_w


def _streak_info(seq):
    """Trả về (side_last, streak_len)."""
    if not seq:
        return None, 0
    last = seq[-1]
    s = 1
    i = len(seq) - 2
    while i >= 0 and seq[i] == last:
        s += 1
        i -= 1
    return last, s


def _momentum(seq, short=5, long=15):
    """Chênh lệch tỉ lệ Tài giữa ngắn hạn và dài hạn (momentum)."""
    n = len(seq)
    if n < long:
        return 0.0
    p_short = seq[-short:].count("Tài") / short
    p_long = seq[-long:].count("Tài") / long
    return p_short - p_long


def _markov(seq, order=2, min_samples=3):
    """Markov bậc order — xác suất Tài sau context hiện tại."""
    n = len(seq)
    if n < order + min_samples:
        return 0.5
    ctx = seq[-order:]
    ct = cx = 0
    for i in range(n - order):
        if seq[i:i + order] == ctx:
            if seq[i + order] == "Tài":
                ct += 1
            else:
                cx += 1
    tot = ct + cx
    if tot < min_samples:
        return 0.5
    return (ct + 0.5) / (tot + 1)  # Laplace smoothing


def prediction(history):
    """
    Thuật toán dự đoán mới — kết hợp:
      1. Weighted recent 12 phiên (thuật toán gốc trong tt ngắn gọn)
      2. Streak analysis (bám cầu / bẻ cầu)
      3. Momentum 5 vs 15
      4. Markov bậc 2 & 3
      5. Mean-reversion khi lệch nhiều
    Trả về dict {side, conf, score_detail}
    """
    if not history or len(history) < 8:
        return {"side": "Đang chờ dữ liệu", "conf": 0, "detail": {}}

    seq = [h["side"] for h in history]  # "Tài" / "Xỉu"
    recent12 = seq[-12:]

    # 1) Weighted recent 12 (gốc)
    tai_w, xiu_w = _recent_weighted(recent12)
    tot_w = tai_w + xiu_w
    if tot_w == 0:
        p_weighted = 0.5
    else:
        p_weighted = tai_w / tot_w  # xác suất Tài
    edge = abs(tai_w - xiu_w) / tot_w if tot_w else 0

    # 2) Streak
    last_side, streak = _streak_info(seq)
    if streak <= 2:
        p_streak = 0.62 if last_side == "Tài" else 0.38   # bám cầu ngắn
    elif streak >= 7:
        p_streak = 0.30 if last_side == "Tài" else 0.70   # bẻ cầu dài
    else:
        p_streak = 0.52 if last_side == "Tài" else 0.48

    # 3) Momentum
    m = _momentum(seq, 5, 15)
    p_momentum = 0.5 + max(-0.18, min(0.18, m))

    # 4) Markov
    p_m2 = _markov(seq, 2, 3)
    p_m3 = _markov(seq, 3, 3)
    p_markov = (p_m2 * 0.6 + p_m3 * 0.4)

    # 5) Mean reversion (nếu 20 phiên lệch > 65%)
    last20 = seq[-20:]
    p20 = last20.count("Tài") / len(last20) if last20 else 0.5
    if p20 >= 0.68:
        p_mr = 0.36
    elif p20 <= 0.32:
        p_mr = 0.64
    else:
        p_mr = 0.5

    # Blend có trọng số
    # weighted: 0.30, streak: 0.18, momentum: 0.14, markov: 0.28, MR: 0.10
    p_tai = (
        p_weighted * 0.30
        + p_streak * 0.18
        + p_momentum * 0.14
        + p_markov * 0.28
        + p_mr * 0.10
    )

    side = "Tài" if p_tai >= 0.5 else "Xỉu"
    conf_raw = max(p_tai, 1 - p_tai) * 100

    # Boost theo edge + sample
    sample_factor = min(len(recent12) / 12, 1)
    conf = conf_raw + edge * 6 * sample_factor
    conf = round(min(82, max(50, conf)), 1)

    return {
        "side": side,
        "conf": conf,
        "detail": {
            "p_tai": round(p_tai * 100, 2),
            "p_weighted": round(p_weighted * 100, 2),
            "p_streak": round(p_streak * 100, 2),
            "p_momentum": round(p_momentum * 100, 2),
            "p_markov": round(p_markov * 100, 2),
            "p_mr": round(p_mr * 100, 2),
            "edge": round(edge * 100, 2),
            "streak": streak,
            "last_side": last_side,
        },
    }


# ================== API CALL ==================
def fetch_source():
    global last_poll_ts
    try:
        url = f"{SOURCE_API}&_t={int(time.time() * 1000)}"
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=8)
        r.raise_for_status()
        last_poll_ts = time.time()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def parse_sessions(data):
    """Chuẩn hoá list phiên từ API."""
    if "error" in data or "list" not in data:
        return []
    out = []
    for s in data["list"]:
        res = (s.get("resultTruyenThong") or "").upper()
        if res in ("TAI", "XIU"):
            out.append({
                "session": s.get("id"),
                "dice": s.get("dices", []),
                "total": s.get("point", 0),
                "side": "Tài" if res == "TAI" else "Xỉu",
            })
    # sort tăng dần theo id để lịch sử cũ -> mới
    out.sort(key=lambda x: x["session"] or 0)
    return out


# ================== WORKER POLL NHANH ==================
def realtime_worker():
    """
    Poll API mỗi ~1.2s. Khi phát hiện phiên mới (id thay đổi):
      - Cập nhật GLOBAL_HISTORY
      - Chạy prediction() trên lịch sử mới
      - Lưu latest_prediction + tăng prediction_version
      - Ghi vào file history
    """
    global latest_api_session_id, latest_prediction, prediction_version, GLOBAL_HISTORY

    last_phien = None
    while True:
        try:
            data = fetch_source()
            if "error" in data:
                time.sleep(2)
                continue

            sessions = parse_sessions(data)
            if not sessions:
                time.sleep(2)
                continue

            # Cập nhật lịch sử toàn cục
            GLOBAL_HISTORY = sessions[-MAX_GLOBAL_HISTORY:]
            newest = sessions[-1]
            newest_id = newest["session"]

            if newest_id != last_phien:
                # ==== PHIÊN MỚI ====
                last_phien = newest_id
                latest_api_session_id = newest_id

                pred = prediction(GLOBAL_HISTORY)
                if pred["side"] in ("Tài", "Xỉu"):
                    latest_prediction = {
                        "session": newest_id,
                        "predict_for_session": newest_id + 1,
                        "du_doan": "TAI" if pred["side"] == "Tài" else "XIU",
                        "side": pred["side"],
                        "do_tin_cay": pred["conf"],
                        "detail": pred["detail"],
                        "last_result": newest["side"],
                        "last_dice": newest["dice"],
                        "last_total": newest["total"],
                        "history_seq": [h["side"] for h in GLOBAL_HISTORY[-20:]],
                        "thoi_gian": datetime.now().isoformat(timespec="seconds"),
                        "poll_ts": time.time(),
                    }
                    prediction_version += 1

                    # Lưu file history
                    hist = load_history()
                    if not any(h.get("phien") == newest_id for h in hist):
                        hist.append({
                            "phien": newest_id,
                            "du_doan": latest_prediction["du_doan"],
                            "do_tin_cay": f"{pred['conf']:.1f}%",
                            "thuat_toan": "NEW_V3",
                            "chi_tiet": pred["detail"],
                            "ket_qua_phien": newest["side"],
                            "thoi_gian": datetime.now().isoformat(timespec="seconds"),
                        })
                        save_history(hist)

            # Poll nhanh khi chưa có phiên mới
            time.sleep(1.2)
        except Exception:
            time.sleep(2)


# ================== HTML ==================
HTML_PAGE = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🎯 TXMD5 AI Predictor — Live</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  :root {{
    --tai:#ff3d68; --tai-glow:rgba(255,61,104,.6);
    --xiu:#00e5a0; --xiu-glow:rgba(0,229,160,.6);
    --purple:#7b5cff; --blue:#00b4ff;
  }}
  body {{
    font-family:'Inter',sans-serif; min-height:100vh;
    background:#0a0e27;
    background-image:
      radial-gradient(at 20% 10%, rgba(123,92,255,.25) 0%, transparent 50%),
      radial-gradient(at 80% 20%, rgba(0,180,255,.20) 0%, transparent 50%),
      radial-gradient(at 50% 90%, rgba(255,61,104,.18) 0%, transparent 50%);
    display:flex; align-items:center; justify-content:center;
    padding:20px; color:#fff;
  }}
  .card {{
    width:100%; max-width:440px;
    background:rgba(20,25,55,.65); backdrop-filter:blur(24px);
    border-radius:28px; padding:30px 26px;
    border:1px solid rgba(255,255,255,.12);
    box-shadow:0 30px 80px rgba(0,0,0,.5), 0 0 60px rgba(123,92,255,.15);
  }}
  .header {{ text-align:center; margin-bottom:22px; }}
  .logo {{
    display:inline-flex; align-items:center; gap:8px;
    padding:7px 16px; background:rgba(255,255,255,.06);
    border:1px solid rgba(255,255,255,.1); border-radius:100px;
    font-size:12px; font-weight:600; letter-spacing:1px; color:#b8c4ff;
    margin-bottom:10px;
  }}
  .logo .dot {{
    width:8px; height:8px; border-radius:50%;
    background:var(--xiu); box-shadow:0 0 12px var(--xiu-glow);
    animation:pulse 1.4s infinite;
  }}
  @keyframes pulse {{ 0%,100%{{opacity:1;transform:scale(1)}} 50%{{opacity:.4;transform:scale(.8)}} }}
  h1 {{
    font-size:24px; font-weight:900;
    background:linear-gradient(135deg,#fff,#b8c4ff 60%,#7b5cff);
    -webkit-background-clip:text; background-clip:text;
    -webkit-text-fill-color:transparent;
  }}
  .prediction {{
    text-align:center; padding:28px 18px 24px; border-radius:22px;
    margin-bottom:18px; transition:all .35s; position:relative; overflow:hidden;
  }}
  .prediction.tai {{
    background:linear-gradient(135deg,rgba(255,61,104,.18),rgba(255,61,104,.05));
    border:1.5px solid rgba(255,61,104,.4);
    box-shadow:0 0 40px rgba(255,61,104,.25);
  }}
  .prediction.xiu {{
    background:linear-gradient(135deg,rgba(0,229,160,.18),rgba(0,229,160,.05));
    border:1.5px solid rgba(0,229,160,.4);
    box-shadow:0 0 40px rgba(0,229,160,.25);
  }}
  .pred-label {{ font-size:11px; font-weight:700; letter-spacing:3px; opacity:.7; margin-bottom:8px; }}
  .pred-value {{
    font-size:52px; font-weight:900; letter-spacing:4px;
    line-height:1; margin-bottom:8px;
    animation:pop .5s cubic-bezier(.3,1.5,.5,1);
  }}
  @keyframes pop {{ 0%{{transform:scale(.5);opacity:0}} 100%{{transform:scale(1);opacity:1}} }}
  .prediction.tai .pred-value {{ color:var(--tai); text-shadow:0 0 30px var(--tai-glow); }}
  .prediction.xiu .pred-value {{ color:var(--xiu); text-shadow:0 0 30px var(--xiu-glow); }}
  .conf-badge {{
    display:inline-flex; align-items:center; gap:6px;
    padding:6px 14px; background:rgba(255,255,255,.1);
    border-radius:100px; font-size:13px; font-weight:700;
  }}
  .live-tag {{
    display:inline-block; margin-left:8px; padding:3px 9px;
    background:rgba(0,229,160,.2); border:1px solid rgba(0,229,160,.5);
    color:#4dffc8; border-radius:100px; font-size:10px; font-weight:800;
    letter-spacing:1px;
  }}
  .live-tag.off {{ background:rgba(255,61,104,.15); border-color:rgba(255,61,104,.5); color:#ff7b9a; }}
  .info-grid {{ display:grid; gap:9px; margin-bottom:16px; }}
  .info-row {{
    display:flex; justify-content:space-between; align-items:center;
    padding:12px 15px; background:rgba(255,255,255,.04);
    border:1px solid rgba(255,255,255,.06); border-radius:13px; font-size:13px;
  }}
  .info-row .label {{ color:#8b96c9; display:flex; align-items:center; gap:7px; font-weight:500; }}
  .info-row .value {{ font-weight:700; }}
  .tag {{
    display:inline-block; padding:3px 9px; border-radius:100px;
    font-size:11px; font-weight:700; letter-spacing:.5px;
  }}
  .tag-version {{ background:linear-gradient(135deg,#7b5cff,#b84dff); color:#fff; }}
  .bars {{ display:grid; gap:10px; padding:14px; background:rgba(0,0,0,.2); border-radius:14px; margin-bottom:16px; }}
  .bar-head {{ display:flex; justify-content:space-between; font-size:11px; font-weight:600; margin-bottom:5px; color:#a8b3dd; }}
  .bar-track {{ height:7px; background:rgba(255,255,255,.08); border-radius:100px; overflow:hidden; }}
  .bar-fill {{ height:100%; border-radius:100px; transition:width .8s cubic-bezier(.2,.8,.2,1); }}
  .bar-tai {{ background:linear-gradient(90deg,#ff3d68,#ff9a3d); }}
  .bar-v1 {{ background:linear-gradient(90deg,#7b5cff,#b84dff); }}
  .bar-v2 {{ background:linear-gradient(90deg,#00b4ff,#00e5a0); }}
  .history-chips {{
    display:flex; flex-wrap:wrap; gap:5px; padding:12px 14px;
    background:rgba(0,0,0,.2); border-radius:14px; margin-bottom:14px;
  }}
  .chip {{
    width:30px; height:30px; display:flex; align-items:center; justify-content:center;
    border-radius:9px; font-size:10px; font-weight:800;
  }}
  .chip.t {{ background:rgba(255,61,104,.2); color:#ff7b9a; border:1px solid rgba(255,61,104,.4); }}
  .chip.x {{ background:rgba(0,229,160,.2); color:#4dffc8; border:1px solid rgba(0,229,160,.4); }}
  .btn {{
    width:100%; padding:14px; border:none; border-radius:14px;
    background:linear-gradient(135deg,#7b5cff,#b84dff 50%,#ff3d68);
    background-size:200% 200%; color:#fff; font-size:14px; font-weight:800;
    letter-spacing:1px; cursor:pointer; transition:all .3s;
    box-shadow:0 10px 30px rgba(123,92,255,.4);
  }}
  .btn:hover {{ background-position:100% 50%; transform:translateY(-2px); }}
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
      <div class="logo"><span class="dot"></span> AI PREDICTOR · LIVE v3</div>
      <h1>TXMD5 Predictor</h1>
    </div>
    <div id="app-content">{content}</div>
    <div class="credit">Powered by <strong>{credit}</strong></div>
  </div>
<script>
const clientVersion = {{version}};
let currentVersion = clientVersion;

async function refresh() {{
  try {{
    const r = await fetch('/api/live?since=' + currentVersion, {{cache:'no-store'}});
    const j = await r.json();
    if (j.version > currentVersion) {{
      currentVersion = j.version;
      document.getElementById('app-content').innerHTML = j.html;
      document.getElementById('live-dot')?.classList.add('on');
    }}
    document.getElementById('last-update').textContent = j.server_time || '';
  }} catch(e) {{}}
  setTimeout(refresh, 800);
}}
refresh();
</script>
</body>
</html>
"""


def render_content():
    """Render HTML fragment từ latest_prediction (không có thẻ html/body)."""
    global latest_prediction
    p = latest_prediction
    if not p:
        return '''
        <div class="error">
          <span class="emoji">⏳</span>
          Đang chờ phiên mới từ API...<br>
          <small style="opacity:.7">Hệ thống sẽ tự cập nhật khi có kết quả</small>
        </div>'''

    side = p["side"]
    cls = "tai" if side == "Tài" else "xiu"
    conf = p["do_tin_cay"]
    d = p["detail"]

    chips = "".join(
        f'<div class="chip {"t" if s=="Tài" else "x"}">{s[0]}</div>'
        for s in p["history_seq"][-20:]
    )

    return f'''
    <div class="prediction {cls}">
      <div class="pred-label">Dự đoán phiên {p["predict_for_session"]}</div>
      <div class="pred-value">{p["du_doan"]}</div>
      <div class="conf-badge">
        🎯 {conf:.1f}%
        <span class="live-tag" id="live-dot">● LIVE</span>
      </div>
    </div>
    <div class="info-grid">
      <div class="info-row"><span class="label">🔢 Phiên vừa xong</span><span class="value">{p["session"]}</span></div>
      <div class="info-row"><span class="label">🎲 Kết quả</span><span class="value">{side.upper() if False else ("TAI" if p["last_result"]=="Tài" else "XIU")} {p["last_dice"]} ({p["last_total"]}đ)</span></div>
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
    <div class="time" id="last-update">Cập nhật: {p["thoi_gian"]}</div>
    '''


def render_html():
    content = render_content()
    ver = prediction_version
    return HTML_PAGE.format(content=content, credit=CREDIT, version=ver)


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
    return Response(render_html(), mimetype="text/html; charset=utf-8")


@app.route("/api/live")
def api_live():
    """Endpoint cho frontend polling — trả về version + HTML fragment mới."""
    since = int(request.args.get("since", 0))
    if prediction_version > since or True:
        html = render_content()
    else:
        html = ""
    return Response(json.dumps({
        "version": prediction_version,
        "html": html,
        "server_time": datetime.now().strftime("%H:%M:%S • %d/%m/%Y"),
        "latest": latest_prediction,
    }, ensure_ascii=False), mimetype="application/json; charset=utf-8")


@app.route("/dudoan")
def dudoan():
    if not latest_prediction:
        return Response(json.dumps({
            "status": "waiting", "message": "Chưa có phiên mới"
        }, ensure_ascii=False, indent=2), mimetype="application/json; charset=utf-8")
    return Response(json.dumps({
        "status": "ok",
        "du_doan": latest_prediction["du_doan"],
        "do_tin_cay": f"{latest_prediction['do_tin_cay']:.1f}%",
        "phien": latest_prediction["session"],
        "du_doan_cho_phien": latest_prediction["predict_for_session"],
        "ket_qua_truoc": latest_prediction["last_result"],
        "thuat_toan": "NEW_V3",
        "chi_tiet": latest_prediction["detail"],
        "version": prediction_version,
    }, ensure_ascii=False, indent=2), mimetype="application/json; charset=utf-8")


@app.route("/stream")
def stream():
    """SSE — đẩy dự đoán mới ngay khi có phiên mới."""
    def gen():
        last_v = -1
        yield f"data: {json.dumps({'hello': True, 'version': prediction_version})}\n\n"
        while True:
            if prediction_version != last_v and latest_prediction:
                last_v = prediction_version
                payload = json.dumps(latest_prediction, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            time.sleep(0.8)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/history")
def history_page():
    h = load_history()
    rows = "".join(
        f'<div class="info-row"><span class="label">🔢 {x.get("phien")}</span>'
        f'<span class="value">{x.get("du_doan")} • {x.get("do_tin_cay")}</span></div>'
        for x in reversed(h[-30:])
    ) or '<p style="text-align:center;color:#6b76a8;padding:20px">Chưa có lịch sử</p>'
    content = f'''
    <div class="header" style="margin-bottom:18px">
      <h1 style="font-size:20px">📜 Lịch sử</h1>
      <p style="color:#8b96c9;font-size:12px;margin-top:6px">Tổng: {len(h)} phiên</p>
    </div>
    <div class="info-grid">{rows}</div>'''
    return Response(HTML_PAGE.format(content=content, credit=CREDIT, version=prediction_version),
                    mimetype="text/html; charset=utf-8")


# ================== START ==================
if __name__ == "__main__":
    threading.Thread(target=realtime_worker, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
