import os, json, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from iqoptionapi.stable_api import IQ_Option

TOKEN = os.environ["TG_TOKEN"]
CHANNEL = os.environ["TG_CHANNEL"]
EMAIL = os.environ["IQ_EMAIL"]
PASSWORD = os.environ["IQ_PASS"]

PAIRS = ["EURUSD-OTC", "GBPUSD-OTC", "EURJPY-OTC", "USDJPY-OTC",
         "EURGBP-OTC", "USDCHF-OTC", "GBPJPY-OTC", "AUDCAD-OTC"]
TIMEFRAME = 120          # 2 minute candles
EXPIRY_MIN = 2
MAX_SIGNALS = 15
MIN_GAP = 30 * 60        # seconds between signals
MIN_LEAD = 180           # entry time is at least 3 minutes away
START_HOUR = 8
END_MINUTES = 20 * 60 + 30   # last signal at 8:30pm Lagos time
SUMMARY_HOUR = 21            # summary from 9pm Lagos time

LAGOS = ZoneInfo("Africa/Lagos")
now_dt = datetime.now(LAGOS)
today = now_dt.strftime("%Y-%m-%d")
EVENT = os.environ.get("GITHUB_EVENT_NAME", "")
print("event:", EVENT)

def send(text):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHANNEL, "text": text})
    print("telegram:", r.status_code, r.text[:150])

if EVENT == "workflow_dispatch":
    send("Test: the bot is connected 😊")

try:
    with open("state.json") as f:
        state = json.load(f)
except Exception:
    state = {"signals": [], "summary_day": ""}

cutoff = time.time() - 7 * 86400
state["signals"] = [s for s in state["signals"] if s["ts"] > cutoff]

def save():
    with open("state.json", "w") as f:
        json.dump(state, f)

api = IQ_Option(EMAIL, PASSWORD)
ok, reason = api.connect()
if not ok:
    print("IQ Option login failed:", reason)
    raise SystemExit(1)
api.change_balance("PRACTICE")

def get_df(pair):
    c = api.get_candles(pair, TIMEFRAME, 120, time.time())
    df = pd.DataFrame(c).rename(columns={"min": "low", "max": "high"})
    return df.iloc[:-1]

def analyze(df):
    close = df["close"]
    fast = EMAIndicator(close, 9).ema_indicator()
    slow = EMAIndicator(close, 21).ema_indicator()
    rsi = RSIIndicator(close, 14).rsi().iloc[-1]
    bull = [df["close"].iloc[-i] > df["open"].iloc[-i] for i in (1, 2)]
    bear = [df["close"].iloc[-i] < df["open"].iloc[-i] for i in (1, 2)]
    call = sum([fast.iloc[-1] > slow.iloc[-1], all(bull),
                50 < rsi < 68, close.iloc[-1] > fast.iloc[-1]])
    put = sum([fast.iloc[-1] < slow.iloc[-1], all(bear),
               32 < rsi < 50, close.iloc[-1] < fast.iloc[-1]])
    if call >= 4: return "CALL"
    if put >= 4: return "PUT"
    return None

def settle():
    for s in state["signals"]:
        if s["result"] is not None or "levels" not in s:
            continue
        # levels: 0 = direct entry, 1 to 3 = Martingale levels
        while len(s["levels"]) < 4:
            k = len(s["levels"])
            lvl_entry = s["entry_ts"] + TIMEFRAME * k
            lvl_end = lvl_entry + TIMEFRAME
            if lvl_end > time.time() - 60:
                break
            try:
                c = api.get_candles(s["pair"], TIMEFRAME, 5, lvl_end + 60)
                m = [x for x in c if x["from"] == lvl_entry]
                if not m:
                    break
                o, cl = m[0]["open"], m[0]["close"]
            except Exception as e:
                print("settle error", e)
                break
            if cl == o: r = "DRAW"
            elif (cl > o) == (s["direction"] == "CALL"): r = "WIN"
            else: r = "LOSS"
            s["levels"].append(r)
            if r == "WIN":
                s["result"] = "WIN"
                s["win_level"] = k
                break
            if r == "DRAW":
                s["result"] = "DRAW"
                break
            if k == 3:
                s["result"] = "LOSS"

