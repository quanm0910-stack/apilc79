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


def get_seq(data):
    if "error" in data or "list" not in data:
        return []
    rows = []
    for item in data["list"]:
        res = item.get("resultTruyenThong")
        if res in ("TAI", "XIU"):
            rows.append(res)
    rows.reverse()
    return rows[-40:]


# ================== THUẬT TOÁN (giữ nguyên) ==================
def v1_ctw(seq):
    n = len(seq)
    if n < 10:
        return 0.5
    best_p, best_n = 0.5, 0
    for d in range(min(6, n - 1), 0, -1):
        ctx = "|".join(seq[-d:])
        ct = cx = 0
        for i in range(n - d):
            if "|".join(seq[i:i + d]) != ctx:
                continue
            if seq[i + d] == "TAI":
                ct += 1
            else:
                cx += 1
        tot = ct + cx
        if tot >= 3 and tot > best_n:
            best_p = (ct + 0.5) / (tot + 1)
            best_n = tot
    return best_p


def v1_pattern(seq):
    n = len(seq)
    if n < 8:
        return 0.5
    best_p, best_l = 0.5, 0
    for L in range(2, min(6, n - 1) + 1):
        ctx = "|".join(seq[-L:])
        ct = cx = 0
        for i in range(n - L):
            if "|".join(seq[i:i + L]) != ctx:
                continue
            if seq[i + L] == "TAI":
                ct += 1
            else:
                cx += 1
        tot = ct + cx
        if tot >= 3 and L > best_l:
            best_p = (ct + 0.5) / (tot + 1)
            best_l = L
    return best_p


def v1_streak(seq):
    n = len(seq)
    if not n:
        return 0.5
    last = seq[-1]
    s = 1
    i = n - 2
    while i >= 0 and seq[i] == last:
        s += 1
        i -= 1
    if s == 1:
        return 0.55 if last == "TAI" else 0.45

    signals_theo = signals_be = same_theo = same_be = 0
    i = 1
    while i < n - s:
        cur = seq[i]
        blen = 1
        j = i - 1
        while j >= 0 and seq[j] == cur:
            blen += 1
            j -= 1
        if blen == s and i + 1 < n:
            if seq[i + 1] == cur:
                same_theo += 1
            else:
                same_be += 1
        i += 1

    total_hist = same_theo + same_be
    if total_hist >= 3:
        if same_theo > same_be * 1.3:
            signals_theo += 2
        elif same_be > same_theo * 1.3:
            signals_be += 2

    if n >= 5:
        last5 = seq[-5:]
        if len(set(last5)) == 1:
            signals_theo += 1
        elif last5.count(last5[0]) == 4:
            signals_be += 1

    last10 = seq[-10:]
    p = last10.count("TAI") / len(last10)
    if p >= 0.7 or p <= 0.3:
        signals_theo += 1
    elif 0.45 <= p <= 0.55:
        signals_be += 1

    if s <= 3:
        signals_theo += 2
    elif 7 <= s <= 9:
        signals_be += 1
    elif s >= 10:
        signals_be += 3

    if signals_theo > signals_be:
        conf = 0.55 + min(0.10, (signals_theo - signals_be) * 0.03)
        return conf if last == "TAI" else 1 - conf
    if signals_be > signals_theo:
        conf = 0.50 + min(0.15, (signals_be - signals_theo) * 0.03)
        return 1 - conf if last == "TAI" else conf
    return 0.55 if last == "TAI" else 0.45


def v1_freq(seq, w=10):
    r = seq[-w:]
    return r.count("TAI") / len(r) if r else 0.5


def v1_entropy(seq, w=10):
    r = seq[-w:]
    if not r:
        return 0.5
    p = r.count("TAI") / len(r)
    if p == 0 or p == 1:
        return p
    import math
    H = -(p * math.log2(p) + (1 - p) * math.log2(1 - p))
    return 0.5 if H >= 0.9 else p


def v1_recent_transition(seq):
    n = len(seq)
    if n < 6:
        return 0.5
    r = seq[-10:]
    tt = tx = xx = xt = 0
    for i in range(1, len(r)):
        a, b = r[i - 1], r[i]
        if a == "TAI" and b == "TAI":
            tt += 1
        elif a == "TAI" and b == "XIU":
            tx += 1
        elif a == "XIU" and b == "XIU":
            xx += 1
        elif a == "XIU" and b == "TAI":
            xt += 1
    last = r[-1]
    same = tt if last == "TAI" else xx
    flip = tx if last == "TAI" else xt
    total = same + flip
    if total < 3:
        return 0.5
    p = same / total
    return (p * 0.55 + (1 - p) * 0.45) if last == "TAI" else ((1 - p) * 0.55 + p * 0.45)


