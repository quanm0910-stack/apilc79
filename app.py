import os
import json
import time
import threading
import requests
from flask import Flask, Response
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

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://tele68.com/",
    "Origin": "https://tele68.com",
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
    global GLOBAL_HISTORY, latest_prediction, prediction_version, api_error_msg
    
    with lock:
        try:
            url = f"{SOURCE_API}&_t={int(time.time() * 1000)}"
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=10)
            
            if r.status_code != 200:
                api_error_msg = f"Lỗi HTTP {r.status_code}"
                return False

            data = r.json()
            if "list" not in data:
                api_error_msg = "API không trả về danh sách phiên."
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
                return False
                
            api_error_msg = None
            GLOBAL_HISTORY = sessions[-MAX_GLOBAL_HISTORY:]
            newest = sessions[-1]
            
            # Bắt buộc cập nhật nếu sang phiên mới
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
                    
                    # Ghi file lịch sử để check đúng/sai
                    hist = []
                    if os.path.exists(HISTORY_FILE):
                        try:
                            with open(HISTORY_FILE, "r", encoding="utf-8") as f: hist = json.load(f)
                        except: pass
                        
                    if not any(h.get("phien") == newest["session"] for h in hist):
                        hist.append({
                            "phien": newest["session"], "du_doan": latest_prediction["du_doan"],
                            "ket_qua_phien": newest["side"],
                        })
                        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                            json.dump(hist, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            api_error_msg = f"Lỗi mạng: {str(e)[:50]}"
            return False

# ================== WORKER CHẠY NGẦM (TỐI ƯU 0.5S) ==================
def realtime_worker():
    while True:
        try:
            fetch_and_update()
        except Exception:
            pass
        time.sleep(0.5) # 🚀 Check liên tục mỗi 0.5s

@app.after_request
def add_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

@app.route("/dudoan")
def dudoan():
    # 🚀 BẮT BUỘC FETCH MỚI NGAY KHI CÓ REQUEST (Fix lỗi chậm phiên)
    fetch_and_update()
    
    if not latest_prediction:
        return Response(json.dumps({"status": "waiting", "message": "Đang chờ dữ liệu..."}, ensure_ascii=False), mimetype="application/json; charset=utf-8")
    
    p = latest_prediction
    
    dice_list = p.get("last_dice", [])
    chi_tiet_str = "-".join(map(str, dice_list)) if dice_list else "N/A"
    
    history_seq = p.get("history_seq", [])
    cau_str = "".join("T" if s == "Tài" else "X" for s in history_seq)
    
    prev_status = "N/A"
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                hist = json.load(f)
                if len(hist) >= 1:
                    last_record = hist[-1]
                    if last_record.get("phien") == p["session"]:
                        if last_record.get("du_doan") == ("TAI" if p["last_result"] == "Tài" else "XIU"):
                            prev_status = "ĐÚNG"
                        else:
                            prev_status = "SAI"
        except Exception:
            pass

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

# Trang chủ mặc định (nếu bạn cần)
@app.route("/")
def home():
    return "API đang chạy ổn định!"

if __name__ == "__main__":
    threading.Thread(target=realtime_worker, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
