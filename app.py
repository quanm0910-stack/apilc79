import os
import json
import time
import threading
import requests
from flask import Flask, Response, render_template_string
from datetime import datetime

app = Flask(__name__)

# ===== CẤU HÌNH API =====
SOURCE_API_MD5 = [
    "https://wtxmd52.tele68.com/v1/txmd5/sessions?cp=R&cl=R&pf=web&at=897ccdd1391cdbb50091fe7cd3283dd7",
]
SOURCE_API_THUONG = [
    "https://wtx.tele68.com/v1/tx/lite-sessions?cp=R&cl=R&pf=web&at=897ccdd1391cdbb50091fe7cd3283dd7",
]

HISTORY_FILE_MD5 = "history_md5.json"
HISTORY_FILE_THUONG = "history_thuong.json"
CREDIT = "chủ nguồn api @Duykhanhios"

lock = threading.Lock()
MAX_GLOBAL_HISTORY = 2000

STATE = {
    "thuong": {
        "history": [], "latest": None, "version": 0,
        "error": None, "apis": SOURCE_API_THUONG, "file": HISTORY_FILE_THUONG,
        "name": "BÀN THƯỜNG", "api_idx": 0,
    },
    "md5": {
        "history": [], "latest": None, "version": 0,
        "error": None, "apis": SOURCE_API_MD5, "file": HISTORY_FILE_MD5,
        "name": "BÀN MD5", "api_idx": 0,
    },
}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://tele68.com/",
    "Origin": "https://tele68.com",
}

# ================== THUẬT TOÁN ==================
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
    last = seq[-1]; s = 1; i = len(seq) - 2
    while i >= 0 and seq[i] == last:
        s += 1; i -= 1
    return last, s

def _momentum(seq, short=5, long=15):
    n = len(seq)
    if n < long: return 0.0
    return (seq[-short:].count("Tài") / short) - (seq[-long:].count("Tài") / long)

def _markov(seq, order=2, min_samples=3):
    n = len(seq)
    if n < order + min_samples: return 0.5
    ctx = seq[-order:]; ct = cx = 0
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

# ================== FETCH + FALLBACK ==================
def fetch_and_update(bang):
    st = STATE[bang]
    with lock:
        apis = st["apis"]
        for attempt in range(len(apis)):
            idx = (st["api_idx"] + attempt) % len(apis)
            try:
                url = f"{apis[idx]}&_t={int(time.time() * 1000)}"
                r = requests.get(url, headers=BROWSER_HEADERS, timeout=10)
                if r.status_code != 200: continue
                data = r.json()
                if "list" not in data: continue

                sessions = []
                for s in data.get("list", []):
                    res = (s.get("resultTruyenThong") or "").upper()
                    if res in ("TAI", "XIU"):
                        sessions.append({
                            "session": s.get("id"), "dice": s.get("dices", []),
                            "total": s.get("point", 0), "side": "Tài" if res == "TAI" else "Xỉu",
                        })
                sessions.sort(key=lambda x: x["session"] or 0)
                if not sessions: continue

                st["error"] = None
                st["api_idx"] = idx
                st["history"] = sessions[-MAX_GLOBAL_HISTORY:]
                newest = sessions[-1]

                if not st["latest"] or st["latest"]["session"] != newest["session"]:
                    pred = prediction(st["history"])
                    if pred["side"] in ("Tài", "Xỉu"):
                        st["latest"] = {
                            "session": newest["session"], "predict_for_session": newest["session"] + 1,
                            "du_doan": "TAI" if pred["side"] == "Tài" else "XIU", "side": pred["side"],
                            "do_tin_cay": pred["conf"], "detail": pred["detail"],
                            "last_result": newest["side"], "last_dice": newest["dice"], "last_total": newest["total"],
                            "history_seq": [h["side"] for h in st["history"][-20:]],
                            "thoi_gian": datetime.now().strftime("%H:%M:%S • %d/%m/%Y"),
                        }
                        st["version"] += 1

                        hist = []
                        if os.path.exists(st["file"]):
                            try:
                                with open(st["file"], "r", encoding="utf-8") as f: hist = json.load(f)
                            except: pass
                        if not any(h.get("phien") == newest["session"] for h in hist):
                            hist.append({
                                "phien": newest["session"], "du_doan": st["latest"]["du_doan"],
                                "ket_qua_phien": newest["side"],
                            })
                            with open(st["file"], "w", encoding="utf-8") as f:
                                json.dump(hist, f, ensure_ascii=False, indent=2)
                return True
            except Exception:
                continue
        st["error"] = "Tất cả API đều lỗi"
        return False