def v1_cau(seq):
    n = len(seq)
    if n < 8:
        return 0.5
    last = seq[-1]
    sigs = []
    s = 1
    i = n - 2
    while i >= 0 and seq[i] == last:
        s += 1
        i -= 1

    l7 = seq[-7:]
    changes = sum(1 for i in range(1, len(l7)) if l7[i] != l7[i - 1])

    if n >= 6:
        l6 = seq[-6:]
        if l6[0] == l6[1] and l6[2] == l6[3] and l6[4] == l6[5] \
                and l6[0] != l6[2] and l6[2] != l6[4]:
            sigs.append((l6[4], 2.5))
    if n >= 6:
        l6 = seq[-6:]
        if l6[0] == l6[1] == l6[2] and l6[3] == l6[4] == l6[5] and l6[0] != l6[3]:
            sigs.append(("XIU" if l6[3] == "TAI" else "TAI", 2.3))
    if changes >= 5:
        sigs.append(("XIU" if last == "TAI" else "TAI", 2.5))
    if 2 <= s <= 4:
        sigs.append((last, 1.8))
    elif s >= 8:
        sigs.append(("XIU" if last == "TAI" else "TAI", 2.8))

    if not sigs:
        return 0.5
    st = sum(w for side, w in sigs if side == "TAI")
    sx = sum(w for side, w in sigs if side == "XIU")
    return st / (st + sx) if (st + sx) else 0.5


def algo_v1(seq):
    signals = [v1_ctw(seq), v1_pattern(seq), v1_streak(seq), v1_freq(seq),
               v1_entropy(seq), v1_cau(seq), v1_recent_transition(seq)]
    weights = [1.5, 1.5, 2.0, 1.0, 0.8, 2.3, 1.2]
    return sum(s * w for s, w in zip(signals, weights)) / sum(weights)


def v2_markov2(seq):
    n = len(seq)
    if n < 20:
        return 0.5
    ctx = "|".join(seq[-2:])
    ct = cx = 0
    for i in range(n - 2):
        if "|".join(seq[i:i + 2]) != ctx:
            continue
        if seq[i + 2] == "TAI":
            ct += 1
        else:
            cx += 1
    tot = ct + cx
    return (ct + 0.5) / (tot + 1) if tot >= 3 else 0.5


def v2_markov3(seq):
    n = len(seq)
    if n < 30:
        return 0.5
    ctx = "|".join(seq[-3:])
    ct = cx = 0
    for i in range(n - 3):
        if "|".join(seq[i:i + 3]) != ctx:
            continue
        if seq[i + 3] == "TAI":
            ct += 1
        else:
            cx += 1
    tot = ct + cx
    return (ct + 0.5) / (tot + 1) if tot >= 3 else 0.5


def v2_cycle(seq):
    n = len(seq)
    if n < 20:
        return 0.5
    for period in (2, 3, 4):
        if n < period * 3:
            continue
        matches = total = 0
        for i in range(n - period * 3, n - period):
            if i < 0:
                continue
            if seq[i] == seq[i + period]:
                matches += 1
            total += 1
        if total >= period * 2 and matches / total >= 0.75:
            return 0.62 if seq[n - 1 - period] == "TAI" else 0.38
    return 0.5


def v2_momentum(seq):
    n = len(seq)
    if n < 20:
        return 0.5
    p5 = seq[-5:].count("TAI") / 5
    p15 = seq[-15:].count("TAI") / 15
    d = p5 - p15
    if d > 0.20:
        return 0.65
    if d < -0.20:
        return 0.35
    if d > 0.10:
        return 0.58
    if d < -0.10:
        return 0.42
    return 0.5


def v2_mean_reversion(seq):
    n = len(seq)
    if n < 20:
        return 0.5
    p = seq[-20:].count("TAI") / 20
    if p >= 0.65:
        return 0.35
    if p <= 0.35:
        return 0.65
    return 0.5