def scan():
    minutes_now = now_dt.hour * 60 + now_dt.minute
    if not (START_HOUR * 60 <= minutes_now <= END_MINUTES):
        return
    todays = [s for s in state["signals"] if s["day"] == today]
    if len(todays) >= MAX_SIGNALS:
        return
    if todays and time.time() - max(s["ts"] for s in todays) < MIN_GAP:
        return
    for pair in PAIRS:
        try:
            direction = analyze(get_df(pair))
        except Exception as e:
            print("scan error", pair, e)
            continue
        if direction:
            now = int(time.time())
            entry_ts = (now // TIMEFRAME + 1) * TIMEFRAME
            if entry_ts - now < MIN_LEAD:
                entry_ts += TIMEFRAME
            state["signals"].append({"ts": now, "day": today, "pair": pair,
                "direction": direction, "entry_ts": entry_ts,
                "levels": [], "win_level": None, "result": None})
            def fmt(ts):
                return datetime.fromtimestamp(ts, LAGOS).strftime("%I:%M %p")
            if direction == "CALL":
                word, arrow, dot = "BUY (UP)", "📈", "🟢"
            else:
                word, arrow, dot = "SELL (DOWN)", "📉", "🔴"
            send(f"⚡ LASER AI VIP SIGNAL ⚡\n\n"
                 f"🔥 SIGNAL {len(todays)+1}/{MAX_SIGNALS} 🔥\n\n"
                 f"💱 Pair: {pair}\n"
                 f"{arrow} Direction: {word} {dot}\n"
                 f"⏰ Enter at: {fmt(entry_ts)} (Nigerian time)\n"
                 f"⌛ Expiry: {EXPIRY_MIN} minutes\n\n"
                 f"📊 Martingale Levels:\n"
                 f"• Level 1 → {fmt(entry_ts + TIMEFRAME)}\n"
                 f"• Level 2 → {fmt(entry_ts + 2 * TIMEFRAME)}\n"
                 f"• Level 3 → {fmt(entry_ts + 3 * TIMEFRAME)}\n\n"
                 f"Be ready 1 minute before entry. Good luck 😊\n"
                 f"⚠️ Trade responsibly. Never risk more than you can afford to lose.")
            return

def summary():
    rows = [s for s in state["signals"] if s["day"] == today]
    for r in rows:
        if r["result"] == "WIN" and "win_level" not in r:
            r["win_level"] = 0
    head = "⚡ LASER AI VIP SIGNAL ⚡\n\n"
    if not rows:
        send(head + f"📊 OTC DAILY SUMMARY {today}\n\nNo signals were sent today.")
        return
    direct = sum(r["result"] == "WIN" and r.get("win_level") == 0 for r in rows)
    level = sum(r["result"] == "WIN" and (r.get("win_level") or 0) > 0 for r in rows)
    loss = sum(r["result"] == "LOSS" for r in rows)
    draw = sum(r["result"] == "DRAW" for r in rows)
    pend = sum(r["result"] is None for r in rows)
    settled = len(rows) - pend
    direct_rate = round(direct / max(settled, 1) * 100, 1)
    overall_rate = round((direct + level) / max(direct + level + loss, 1) * 100, 1)

    def label(r):
        if r["result"] == "WIN":
            if r.get("win_level") == 0:
                return "✅ WIN (Direct)"
            return f"✅ WIN (Level {r.get('win_level')})"
        if r["result"] == "LOSS": return "❌ LOSS (all levels)"
        if r["result"] == "DRAW": return "➖ DRAW"
        return "⏳ PENDING"

    lines = [f"{i+1}. {r['pair']} {'BUY' if r['direction']=='CALL' else 'SELL'}: {label(r)}"
             for i, r in enumerate(rows)]
    send(head + f"📊 OTC DAILY SUMMARY {today}\n\n"
         f"Signals: {len(rows)}\n"
         f"✅ Direct wins: {direct}\n"
         f"🔁 Wins on Martingale levels: {level}\n"
         f"❌ Full losses (all 4 entries): {loss}\n"
         f"➖ Draws: {draw}\n"
         f"⏳ Pending: {pend}\n\n"
         f"🎯 Direct entry win rate: {direct_rate}%\n"
         f"🎯 Win rate with levels: {overall_rate}%\n\n" + "\n".join(lines))

settle()
if now_dt.hour >= SUMMARY_HOUR and state["summary_day"] != today:
    summary()
    state["summary_day"] = today
else:
    scan()
save()
