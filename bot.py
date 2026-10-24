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

PAIRS = ["EURUSD-OTC", "GBPUSD-OTC", "EURJPY-OTC", "USDJPY-OTC"]
TIMEFRAME = 300          # 5 minute candles
EXPIRY_MIN = 5
MAX_SIGNALS = 10
MIN_GAP = 30 * 60        # seconds between signals
START_HOUR, END_HOUR = 8, 20   # Lagos time
SUMMARY_HOUR = 21

LAGOS = ZoneInfo("Africa/Lagos")
now_dt = datetime.now(LAGOS)
today = now_dt.strftime("%Y-%m-%d")

def send(text):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHANNEL, "text": text})
    print("telegram:", r.status_code, r.text[:100])
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        send("Test: the bot is connected")

try:
    with open("state.json") as f:
        state = json.load(f)
except Exception:
    state = {"signals": [], "summary_day": ""}

# keep only the last 7 days
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
        if s["result"] is not None or s["expiry_ts"] > time.time() - 60:
            continue
        try:
            c = api.get_candles(s["pair"], 60, 5, s["expiry_ts"] + 60)
            c = [x for x in c if x["from"] + 60 <= s["expiry_ts"]][-1]
            exit_price = c["close"]
        except Exception as e:
            print("settle error", e)
            continue
        if exit_price == s["entry"]: res = "DRAW"
        elif (exit_price > s["entry"]) == (s["direction"] == "CALL"): res = "WIN"
        else: res = "LOSS"
        s["exit"] = exit_price
        s["result"] = res

def scan():
    if not (START_HOUR <= now_dt.hour <= END_HOUR):
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
            entry = api.get_candles(pair, 60, 1, time.time())[-1]["close"]
            ts = int(time.time())
            state["signals"].append({"ts": ts, "day": today, "pair": pair,
                "direction": direction, "entry": entry,
                "expiry_ts": ts + EXPIRY_MIN * 60, "exit": None, "result": None})
            send(f"SIGNAL {len(todays)+1}/{MAX_SIGNALS}\nPair: {pair}\n"
                 f"Direction: {direction}\nExpiry: {EXPIRY_MIN} min\nEnter now.")
            return

def summary():
    rows = [s for s in state["signals"] if s["day"] == today]
    if not rows:
        send(f"DAILY SUMMARY {today}\nNo signals were sent today.")
        return
    w = sum(r["result"] == "WIN" for r in rows)
    l = sum(r["result"] == "LOSS" for r in rows)
    d = sum(r["result"] == "DRAW" for r in rows)
    rate = round(w / max(w + l, 1) * 100, 1)
    lines = [f"{i+1}. {r['pair']} {r['direction']}: {r['result'] or 'PENDING'}"
             for i, r in enumerate(rows)]
    send(f"DAILY SUMMARY {today}\nSignals: {len(rows)}\nWins: {w}\nLosses: {l}\n"
         f"Draws: {d}\nWin rate: {rate}%\n\n" + "\n".join(lines))

settle()
if now_dt.hour >= SUMMARY_HOUR and state["summary_day"] != today:
    summary()
    state["summary_day"] = today
else:
    scan()
save()