def algo_v2(seq):
    signals = [v2_markov2(seq), v2_markov3(seq), v2_cycle(seq),
               v2_momentum(seq), v2_mean_reversion(seq)]
    weights = [1.8, 2.0, 1.5, 1.5, 1.5]
    return sum(s * w for s, w in zip(signals, weights)) / sum(weights)


def analyze(seq, v1_acc=None, v2_acc=None):
    v1_acc = v1_acc or []
    v2_acc = v2_acc or []
    p_v1 = algo_v1(seq)
    p_v2 = algo_v2(seq)

    def avg(a):
        return sum(a) / len(a) if a else 0.5

    v1_score = avg(v1_acc[-5:]) * 0.6 + avg(v1_acc[-15:]) * 0.4
    v2_score = avg(v2_acc[-5:]) * 0.6 + avg(v2_acc[-15:]) * 0.4
    v1_conf = abs(p_v1 - 0.5) * 2
    v2_conf = abs(p_v2 - 0.5) * 2
    both_agree = (p_v1 >= 0.5) == (p_v2 >= 0.5)

    if len(v1_acc) < 10 and len(v2_acc) < 10:
        p_tai, version = p_v1, "V1"
    elif len(v1_acc) < 10:
        p_tai, version = p_v2, "V2"
    elif len(v2_acc) < 10:
        p_tai, version = p_v1, "V1"
    elif both_agree:
        if v1_score > v2_score:
            w1, w2 = 0.65, 0.35
        elif v2_score > v1_score:
            w1, w2 = 0.35, 0.65
        else:
            w1, w2 = 0.5, 0.5
        p_tai = p_v1 * w1 + p_v2 * w2
        version = "BLEND"
    elif v1_score - v2_score > 0.05:
        p_tai, version = p_v1, "V1"
    elif v2_score - v1_score > 0.05:
        p_tai, version = p_v2, "V2"
    elif v1_conf > v2_conf:
        p_tai, version = p_v1, "V1"
    elif v2_conf > v1_conf:
        p_tai, version = p_v2, "V2"
    else:
        p_tai, version = (p_v1 + p_v2) / 2, "BLEND"

    predict = "TAI" if p_tai >= 0.5 else "XIU"
    conf = max(p_tai, 1 - p_tai) * 100
    if both_agree:
        conf = min(80, conf + 5)

    if conf >= 55:
        tier = "STRONG"
    elif conf >= 52:
        predict = "XIU" if predict == "TAI" else "TAI"
        p_tai = 1 - p_tai
        conf = max(p_tai, 1 - p_tai) * 100
        tier = "CONTRARIAN"
    else:
        return None

    return {
        "predict": predict,
        "confidence": min(80, conf) / 100,
        "p_tai": p_tai * 100,
        "p_v1": p_v1 * 100,
        "p_v2": p_v2 * 100,
        "version": version,
        "tier": tier,
    }


def predict_next(data):
    seq = get_seq(data)
    if len(seq) < 8:
        return None, None, None
    out = analyze(seq)
    if not out:
        return None, None, None
    return out["predict"], out["confidence"] * 100, out