def realtime_worker():
    while True:
        try:
            fetch_and_update("thuong")
            fetch_and_update("md5")
        except Exception:
            pass
        time.sleep(0.5)

@app.after_request
def add_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

# ================== HELPERS ==================
def _stats(bang):
    st = STATE[bang]
    hist = []
    if os.path.exists(st["file"]):
        try:
            with open(st["file"], "r", encoding="utf-8") as f: hist = json.load(f)
        except: pass
    win = loss = 0
    streak_type = None; streak_len = 0
    results = []
    for i in range(len(hist) - 1):
        cur = hist[i]; nxt = hist[i+1]
        if nxt.get("phien") == (cur.get("phien") or 0) + 1:
            predicted = cur.get("du_doan")
            actual = "TAI" if nxt.get("ket_qua_phien") == "Tài" else "XIU"
            ok = (predicted == actual)
            results.append(ok)
            if ok:
                win += 1
                if streak_type in (None, True): streak_type = True; streak_len += 1
                else: streak_type = True; streak_len = 1
            else:
                loss += 1
                if streak_type in (None, False): streak_type = False; streak_len += 1
                else: streak_type = False; streak_len = 1
    total = win + loss
    rate = round(win / total * 100, 1) if total else 0
    return {
        "win": win, "loss": loss, "total": total, "rate": rate,
        "streak_type": "win" if streak_type else ("loss" if streak_type is False else None),
        "streak_len": streak_len,
        "recent": results[-10:],
    }

def _build_response(bang):
    st = STATE[bang]
    fetch_and_update(bang)
    if not st["latest"]:
        return {"status": "waiting", "bang": st["name"], "message": "Đang chờ dữ liệu..."}
    p = st["latest"]
    dice_list = p.get("last_dice", [])
    chi_tiet_str = "-".join(map(str, dice_list)) if dice_list else "N/A"
    history_seq = p.get("history_seq", [])
    cau_str = "".join("T" if s == "Tài" else "X" for s in history_seq)

    prev_status = "N/A"
    if os.path.exists(st["file"]):
        try:
            with open(st["file"], "r", encoding="utf-8") as f: hist = json.load(f)
            if len(hist) >= 1:
                last_record = hist[-1]
                if last_record.get("phien") == p["session"]:
                    prev_status = "ĐÚNG" if last_record.get("du_doan") == ("TAI" if p["last_result"] == "Tài" else "XIU") else "SAI"
        except Exception: pass

    return {
        "status": "ok", "bang": st["name"], "phien": p["predict_for_session"],
        "ket_qua": "TÀI" if p["last_result"] == "Tài" else "XỈU", "chi_tiet": chi_tiet_str,
        "tong": p["last_total"], "du_doan": p["du_doan"], "ti_le": round(p["do_tin_cay"], 1),
        "cau": cau_str, "van_truoc": prev_status, "thoi_gian": p["thoi_gian"],
        "credit": CREDIT, "version": p["session"], "stats": _stats(bang), "error": st["error"],
    }

# ================== ROUTES API ==================
@app.route("/dudoan")
def dudoan_thuong():
    return Response(json.dumps(_build_response("thuong"), ensure_ascii=False, indent=2), mimetype="application/json; charset=utf-8")

@app.route("/dudoan/md5")
def dudoan_md5():
    return Response(json.dumps(_build_response("md5"), ensure_ascii=False, indent=2), mimetype="application/json; charset=utf-8")

# ================== PWA MANIFEST & SW INLINE ==================
@app.route("/manifest.json")
def manifest():
    return Response(json.dumps({
        "name": "Tài Xỉu AI Premium", "short_name": "TX AI", "start_url": "/",
        "display": "standalone", "background_color": "#05010f", "theme_color": "#05010f",
        "icons": [{"src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎲</text></svg>", "sizes": "192x192", "type": "image/svg+xml"}]
    }), mimetype="application/json")

@app.route("/sw.js")
def sw():
    return Response("self.addEventListener('fetch', function(e) {});", mimetype="application/javascript")

