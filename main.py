import telebot
from telebot import types
import requests
import pandas as pd
import json
import os  # <--- ĐÃ THÊM
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from ta.trend import MACD, EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange

# ==================== CONFIG (dùng env var để bảo mật) ====================
TOKEN = os.getenv("TOKEN", "8026512064:AAFSq32IIXkPkXPi7kMl-wM5NoD_gqSmpd0")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7760459637"))

COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT", "AVAXUSDT", "SHIBUSDT",
    "LINKUSDT", "DOTUSDT", "NEARUSDT", "LTCUSDT", "UNIUSDT", "PEPEUSDT", "ICPUSDT", "APTUSDT", "HBARUSDT", "CROUSDT",
    "VETUSDT", "MKRUSDT", "FILUSDT", "ATOMUSDT", "IMXUSDT", "OPUSDT", "ARBUSDT", "INJUSDT", "RUNEUSDT", "GRTUSDT"
]

INTERVAL = "15m"
LIMIT = 300
SQUEEZE_THRESHOLD = 0.018
COOLDOWN_MINUTES = 60

bot = telebot.TeleBot(TOKEN)

# ==================== FILE FUNCTIONS ====================
def load_json(file):
    if not os.path.exists(file):
        data = {"users": {}} if file == "users.json" else {"signals": []} if file == "recent_signals.json" else {"results": []}
        save_json(file, data)
    try:
        with open(file, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        # Nếu file bị lỗi, tạo lại file
        print(f"Lỗi đọc file {file}, đang tạo lại...")
        data = {"users": {}} if file == "users.json" else {"signals": []} if file == "recent_signals.json" else {"results": []}
        save_json(file, data)
        return data


def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

users = load_json("users.json")
recent = load_json("recent_signals.json")
results = load_json("results.json")

# ==================== BINANCE DATA ====================
def get_klines(symbol):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={INTERVAL}&limit={LIMIT}"
    try:
        data = requests.get(url, timeout=10).json()
        df = pd.DataFrame(data, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df
    except:
        return None

def add_indicators(df):
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    df["ema200"] = EMAIndicator(close, window=200).ema_indicator()
    macd = MACD(close)
    
    # === LỖI CÚ PHÁP ĐÃ SỬA Ở ĐÂY ===
    df["macd"] = macd.macd()
    # ===============================
    
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    df["rsi14"] = RSIIndicator(close, window=14).rsi() # <--- Tên cột là rsi14

    bb = BollingerBands(close, window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    df["atr"] = atr

    # Keltner Channel
    typical_price = (high + low + close) / 3
    df["kc_mid"] = typical_price.rolling(20).mean()
    df["kc_range"] = atr * 1.5
    df["kc_upper"] = df["kc_mid"] + df["kc_range"]
    df["kc_lower"] = df["kc_mid"] - df["kc_range"]

    # VWAP session approximation
    df["vwap"] = (typical_price * volume).cumsum() / volume.cumsum()

    # FVG
    df["fvg_bull"] = (df["low"].shift(2) > df["high"].shift(1))
    df["fvg_bear"] = (df["high"].shift(2) < df["low"].shift(1))

    # Wick/Body for liquidity grab
    df["body"] = abs(df["open"] - df["close"])
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]

    return df

# ==================== 10 COMBO LOGIC (Đã sửa lỗi runtime) ====================

def check_combo1_fvg_squeeze_pro(df):
    i = -1
    p = -2
    last = df.iloc[i]
    prev = df.iloc[p]

    squeeze = last.bb_width < SQUEEZE_THRESHOLD and last.bb_upper < last.kc_upper and last.bb_lower > last.kc_lower
    breakout_up = last.close > last.bb_upper and prev.close <= prev.bb_upper
    vol_spike = last.volume > df.volume[-20:].mean() * 1.3
    trend_up = last.close > last.ema200
    rsi_ok = last.rsi14 < 68 # <--- Sửa rsi thành rsi14

    if squeeze and breakout_up and vol_spike and trend_up and rsi_ok:
        entry = last.close
        sl = entry - 1.5 * last.atr
        tp = entry + 3.0 * last.atr
        return "LONG", round(entry, 4), round(sl, 4), round(tp, 4), "FVG Squeeze Pro"

    breakout_down = last.close < last.bb_lower and prev.close >= prev.bb_lower
    if squeeze and breakout_down and vol_spike and last.close < last.ema200:
        entry = last.close
        sl = entry + 1.5 * last.atr
        tp = entry - 3.0 * last.atr
        return "SHORT", round(entry, 4), round(sl, 4), round(tp, 4), "FVG Squeeze Pro"
    return None

def check_combo2_macd_ob_retest(df):
    last = df.iloc[-1] # <--- ĐÃ THÊM
    macd_cross_up = last["macd"] > last["macd_signal"] and df["macd"][-2] <= df["macd_signal"][-2]
    price_above_ema200 = last["close"] > last["ema200"]
    
    ob_zone = None
    if all(df["close"][-3:] > df["open"][-3:]): # Check if last 3 candles are bullish
        ob_zone = df["low"][-5:-2].min()
        
    retest = ob_zone is not None and last["low"] <= ob_zone + last["atr"] * 0.5
    vol_confirm = last["volume"] > df["volume"].mean() * 1.1
    
    if macd_cross_up and price_above_ema200 and retest and vol_confirm:
        entry = last["close"]
        sl = ob_zone - last["atr"]
        tp = entry + 2.5 * last["atr"]
        return "LONG", entry, sl, tp, "MACD Order Block Retest"
    return None

def check_combo3_stop_hunt_squeeze(df):
    last = df.iloc[-1]
    squeeze = last["bb_width"] < SQUEEZE_THRESHOLD
    stop_hunt = False
    if last["body"] > 0: # <--- Thêm check chia cho 0
        stop_hunt = (last["lower_wick"] / last["body"] > 2) if last["close"] > last["open"] else (last["upper_wick"] / last["body"] > 2)

    breakout_up = last["close"] > last["bb_upper"]
    if squeeze and stop_hunt and breakout_up:
        entry = last["close"]
        sl = last["low"] - last["atr"]
        tp = entry + 2.8 * last["atr"]
        return "LONG", entry, sl, tp, "Stop Hunt Squeeze"
    return None

def check_combo4_fvg_ema_pullback(df):
    last = df.iloc[-1] # <--- ĐÃ THÊM
    ema8 = EMAIndicator(df["close"], 8).ema_indicator()
    ema21 = EMAIndicator(df["close"], 21).ema_indicator()

    fvg_bull_zones = df[df["fvg_bull"]]
    fvg_pullback = False
    if not fvg_bull_zones.empty and any(df["fvg_bull"][-5:]):
        fvg_pullback = last["low"] <= fvg_bull_zones["high"].max() # <--- Sửa logic check
    
    cross_up = ema8.iloc[-1] > ema21.iloc[-1] and ema8.iloc[-2] <= ema21.iloc[-2] # <--- Sửa logic cross
    
    if fvg_pullback and cross_up:
        entry = last["close"]
        sl = last["low"] - last["atr"] * 0.8
        tp = entry + 2 * last["atr"]
        return "LONG", entry, sl, tp, "FVG EMA Pullback"
    return None

def check_combo5_fvg_macd_divergence(df):
    last = df.iloc[-1] # <--- ĐÃ THÊM
    hist = df["macd_hist"]
    low = df["low"]
    divergence = hist.iloc[-1] > hist.iloc[-3] and low.iloc[-1] < low.iloc[-3]
    fvg = any(df["fvg_bull"][-8:])
    rsi_ok = last["rsi14"] < 30 # <--- Sửa rsi thành rsi14
    
    if divergence and fvg and rsi_ok:
        entry = last["close"]
        sl = low[-5:].min() - last["atr"] # <--- Sửa logic
        tp = entry + 2.5 * last["atr"]
        return "LONG", entry, sl, tp, "FVG + MACD Divergence"
    return None

def check_combo6_ob_liquidity_grab(df):
    last = df.iloc[-1] # <--- ĐÃ THÊM
    ob = df["low"][-6:-3].min()
    liquidity_grab = (last["lower_wick"] / last["body"] > 2.5) if last["body"] > 0 else False # <--- Thêm check
    retest_ob = last["close"] > ob
    macd_pos = last["macd_hist"] > 0
    if liquidity_grab and retest_ob and macd_pos:
        entry = last["close"]
        sl = last["low"] - last["atr"]
        tp = entry + 1.8 * last["atr"]
        return "LONG", entry, sl, tp, "Order Block + Liquidity Grab"
    return None

def check_combo7_stop_hunt_fvg_retest(df):
    last = df.iloc[-1] # <--- ĐÃ THÊM
    stop_hunt = (last["lower_wick"] / last["body"] > 2) if last["body"] > 0 else False # <--- Thêm check
    fvg_after = df["fvg_bull"].iloc[-3:]
    retest = (last["low"] <= df["high"].shift(1).max()) if any(fvg_after) else False # <--- Sửa
    
    if stop_hunt and any(fvg_after) and retest:
        entry = last["close"]
        sl = last["low"] - 0.5 * last["atr"]
        tp = entry + 1.5 * last["atr"]
        return "LONG", entry, sl, tp, "Stop Hunt + FVG Retest"
    return None

def check_combo8_fvg_macd_hist_spike(df):
    last = df.iloc[-1] # <--- ĐÃ THÊM
    hist_spike = all(df["macd_hist"][-3:] > df["macd_hist"][-4:-1].values) # <--- Sửa
    fvg = any(df["fvg_bull"][-5:])
    price_above_vwap = last["close"] > last["vwap"]
    
    if hist_spike and fvg and price_above_vwap:
        entry = last["close"]
        sl = last["low"] - last["atr"]
        tp = entry + 2.5 * last["atr"]
        return "LONG", entry, sl, tp, "FVG + MACD Hist Spike"
    return None

def check_combo9_ob_fvg_confluence(df):
    last = df.iloc[-1] # <--- ĐÃ THÊM
    ob = df["low"][-10:-5].min()
    
    fvg_bull_zones = df[df["fvg_bull"]]
    fvg_zone = 0
    if not fvg_bull_zones.empty and any(df["fvg_bull"][-10:]):
        fvg_zone = fvg_bull_zones["high"].max() # <--- Sửa logic
        
    confluence = (abs(ob - fvg_zone) < last["atr"] * 0.5) if fvg_zone > 0 else False
    engulfing = last["close"] > last["open"] and last["open"] < df["close"].iloc[-2]
    volume_delta = last["volume"] > df["volume"].mean() * 1.5
    
    if confluence and engulfing and volume_delta:
        entry = last["close"]
        sl = min(ob, fvg_zone) - last["atr"] if fvg_zone > 0 else ob - last["atr"]
        tp = entry + 2 * last["atr"]
        return "LONG", entry, sl, tp, "OB + FVG Confluence"
    return None

def check_combo10_smc_ultimate(df):
    last = df.iloc[-1] # <--- ĐÃ THÊM
    squeeze = last["bb_width"] < SQUEEZE_THRESHOLD # <--- Sửa typo
    fvg = any(df["fvg_bull"][-5:])
    macd_up = last["macd_hist"] > 0 and last["macd_hist"] > df["macd_hist"].iloc[-2]
    liquidity = (last["lower_wick"] / last["body"] > 2) if last["body"] > 0 else False # <--- Thêm check
    ob_retest = last["low"] <= df["low"][-5:-2].min()
    
    if squeeze and fvg and macd_up and liquidity and ob_retest:
        entry = last["close"]
        sl = last["low"] - last["atr"]
        tp = entry + 3.5 * last["atr"]
        return "LONG", entry, sl, tp, "SMC Ultimate (FVG+OB+Liquidity+MACD)"
    return None

# SCAN FUNCTION (Sửa lỗi '...')
def scan():
    print(f"[{datetime.now()}] Đang quét tín hiệu...")
    for coin in COINS:
        df = get_klines(coin)
        if df is None or len(df) < 200:
            continue
        
        # Thêm chỉ báo một lần duy nhất
        try:
            df = add_indicators(df)
        except Exception as e:
            print(f"Lỗi khi thêm chỉ báo cho {coin}: {e}")
            continue

        # === DANH SÁCH COMBO ĐÃ SỬA ===
        combos = [
            check_combo1_fvg_squeeze_pro, check_combo2_macd_ob_retest,
            check_combo3_stop_hunt_squeeze, check_combo4_fvg_ema_pullback,
            check_combo5_fvg_macd_divergence, check_combo6_ob_liquidity_grab,
            check_combo7_stop_hunt_fvg_retest, check_combo8_fvg_macd_hist_spike,
            check_combo9_ob_fvg_confluence, check_combo10_smc_ultimate
        ]
        # =============================

        for func in combos:
            try:
                result = func(df) # <--- Chỉ truyền df đã có chỉ báo
                if result:
                    direction, entry, sl, tp, combo_name = result
                    sig_id = f"{coin}_{datetime.now().strftime('%Y%m%d%H%M')}_{combo_name.replace(' ', '')[:10]}"
                    
                    # Check cooldown
                    now = datetime.now()
                    is_in_cooldown = False
                    for s in recent.get("signals", []):
                        if s["coin"] == coin:
                            sig_time = datetime.strptime(s["time"], "%Y-%m-%d %H:%M:%S")
                            if (now - sig_time).total_seconds() / 60 < COOLDOWN_MINUTES:
                                is_in_cooldown = True
                                break
                    
                    if is_in_cooldown:
                        # print(f"Bỏ qua {coin} do đang trong cooldown.")
                        continue # Bỏ qua nếu coin này vừa có tín hiệu

                    text = f"#{coin.replace('USDT', '')} — {direction} 📌\n\n🟢 Điểm vào lệnh: {entry:.4f} 🆗\n🎯 Mục tiêu: {tp:.4f}\n🙅‍♂️ Dừng lỗ: {sl:.4f}\n\n📈Tín hiệu: {combo_name}\n\n❗Nhất định phải tuân thủ quản lý rủi ro – tín hiệu trên chỉ là tham khảo."

                    recent["signals"].append({"id": sig_id, "coin": coin, "time": now.strftime("%Y-%m-%d %H:%M:%S")})
                    save_json("recent_signals.json", recent)
                    print(f"=== TÌM THẤY TÍN HIỆU: {text} ===")

                    for uid in users:
                        user_data = users.get(uid, {})
                        if user_data.get("authorized", False) and (user_data.get("expiry") is None or datetime.fromtimestamp(user_data.get("expiry")) > now):
                            markup = None
                            if int(uid) == ADMIN_ID:
                                markup = types.InlineKeyboardMarkup(row_width=2)
                                markup.add(
                                    types.InlineKeyboardButton("Chốt lời ✅", callback_data=f"win_{sig_id}"),
                                    types.InlineKeyboardButton("Chốt lỗ ❌", callback_data=f"loss_{sig_id}")
                                )
                            try:
                                bot.send_message(uid, text, reply_markup=markup, parse_mode="HTML")
                            except Exception as e:
                                print(f"Lỗi khi gửi tin cho {uid}: {e}")
                    
                    # Sau khi tìm thấy 1 tín hiệu cho coin, dừng quét các combo khác
                    break 

            except Exception as e:
                print(f"Lỗi khi chạy combo {func.__name__} cho {coin}: {e}")
                import traceback
                traceback.print_exc()


# Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(scan, 'interval', minutes=6) # Đổi thành 6 phút
scheduler.start()
print("Bot đã khởi động và scheduler đã bắt đầu.")

# Handlers
@bot.message_handler(commands=["start"])
def start(message):
    uid = str(message.from_user.id)
    name = message.from_user.first_name
    
    if uid not in users:
        users[uid] = {"name": name, "authorized": False, "expiry": None}
    
    user_data = users[uid]
    is_authorized = user_data.get("authorized", False)
    expiry = user_data.get("expiry")
    is_active = is_authorized and (expiry is None or datetime.fromtimestamp(expiry) > datetime.now())

    if is_active:
        welcome = f"👋 Chào mừng {name} quay lại!\n\n🚀 AI Tín hiệu Trading Futures 2025\n\nQuy tắc vàng:\n• Risk tối đa 0.5-1%/lệnh\n• Luôn đặt SL/TP\n• Không FOMO, không revenge trade\n• Liên hệ admin nếu cần hỗ trợ: @HOANGDUNGG789\n\nChúc bạn xanh lè cả tuần! 📈🐂"
        bot.send_message(message.chat.id, welcome)
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Liên hệ ADMIN", url="t.me/HOANGDUNGG789"))
        bot.send_message(message.chat.id, f"👋 Chào {name}! 👾AI TÍN HIỆU TRADING MỚI NHẤT.\n⚡Nhắn tin cho ADMIN để được cấp quyền AI dự đoán.", reply_markup=markup)
    
    save_json("users.json", users)

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Bạn không phải admin.")
        return
    
    data = call.data
    action, sig_id = data.split("_", 1)
    
    result = "Chốt lời ✅" if action == "win" else "Chốt lỗ ❌"
    
    try:
        bot.edit_message_text(call.message.text + f"\n\n{result} (Admin đóng lệnh)", call.message.chat.id, call.message.message_id)
    except Exception as e:
        print(f"Lỗi edit message: {e}") # Có thể do tin nhắn quá cũ

    results.get("results", []).append({"id": sig_id, "result": result, "time": datetime.now().strftime("%Y-%m-%d")})
    save_json("results.json", results)
    bot.answer_callback_query(call.id, f"Đã ghi nhận: {result}")

    # Gửi thông báo chốt lệnh cho mọi người
    coin_name = sig_id.split('_')[0]
    for uid in users:
        if users.get(uid, {}).get("authorized", False) and str(uid) != str(ADMIN_ID):
            try:
                bot.send_message(uid, f"Tín hiệu {coin_name} đã {result}")
            except Exception as e:
                print(f"Lỗi gửi thông báo chốt lệnh cho {uid}: {e}")

@bot.message_handler(commands=["admin"])
def admin_help(message):
    if message.from_user.id != ADMIN_ID:
        return
    text = """
    **Lệnh Admin**
    `/broadcast [nội dung]` - Gửi tin nhắn cho tất cả user.
    `/summary` - Thống kê win/loss 7 ngày qua.
    
    **Cấp quyền (Reply tin nhắn của user):**
    `/grant_week` - Cấp quyền 7 ngày.
    `/grant_month` - Cấp quyền 30 ngày.
    `/grant_perm` - Cấp quyền vĩnh viễn.
    `/remove` - Xóa quyền user.
    """
    bot.reply_to(message, text, parse_mode="Markdown")


@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID)
def admin_commands(message):
    text = message.text
    if text.startswith("/broadcast"):
        msg = text[10:].strip()
        if not msg:
            bot.reply_to(message, "Vui lòng nhập nội dung broadcast. /broadcast [nội dung]")
            return
            
        count = 0
        for uid in users:
            if users.get(uid, {}).get("authorized", False):
                try:
                    bot.send_message(uid, msg)
                    count += 1
                except Exception as e:
                    print(f"Lỗi broadcast đến {uid}: {e}")
        bot.reply_to(message, f"Đã broadcast thành công đến {count} users.")
    
    elif text.startswith(("/grant_week", "/grant_month", "/grant_perm", "/remove")):
        if not message.reply_to_message:
            bot.reply_to(message, "Bạn cần reply tin nhắn của user để dùng lệnh này.")
            return

        target_user = message.reply_to_message.from_user
        target_id = str(target_user.id)
        target_name = target_user.first_name

        if target_id not in users:
            users[target_id] = {"name": target_name, "authorized": False, "expiry": None}

        if text.startswith("/grant_week"):
            expiry = (datetime.now() + timedelta(days=7)).timestamp()
            typ = "tuần"
            users[target_id]["expiry"] = expiry
            users[target_id]["authorized"] = True
        elif text.startswith("/grant_month"):
            expiry = (datetime.now() + timedelta(days=30)).timestamp()
            typ = "tháng"
            users[target_id]["expiry"] = expiry
            users[target_id]["authorized"] = True
        elif text.startswith("/grant_perm"):
            expiry = None
            typ = "vĩnh viễn"
            users[target_id]["expiry"] = expiry
            users[target_id]["authorized"] = True
        elif text.startswith("/remove"):
            users[target_id]["authorized"] = False
            users[target_id]["expiry"] = 0
            typ = "bị xóa"
            bot.reply_to(message, f"Đã xóa quyền của {target_name} ({target_id}).")
            save_json("users.json", users)
            return

        bot.send_message(target_id, f"Chúc mừng! Bạn đã được Admin cấp quyền sử dụng {typ}.")
        bot.reply_to(message, f"Đã cấp quyền {typ} cho {target_name} ({target_id}).")
        save_json("users.json", users)


@bot.message_handler(commands=["summary"])
def summary(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    week_ago = datetime.now() - timedelta(days=7)
    recent_results = [r for r in results.get("results", []) if datetime.strptime(r["time"], "%Y-%m-%d") > week_ago]
    
    win = len([r for r in recent_results if "lời" in r.get("result", "")])
    loss = len([r for r in recent_results if "lỗ" in r.get("result", "")])
    total = win + loss
    
    wr = (win / total * 100) if total > 0 else 0
    bot.send_message(message.chat.id, f"**Thống kê 7 ngày qua:**\n\n✅ Thắng: {win}\n❌ Thua: {loss}\nTổng: {total}\n\n📊 Win rate: {wr:.1f}%", parse_mode="Markdown")

# Thêm handler mặc định để bắt các tin nhắn khác
@bot.message_handler(func=lambda message: True)
def handle_all_other_messages(message):
    if str(message.from_user.id) == str(ADMIN_ID):
        admin_commands(message) # Cho phép admin dùng lệnh mà ko cần /
    else:
        # Người dùng thường, có thể trả lời hoặc không
        pass # bot.reply_to(message, "Gõ /start để bắt đầu.")
        
print("Bot đang chạy...")
bot.infinity_polling()