# ================== HTML TEMPLATE (MỚI) ==================
HTML_PAGE = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🎯 TXMD5 AI Predictor</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  :root {{
    --tai: #ff3d68;
    --tai-glow: rgba(255,61,104,0.6);
    --xiu: #00e5a0;
    --xiu-glow: rgba(0,229,160,0.6);
    --purple: #7b5cff;
    --blue: #00b4ff;
  }}

  body {{
    font-family: 'Inter', -apple-system, sans-serif;
    min-height: 100vh;
    background: #0a0e27;
    background-image:
      radial-gradient(at 20% 10%, rgba(123,92,255,0.25) 0%, transparent 50%),
      radial-gradient(at 80% 20%, rgba(0,180,255,0.20) 0%, transparent 50%),
      radial-gradient(at 50% 90%, rgba(255,61,104,0.18) 0%, transparent 50%);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    color: #fff;
    overflow-x: hidden;
    position: relative;
  }}

  /* Hạt sáng nền */
  body::before {{
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      radial-gradient(2px 2px at 20% 30%, rgba(255,255,255,0.4), transparent),
      radial-gradient(2px 2px at 60% 70%, rgba(255,255,255,0.3), transparent),
      radial-gradient(1px 1px at 40% 50%, rgba(255,255,255,0.5), transparent),
      radial-gradient(2px 2px at 90% 10%, rgba(255,255,255,0.35), transparent),
      radial-gradient(1px 1px at 10% 90%, rgba(255,255,255,0.3), transparent);
    background-size: 200% 200%;
    animation: stars 8s ease-in-out infinite;
    pointer-events: none;
    z-index: 0;
  }}
  @keyframes stars {{
    0%, 100% {{ background-position: 0% 0%; opacity: .7; }}
    50% {{ background-position: 100% 100%; opacity: 1; }}
  }}

  .card {{
    position: relative;
    z-index: 1;
    width: 100%;
    max-width: 440px;
    background: rgba(20, 25, 55, 0.65);
    backdrop-filter: blur(24px) saturate(180%);
    -webkit-backdrop-filter: blur(24px) saturate(180%);
    border-radius: 28px;
    padding: 32px 28px;
    border: 1px solid rgba(255,255,255,0.12);
    box-shadow:
      0 30px 80px rgba(0,0,0,0.5),
      0 0 0 1px rgba(255,255,255,0.05) inset,
      0 0 60px rgba(123,92,255,0.15);
    animation: fadeUp .6s cubic-bezier(.2,.8,.2,1);
  }}
  @keyframes fadeUp {{
    from {{ opacity: 0; transform: translateY(20px) scale(.97); }}
    to {{ opacity: 1; transform: translateY(0) scale(1); }}
  }}

  /* Header */
  .header {{
    text-align: center;
    margin-bottom: 26px;
  }}
  .logo {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 8px 18px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 100px;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 1px;
    color: #b8c4ff;
    margin-bottom: 12px;
  }}
  .logo .dot {{
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--xiu);
    box-shadow: 0 0 12px var(--xiu-glow);
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: .5; transform: scale(.85); }}
  }}
  h1 {{
    font-size: 26px;
    font-weight: 900;
    background: linear-gradient(135deg, #fff 0%, #b8c4ff 60%, #7b5cff 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    letter-spacing: -0.5px;
  }}

  /* Dự đoán chính */
  .prediction {{
    position: relative;
    text-align: center;
    padding: 30px 20px 26px;
    border-radius: 24px;
    margin-bottom: 22px;
    overflow: hidden;
    transition: all .3s;
  }}
  .prediction.tai {{
    background: linear-gradient(135deg, rgba(255,61,104,0.18), rgba(255,61,104,0.05));
    border: 1.5px solid rgba(255,61,104,0.4);
    box-shadow: 0 0 40px rgba(255,61,104,0.25), inset 0 0 30px rgba(255,61,104,0.08);
  }}
  .prediction.xiu {{
    background: linear-gradient(135deg, rgba(0,229,160,0.18), rgba(0,229,160,0.05));
    border: 1.5px solid rgba(0,229,160,0.4);
    box-shadow: 0 0 40px rgba(0,229,160,0.25), inset 0 0 30px rgba(0,229,160,0.08);
  }}
  .prediction::before {{
    content: '';
    position: absolute;
    inset: -50%;
    background: conic-gradient(from 0deg, transparent, rgba(255,255,255,0.1), transparent 30%);
    animation: rotate 4s linear infinite;
  }}
  @keyframes rotate {{
    to {{ transform: rotate(360deg); }}
  }}
  .prediction .inner {{ position: relative; z-index: 1; }}
  .pred-label {{
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 3px;
    text-transform: uppercase;
    opacity: .7;
    margin-bottom: 8px;
  }}
  .pred-value {{
    font-size: 56px;
    font-weight: 900;
    letter-spacing: 4px;
    line-height: 1;
    margin-bottom: 8px;
    animation: pop .5s cubic-bezier(.3,1.5,.5,1);
  }}
  @keyframes pop {{
    0% {{ transform: scale(.5); opacity: 0; }}
    100% {{ transform: scale(1); opacity: 1; }}
  }}
  .prediction.tai .pred-value {{
    color: var(--tai);
    text-shadow: 0 0 30px var(--tai-glow), 0 0 60px var(--tai-glow);
  }}
  .prediction.xiu .pred-value {{
    color: var(--xiu);
    text-shadow: 0 0 30px var(--xiu-glow), 0 0 60px var(--xiu-glow);
  }}
  .conf-badge {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    background: rgba(255,255,255,0.1);
    border-radius: 100px;
    font-size: 13px;
    font-weight: 700;
    backdrop-filter: blur(10px);
  }}
  .conf-badge .icon {{ font-size: 14px; }}

  /* Rows thông tin */
  .info-grid {{
    display: grid;
    gap: 10px;
    margin-bottom: 18px;
  }}
  .info-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 13px 16px;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 14px;
    font-size: 14px;
    transition: all .2s;
  }}
  .info-row:hover {{
    background: rgba(255,255,255,0.08);
    border-color: rgba(123,92,255,0.3);
    transform: translateX(3px);
  }}
  .info-row .label {{
    color: #8b96c9;
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 500;
  }}
  .info-row .value {{
    font-weight: 700;
    color: #fff;
  }}
  .tag {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 100px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .5px;
  }}
  .tag-strong {{
    background: linear-gradient(135deg, #00e5a0, #00b4ff);
    color: #001a14;
  }}
  .tag-contra {{
    background: linear-gradient(135deg, #ffb800, #ff7b00);
    color: #2a1500;
  }}
  .tag-version {{
    background: linear-gradient(135deg, #7b5cff, #b84dff);
    color: #fff;
  }}

  /* Progress bars */
  .bars {{
    display: grid;
    gap: 12px;
    padding: 16px;
    background: rgba(0,0,0,0.2);
    border-radius: 16px;
    margin-bottom: 18px;
  }}
  .bar-item {{ }}
  .bar-head {{
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    font-weight: 600;
    margin-bottom: 6px;
    color: #a8b3dd;
  }}
  .bar-track {{
    height: 8px;
    background: rgba(255,255,255,0.08);
    border-radius: 100px;
    overflow: hidden;
  }}
  .bar-fill {{
    height: 100%;
    border-radius: 100px;
    transition: width 1s cubic-bezier(.2,.8,.2,1);
  }}
  .bar-v1 {{ background: linear-gradient(90deg, #7b5cff, #b84dff); }}
  .bar-v2 {{ background: linear-gradient(90deg, #00b4ff, #00e5a0); }}
  .bar-tai {{ background: linear-gradient(90deg, #ff3d68, #ff9a3d); }}

  /* Lịch sử */
  .history-chips {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 14px 16px;
    background: rgba(0,0,0,0.2);
    border-radius: 16px;
    margin-bottom: 18px;
  }}
  .chip {{
    width: 32px; height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .5px;
    transition: transform .2s;
  }}
  .chip:hover {{ transform: scale(1.15); }}
  .chip.t {{ background: rgba(255,61,104,0.2); color: #ff7b9a; border: 1px solid rgba(255,61,104,0.4); }}
  .chip.x {{ background: rgba(0,229,160,0.2); color: #4dffc8; border: 1px solid rgba(0,229,160,0.4); }}

  /* Button */
  .btn {{
    width: 100%;
    padding: 15px;
    border: none;
    border-radius: 16px;
    background: linear-gradient(135deg, #7b5cff 0%, #b84dff 50%, #ff3d68 100%);
    background-size: 200% 200%;
    color: #fff;
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 1px;
    cursor: pointer;
    transition: all .3s;
    box-shadow: 0 10px 30px rgba(123,92,255,0.4);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }}
  .btn:hover {{
    background-position: 100% 50%;
    transform: translateY(-2px);
    box-shadow: 0 15px 40px rgba(123,92,255,0.6);
  }}
  .btn:active {{ transform: translateY(0); }}

  /* Footer */
  .credit {{
    text-align: center;
    margin-top: 20px;
    padding-top: 16px;
    border-top: 1px solid rgba(255,255,255,0.08);
    font-size: 12px;
    color: #6b76a8;
    font-weight: 500;
  }}
  .credit strong {{
    background: linear-gradient(135deg, #7b5cff, #ff3d68);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight: 800;
  }}

  .error {{
    text-align: center;
    padding: 40px 20px;
    color: #ff7b9a;
    font-weight: 600;
  }}
  .error .emoji {{ font-size: 40px; display: block; margin-bottom: 12px; }}

  .time {{
    text-align: center;
    font-size: 11px;
    color: #4d5a8a;
    margin-top: 12px;
    font-weight: 500;
  }}

  /* Nút phụ */
  .nav-links {{
    display: flex;
    gap: 8px;
    margin-top: 14px;
  }}
  .nav-links a {{
    flex: 1;
    text-align: center;
    padding: 11px;
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
    color: #a8b3dd;
    text-decoration: none;
    font-size: 13px;
    font-weight: 600;
    transition: all .2s;
  }}
  .nav-links a:hover {{
    background: rgba(123,92,255,0.15);
    border-color: rgba(123,92,255,0.4);
    color: #fff;
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="logo">
        <span class="dot"></span>
        AI PREDICTOR v2.0
      </div>
      <h1>TXMD5 Predictor</h1>
    </div>
    {content}
    <div class="credit">Powered by <strong>{credit}</strong></div>
  </div>
</body>
</html>
"""


def render_html(data):
    if "error" in data:
        content = f'''
        <div class="error">
          <span class="emoji">⚠️</span>
          Lỗi nguồn dữ liệu<br><small style="opacity:.7">{data["error"]}</small>
        </div>'''
        return HTML_PAGE.format(content=content, credit=CREDIT)

    pred, conf, meta = predict_next(data)
    if pred is None:
        content = '''
        <div class="error">
          <span class="emoji">⏳</span>
          Chưa đủ dữ liệu để dự đoán<br>
          <small style="opacity:.7">Cần ≥ 8 phiên và độ tin cậy ≥ 52%</small>
        </div>'''
        return HTML_PAGE.format(content=content, credit=CREDIT)

    cls = "tai" if pred == "TAI" else "xiu"
    rows = data.get("list", [])
    last = rows[0] if rows else {}
    phien = last.get("id", "-")
    ket_qua = last.get("resultTruyenThong", "-")
    dices = last.get("dices", [])
    point = last.get("point", "-")

    seq = get_seq(data)
    chips = "".join(
        f'<div class="chip {"t" if s=="TAI" else "x"}">{s[0]}</div>'
        for s in seq[-20:]
    )

    tier_tag = "tag-strong" if meta["tier"] == "STRONG" else "tag-contra"
    tier_text = "🔥 STRONG" if meta["tier"] == "STRONG" else "🔄 CONTRARIAN"

    content = f'''
    <div class="prediction {cls}">
      <div class="inner">
        <div class="pred-label">Dự đoán phiên tiếp</div>
        <div class="pred-value">{pred}</div>
        <div class="conf-badge">
          <span class="icon">🎯</span>
          Độ tin cậy: {conf:.1f}%
        </div>
      </div>
    </div>

    <div class="info-grid">
      <div class="info-row">
        <span class="label">🔢 Phiên hiện tại</span>
        <span class="value">{phien}</span>
      </div>
      <div class="info-row">
        <span class="label">🎲 Kết quả trước</span>
        <span class="value">{ket_qua} {dices} ({point}đ)</span>
      </div>
      <div class="info-row">
        <span class="label">🧠 Thuật toán</span>
        <span class="value"><span class="tag tag-version">{meta["version"]}</span></span>
      </div>
      <div class="info-row">
        <span class="label">⚡ Tín hiệu</span>
        <span class="value"><span class="tag {tier_tag}">{tier_text}</span></span>
      </div>
    </div>

    <div class="bars">
      <div class="bar-item">
        <div class="bar-head"><span>🎲 Xác suất TÀI</span><span>{meta["p_tai"]:.1f}%</span></div>
        <div class="bar-track"><div class="bar-fill bar-tai" style="width:{meta["p_tai"]:.1f}%"></div></div>
      </div>
      <div class="bar-item">
        <div class="bar-head"><span>🧩 V1 (Cổ điển)</span><span>{meta["p_v1"]:.1f}%</span></div>
        <div class="bar-track"><div class="bar-fill bar-v1" style="width:{meta["p_v1"]:.1f}%"></div></div>
      </div>
      <div class="bar-item">
        <div class="bar-head"><span>🚀 V2 (Hiện đại)</span><span>{meta["p_v2"]:.1f}%</span></div>
        <div class="bar-track"><div class="bar-fill bar-v2" style="width:{meta["p_v2"]:.1f}%"></div></div>
      </div>
    </div>

    <div class="history-chips">{chips}</div>

    <button class="btn" onclick="location.reload()">🔄 LÀM MỚI DỰ ĐOÁN</button>

    <div class="nav-links">
      <a href="/history">📜 Lịch sử</a>
      <a href="/dudoan">🔗 JSON API</a>
    </div>

    <div class="time">Cập nhật: {datetime.now().strftime("%H:%M:%S • %d/%m/%Y")}</div>
    '''
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
            rows = data.get("list", [])
            if not rows:
                time.sleep(15)
                continue
            phien = rows[0].get("id")
            if phien and phien != last_phien:
                pred, conf, meta = predict_next(data)
                if pred is not None:
                    history = load_history()
                    if not any(h.get("phien") == phien for h in history):
                        history.append({
                            "phien": phien,
                            "du_doan": pred,
                            "do_tin_cay": f"{conf:.1f}%",
                            "thuat_toan": meta["version"],
                            "tier": meta["tier"],
                            "ket_qua_phien_truoc": rows[0].get("resultTruyenThong"),
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
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers.pop("X-Frame-Options", None)
    resp.headers["Content-Security-Policy"] = "frame-ancestors *"
    return resp


@app.route("/dudoan")
def dudoan():
    data = fetch_source()
    if "error" in data:
        return Response(
            json.dumps({"status": "error", "message": data["error"]}, ensure_ascii=False, indent=2),
            mimetype="application/json; charset=utf-8"
        ), 500

    pred, conf, meta = predict_next(data)
    if pred is None:
        return Response(
            json.dumps({"status": "no_data", "message": "Chưa đủ dữ liệu hoặc conf < 52%"}, ensure_ascii=False, indent=2),
            mimetype="application/json; charset=utf-8"
        ), 200

    rows = data.get("list", [])
    last = rows[0] if rows else {}

    phien = last.get("id")
    history = load_history()
    if phien and not any(h.get("phien") == phien for h in history):
        history.append({
            "phien": phien,
            "du_doan": pred,
            "do_tin_cay": f"{conf:.1f}%",
            "thuat_toan": meta["version"],
            "tier": meta["tier"],
            "ket_qua_phien_truoc": last.get("resultTruyenThong"),
            "thoi_gian": datetime.now().isoformat(timespec="seconds"),
        })
        save_history(history)

    payload = {
        "du_doan": pred,
        "do_tin_cay": f"{conf:.1f}%",
        "phien": phien,
        "ket_qua_truoc": last.get("resultTruyenThong"),
        "thuat_toan": meta["version"],
        "tier": meta["tier"],
        "p_tai": round(meta["p_tai"], 2),
        "p_v1": round(meta["p_v1"], 2),
        "p_v2": round(meta["p_v2"], 2),
        "lich_su": " ".join(get_seq(data)[-10:]),
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8"
    )


@app.route("/")
@app.route("/predict")
def predict_page():
    data = fetch_source()
    return Response(render_html(data), mimetype="text/html; charset=utf-8")


@app.route("/history")
def history_page():
    h = load_history()
    chips = "".join(
        f'<div class="chip {"t" if x.get("du_doan")=="TAI" else "x"}">{x.get("du_doan","")[0]}</div>'
        for x in reversed(h[-40:])
    ) or '<p style="color:#6b76a8;text-align:center;padding:20px">Chưa có lịch sử</p>'

    rows_html = "".join(
        f'<div class="info-row">'
        f'<span class="label">🔢 Phiên {x.get("phien")}</span>'
        f'<span class="value">{x.get("du_doan")} • {x.get("do_tin_cay")}</span>'
        f'</div>'
        for x in reversed(h[-30:])
    ) or ''

    content = f'''
    <div class="header" style="margin-bottom:20px">
      <h1 style="font-size:22px">📜 Lịch sử dự đoán</h1>
      <p style="color:#8b96c9;font-size:13px;margin-top:8px">Tổng: {len(h)} phiên</p>
    </div>
    <div class="history-chips" style="margin-bottom:20px">{chips}</div>
    <div class="info-grid">{rows_html}</div>
    <div class="nav-links">
      <a href="/">🏠 Trang chủ</a>
      <a href="/dudoan">🔗 JSON</a>
    </div>
    '''
    return Response(HTML_PAGE.format(content=content, credit=CREDIT), mimetype="text/html; charset=utf-8")


# ================== KHỞI ĐỘNG ==================
if __name__ == "__main__":
    threading.Thread(target=background_worker, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