# ================== GIAO DIỆN HTML ==================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Tài Xỉu AI • Premium</title>
<meta name="theme-color" content="#05010f">
<link rel="manifest" href="/manifest.json">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;800;900&family=Be+Vietnam+Pro:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg1:#05010f;--bg2:#0d0620;--card:rgba(20,12,40,.65);--border:rgba(168,85,247,.25);--tai:#ff2e63;--xiu:#00e5ff;--gold:#ffd166;--ok:#22c55e;--bad:#ef4444;--txt:#e2e8f0;--dim:#8b8ba7;}
[data-theme="light"]{--bg1:#f3f0ff;--bg2:#e0e7ff;--card:rgba(255,255,255,.75);--border:rgba(168,85,247,.25);--txt:#1e1b2e;--dim:#6b6b85;}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:'Be Vietnam Pro',system-ui,sans-serif;background:var(--bg1);color:var(--txt);min-height:100vh;padding:18px 14px 40px;display:flex;flex-direction:column;align-items:center;overflow-x:hidden;position:relative;transition:background .4s;}
body::before{content:'';position:fixed;inset:-20%;z-index:-2;background:radial-gradient(circle at 15% 20%, rgba(168,85,247,.35), transparent 45%),radial-gradient(circle at 85% 15%, rgba(0,229,255,.25), transparent 45%),radial-gradient(circle at 50% 90%, rgba(255,46,99,.25), transparent 50%),linear-gradient(180deg,var(--bg1),var(--bg2));filter:blur(40px);animation:auroraMove 18s ease-in-out infinite alternate;}
@keyframes auroraMove{0%{transform:translate(0,0) scale(1)}100%{transform:translate(-3%,2%) scale(1.08)}}
.container{width:100%;max-width:460px;position:relative;z-index:1;}

.top-controls{display:flex;justify-content:flex-end;gap:8px;margin-bottom:8px;}
.icon-btn{width:38px;height:38px;border-radius:12px;background:rgba(255,255,255,.06);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:16px;cursor:pointer;transition:.2s;backdrop-filter:blur(10px);color:var(--txt);}
.icon-btn:hover{background:rgba(255,255,255,.12);transform:translateY(-2px)}
.icon-btn.active{background:linear-gradient(135deg,#a855f7,#ff2e63);border-color:transparent}

.brand{text-align:center;margin-bottom:16px;}
.brand .crown{font-size:24px;filter:drop-shadow(0 0 12px var(--gold));animation:float 3s ease-in-out infinite;display:inline-block;}
@keyframes float{50%{transform:translateY(-6px)}}
.brand h1{font-family:'Orbitron',sans-serif;font-size:22px;font-weight:900;letter-spacing:3px;margin-top:2px;background:linear-gradient(90deg,#00e5ff 0%,#a855f7 45%,#ff2e63 100%);background-size:200% auto;-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;animation:shine 4s linear infinite;}
@keyframes shine{to{background-position:200% center}}
.brand p{font-size:10px;color:var(--dim);margin-top:4px;letter-spacing:1.5px;text-transform:uppercase;}

.tabs{display:flex;gap:6px;margin-bottom:14px;background:rgba(255,255,255,.04);border:1px solid var(--border);padding:6px;border-radius:16px;backdrop-filter:blur(12px);}
.tab{flex:1;padding:11px 10px;text-align:center;border-radius:12px;font-size:13px;font-weight:800;letter-spacing:.5px;cursor:pointer;transition:.3s;color:var(--dim);user-select:none;}
.tab.active{color:#fff;background:linear-gradient(135deg,#a855f7,#ff2e63);box-shadow:0 6px 22px rgba(168,85,247,.45);}

.card{background:var(--card);backdrop-filter:blur(20px);border:1px solid var(--border);border-radius:24px;padding:22px 20px;margin-bottom:14px;position:relative;overflow:hidden;box-shadow:0 20px 50px rgba(0,0,0,.5), inset 0 1px 0 rgba(255,255,255,.08);}
.card::before{content:'';position:absolute;top:0;left:-50%;width:200%;height:1px;background:linear-gradient(90deg,transparent,#00e5ff,#a855f7,#ff2e63,transparent);animation:scan 3s linear infinite;opacity:.7;}
@keyframes scan{to{transform:translateX(50%)}}

.pred-label{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:2px;text-align:center;margin-bottom:14px;font-weight:700;}
.pred-side{text-align:center;font-family:'Orbitron',sans-serif;font-size:64px;font-weight:900;line-height:1;letter-spacing:4px;transition:.35s;}
.pred-side.tai{color:var(--tai);text-shadow:0 0 20px rgba(255,46,99,.8),0 0 40px rgba(255,46,99,.5),0 0 80px rgba(255,46,99,.3);animation:taiPulse 1.6s ease-in-out infinite;}
.pred-side.xiu{color:var(--xiu);text-shadow:0 0 20px rgba(0,229,255,.8),0 0 40px rgba(0,229,255,.5),0 0 80px rgba(0,229,255,.3);animation:xiuPulse 1.6s ease-in-out infinite;}
@keyframes taiPulse{50%{text-shadow:0 0 30px rgba(255,46,99,1),0 0 60px rgba(255,46,99,.7),0 0 100px rgba(255,46,99,.5)}}
@keyframes xiuPulse{50%{text-shadow:0 0 30px rgba(0,229,255,1),0 0 60px rgba(0,229,255,.7),0 0 100px rgba(0,229,255,.5)}}
.pred-side.waiting{font-size:20px;color:var(--dim);letter-spacing:1px;font-family:'Be Vietnam Pro';font-weight:600;}

.conf-wrap{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:16px;}
.conf-chip{display:inline-flex;align-items:center;gap:10px;padding:8px 16px;border-radius:999px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);backdrop-filter:blur(10px);}
.conf-chip .lbl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;font-weight:700;}
.conf-chip .val{font-family:'Orbitron',sans-serif;font-size:15px;font-weight:800;color:var(--txt);}
.conf-bar{width:90px;height:5px;border-radius:3px;background:rgba(255,255,255,.12);overflow:hidden;}
.conf-fill{height:100%;width:0%;background:linear-gradient(90deg,#00e5ff,#a855f7,#ff2e63);border-radius:3px;transition:width .6s ease;box-shadow:0 0 12px rgba(168,85,247,.8);}

.grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:20px;}
.info{background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:11px 13px;transition:.25s;}
.info:hover{background:rgba(255,255,255,.06);border-color:rgba(168,85,247,.35);}
.info .k{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:4px;}
.info .v{font-size:15px;font-weight:800;font-family:'Orbitron',sans-serif;letter-spacing:.5px;}
.v.tai{color:var(--tai);text-shadow:0 0 10px rgba(255,46,99,.5);}
.v.xiu{color:var(--xiu);text-shadow:0 0 10px rgba(0,229,255,.5);}
.v.ok{color:var(--ok);text-shadow:0 0 10px rgba(34,197,94,.5);}
.v.bad{color:var(--bad);text-shadow:0 0 10px rgba(239,68,68,.5);}
.info .v.small{font-size:11px;font-family:'Be Vietnam Pro';letter-spacing:0;}

.cau-box{margin-top:20px;}
.cau-title{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:2px;font-weight:700;margin-bottom:10px;display:flex;align-items:center;gap:8px;}
.cau-title::before{content:'';width:3px;height:12px;border-radius:2px;background:linear-gradient(180deg,#00e5ff,#a855f7);}
.cau-list{display:flex;flex-wrap:wrap;gap:5px;}
.cau-cell{width:26px;height:26px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;font-family:'Orbitron',sans-serif;transition:.2s;}
.cau-cell.t{background:linear-gradient(135deg,rgba(255,46,99,.25),rgba(255,46,99,.1));color:var(--tai);border:1px solid rgba(255,46,99,.5);box-shadow:0 0 10px rgba(255,46,99,.3);}
.cau-cell.x{background:linear-gradient(135deg,rgba(0,229,255,.2),rgba(0,229,255,.08));color:var(--xiu);border:1px solid rgba(0,229,255,.5);box-shadow:0 0 10px rgba(0,229,255,.3);}

.stats-box{margin-top:20px;padding:14px;border-radius:16px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);}
.stats-title{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:2px;font-weight:700;margin-bottom:10px;display:flex;align-items:center;gap:8px;}
.stats-title::before{content:'';width:3px;height:12px;border-radius:2px;background:linear-gradient(180deg,#ffd166,#ff2e63);}
.stats-row{display:flex;gap:8px;margin-bottom:10px;}
.stat-pill{flex:1;text-align:center;padding:8px;border-radius:10px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);}
.stat-pill .num{font-family:'Orbitron',sans-serif;font-size:18px;font-weight:800;}
.stat-pill .lbl{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-top:2px;font-weight:700;}
.stat-pill.win .num{color:var(--ok);}
.stat-pill.loss .num{color:var(--bad);}
.stat-pill.rate .num{color:var(--gold);}
.streak-badge{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:999px;font-size:11px;font-weight:800;margin-bottom:8px;}
.streak-badge.win{background:rgba(34,197,94,.15);color:var(--ok);border:1px solid rgba(34,197,94,.4);}
.streak-badge.loss{background:rgba(239,68,68,.15);color:var(--bad);border:1px solid rgba(239,68,68,.4);}
.recent-list{display:flex;gap:4px;flex-wrap:wrap;}
.recent-cell{width:22px;height:22px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;}
.recent-cell.ok{background:rgba(34,197,94,.2);color:var(--ok);border:1px solid rgba(34,197,94,.5);}
.recent-cell.bad{background:rgba(239,68,68,.2);color:var(--bad);border:1px solid rgba(239,68,68,.5);}

.copy-btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;margin-top:14px;padding:12px;border-radius:14px;background:linear-gradient(135deg,rgba(168,85,247,.2),rgba(255,46,99,.2));border:1px solid rgba(168,85,247,.4);color:var(--txt);font-weight:800;font-size:13px;cursor:pointer;transition:.2s;font-family:'Be Vietnam Pro';}
.copy-btn:hover{background:linear-gradient(135deg,rgba(168,85,247,.35),rgba(255,46,99,.35));transform:translateY(-1px);}
.copy-btn:active{transform:scale(.98);}

.err{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.4);color:#fca5a5;padding:11px 14px;border-radius:12px;font-size:12px;margin-bottom:12px;font-weight:600;}
.foot{text-align:center;font-size:11px;color:var(--dim);margin-top:10px;line-height:1.8;}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);margin-right:6px;vertical-align:middle;box-shadow:0 0 10px var(--ok);animation:blink 1.4s infinite;}
@keyframes blink{50%{opacity:.25;box-shadow:0 0 2px var(--ok)}}
.credit{margin-top:8px;font-size:11px;color:var(--gold);letter-spacing:.5px;font-weight:700;text-shadow:0 0 10px rgba(255,209,102,.4);}

.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);background:rgba(20,12,40,.95);backdrop-filter:blur(20px);border:1px solid rgba(168,85,247,.4);color:#fff;padding:12px 22px;border-radius:14px;font-size:13px;font-weight:700;box-shadow:0 10px 30px rgba(0,0,0,.5);opacity:0;transition:.35s;z-index:9999;pointer-events:none;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
</style>
</head>
<body>
<div class="container">
  <div class="top-controls">
    <button class="icon-btn" id="soundBtn" onclick="toggleSound()" title="Âm thanh">🔊</button>
    <button class="icon-btn" id="notifBtn" onclick="toggleNotif()" title="Thông báo">🔔</button>
    <button class="icon-btn" id="themeBtn" onclick="toggleTheme()" title="Sáng/Tối">🌙</button>
  </div>

  <div class="brand">
    <div class="crown">👑</div>
    <h1>TÀI XỈU AI</h1>
    <p>Premium Prediction System v3.0</p>
  </div>

  <div class="tabs">
    <div class="tab active" data-bang="thuong" onclick="switchTab('thuong')">🎯 BÀN THƯỜNG</div>
    <div class="tab" data-bang="md5" onclick="switchTab('md5')">💎 BÀN MD5</div>
  </div>

  <div id="errorBox"></div>

  <div class="card">
    <div class="pred-label">▸ Dự Đoán Phiên Tiếp Theo ◂</div>
    <div id="predSide" class="pred-side waiting">Đang tải...</div>
    <div class="conf-wrap" id="confWrap" style="display:none">
      <div class="conf-chip">
        <span class="lbl">Độ tin cậy</span>
        <div class="conf-bar"><div class="conf-fill" id="confFill"></div></div>
        <span class="val" id="confVal">0%</span>
      </div>
    </div>

    <div class="grid">
      <div class="info"><div class="k">Phiên dự đoán</div><div class="v" id="phien">--</div></div>
      <div class="info"><div class="k">Kết quả trước</div><div class="v" id="ketqua">--</div></div>
      <div class="info"><div class="k">Xúc xắc</div><div class="v" id="xucxac">--</div></div>
      <div class="info"><div class="k">Tổng điểm</div><div class="v" id="tong">--</div></div>
      <div class="info"><div class="k">Ván trước</div><div class="v" id="vantruoc">--</div></div>
      <div class="info"><div class="k">Cập nhật</div><div class="v small" id="thoigian">--</div></div>
    </div>

    <div class="cau-box">
      <div class="cau-title">Cầu 20 phiên gần nhất</div>
      <div class="cau-list" id="cauList"></div>
    </div>

    <div class="stats-box">
      <div class="stats-title">Thống kê thuật toán</div>
      <div class="stats-row">
        <div class="stat-pill win"><div class="num" id="stWin">0</div><div class="lbl">Thắng</div></div>
        <div class="stat-pill loss"><div class="num" id="stLoss">0</div><div class="lbl">Thua</div></div>
        <div class="stat-pill rate"><div class="num" id="stRate">0%</div><div class="lbl">Tỉ lệ</div></div>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
        <div id="stStreak" class="streak-badge" style="display:none"></div>
        <div class="recent-list" id="stRecent"></div>
      </div>
    </div>

    <button class="copy-btn" onclick="copyResult()">📋 Copy Kết Quả Nhanh</button>
  </div>

  <div class="foot">
    <div><span class="dot"></span>Hệ thống hoạt động • Auto 1s</div>
    <div class="credit" id="credit"></div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
let currentBang = 'thuong';
let lastPhiens = {'thuong': null, 'md5': null};
let soundOn = localStorage.getItem('tx_sound') !== 'false';
let theme = localStorage.getItem('tx_theme') || 'dark';

// Audio Context setup
let audioCtx;
function playSound(type) {
  if (!soundOn) return;
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.connect(gain); gain.connect(audioCtx.destination);
    
    if (type === 'new') {
      osc.type = 'sine'; osc.frequency.setValueAtTime(880, audioCtx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(1200, audioCtx.currentTime + 0.1);
      gain.gain.setValueAtTime(0.1, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.1);
      osc.start(); osc.stop(audioCtx.currentTime + 0.1);
    } else if (type === 'win') {
      osc.type = 'triangle'; osc.frequency.setValueAtTime(600, audioCtx.currentTime);
      osc.frequency.setValueAtTime(800, audioCtx.currentTime + 0.1);
      osc.frequency.setValueAtTime(1000, audioCtx.currentTime + 0.2);
      gain.gain.setValueAtTime(0.1, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.3);
      osc.start(); osc.stop(audioCtx.currentTime + 0.3);
    }
  } catch(e) {}
}

// Setup UI state
function updateUIState() {
  document.getElementById('soundBtn').textContent = soundOn ? '🔊' : '🔇';
  document.getElementById('soundBtn').classList.toggle('active', soundOn);
  document.documentElement.setAttribute('data-theme', theme);
  document.getElementById('themeBtn').textContent = theme === 'dark' ? '🌙' : '☀️';
}
updateUIState();

function toggleSound() {
  soundOn = !soundOn;
  localStorage.setItem('tx_sound', soundOn);
  updateUIState();
  if (soundOn) playSound('new');
}
function toggleTheme() {
  theme = theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('tx_theme', theme);
  updateUIState();
}
function toggleNotif() {
  if (!("Notification" in window)) {
    showToast("Trình duyệt không hỗ trợ thông báo"); return;
  }
  if (Notification.permission === "granted") {
    showToast("Thông báo đã bật sẵn sàng!");
    document.getElementById('notifBtn').classList.add('active');
  } else if (Notification.permission !== "denied") {
    Notification.requestPermission().then(p => {
      if (p === "granted") {
        document.getElementById('notifBtn').classList.add('active');
        showToast("Đã bật thông báo đẩy!");
      }
    });
  } else {
    showToast("Bạn đã chặn thông báo trên trình duyệt");
  }
}
if ("Notification" in window && Notification.permission === "granted") {
  document.getElementById('notifBtn').classList.add('active');
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

// Copy function
function copyResult() {
  const p = document.getElementById('predSide').textContent;
  const ph = document.getElementById('phien').textContent;
  const conf = document.getElementById('confVal').textContent;
  if (p === 'Đang tải...' || p === 'Đang chờ...') return;
  const text = `Phiên ${ph} | ${p} | Độ tin cậy: ${conf} | Bàn: ${currentBang.toUpperCase()}`;
  navigator.clipboard.writeText(text).then(() => {
    showToast('✅ Đã copy: ' + text);
  });
}

function switchTab(bang) {
  if (bang === currentBang) return;
  currentBang = bang;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.bang === bang));
  const p = document.getElementById('predSide');
  p.className = 'pred-side waiting';
  p.textContent = 'Đang tải...';
  document.getElementById('confWrap').style.display = 'none';
  fetchData();
}

async function fetchData() {
  const url = currentBang === 'md5' ? '/dudoan/md5' : '/dudoan';
  try {
    const r = await fetch(url);
    const d = await r.json();
    render(d);
  } catch(e) {
    document.getElementById('errorBox').innerHTML = '<div class="err">⚠️ Không kết nối được server</div>';
  }
}

function render(d) {
  document.getElementById('errorBox').innerHTML = d.error ? `<div class="err">⚠️ ${d.error}</div>` : '';

  if (d.status !== 'ok') {
    const p = document.getElementById('predSide');
    p.className = 'pred-side waiting';
    p.textContent = d.message || 'Đang chờ dữ liệu...';
    return;
  }

  // Detect new session
  if (lastPhiens[currentBang] !== null && lastPhiens[currentBang] !== d.phien) {
    playSound('new');
    if (d.van_truoc === 'ĐÚNG') {
      playSound('win');
      showToast('🎉 Chúc mừng! Ván trước dự đoán ĐÚNG');
    }
    if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
      new Notification("Tài Xỉu AI - Phiên mới " + d.phien, {
        body: `Dự đoán: ${d.du_doan} | Tin cậy: ${d.ti_le}%`,
        icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🎲</text></svg>'
      });
    }
  }
  lastPhiens[currentBang] = d.phien;

  // Main prediction
  const p = document.getElementById('predSide');
  p.textContent = d.du_doan === 'TAI' ? 'TÀI' : 'XỈU';
  p.className = 'pred-side ' + (d.du_doan === 'TAI' ? 'tai' : 'xiu');

  document.getElementById('confWrap').style.display = 'flex';
  document.getElementById('confVal').textContent = d.ti_le + '%';
  document.getElementById('confFill').style.width = d.ti_le + '%';

  document.getElementById('phien').textContent = '#' + d.phien;
  const kq = document.getElementById('ketqua');
  kq.textContent = d.ket_qua;
  kq.className = 'v ' + (d.ket_qua === 'TÀI' ? 'tai' : 'xiu');

  document.getElementById('xucxac').textContent = d.chi_tiet;
  document.getElementById('tong').textContent = d.tong;

  const vt = document.getElementById('vantruoc');
  vt.textContent = d.van_truoc;
  vt.className = 'v ' + (d.van_truoc === 'ĐÚNG' ? 'ok' : (d.van_truoc === 'SAI' ? 'bad' : ''));

  document.getElementById('thoigian').textContent = (d.thoi_gian || '--').split(' • ')[0];
  document.getElementById('credit').textContent = '👑 ' + (d.credit || '');

  const cauStr = d.cau || '';
  document.getElementById('cauList').innerHTML = cauStr.split('').map(c =>
    `<div class="cau-cell ${c === 'T' ? 't' : 'x'}">${c}</div>`
  ).join('');

  // Stats
  if (d.stats) {
    document.getElementById('stWin').textContent = d.stats.win || 0;
    document.getElementById('stLoss').textContent = d.stats.loss || 0;
    document.getElementById('stRate').textContent = (d.stats.rate || 0) + '%';
    
    const stStreak = document.getElementById('stStreak');
    if (d.stats.streak_type && d.stats.streak_len > 0) {
      stStreak.style.display = 'inline-flex';
      stStreak.className = `streak-badge ${d.stats.streak_type}`;
      stStreak.textContent = d.stats.streak_type === 'win' 
        ? `🔥 Chuỗi thắng: ${d.stats.streak_len}` 
        : `❄️ Chuỗi thua: ${d.stats.streak_len}`;
    } else {
      stStreak.style.display = 'none';
    }

    document.getElementById('stRecent').innerHTML = (d.stats.recent || []).map(r =>
      `<div class="recent-cell ${r ? 'ok' : 'bad'}">${r ? '✅' : '❌'}</div>`
    ).join('');
  }
}

// PWA Setup
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(()=>{}));
}

fetchData();
setInterval(fetchData, 1000);
</script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML_TEMPLATE)

if __name__ == "__main__":
    threading.Thread(target=realtime_worker, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
