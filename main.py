"""
Telegram 私人健身助理 Bot - main.py
=====================================
新功能：運動記錄、每日定時推送、熱量目標、體重趨勢分析
"""

# ============================================================
#  ⚙️  配置區域 - 請修改以下變數
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")

USER_CHAT_ID = int(os.getenv("USER_CHAT_ID", "0"))

USER_CHAT_ID        =     # 填入您的 Telegram Chat ID（發送 /start 後從日誌查看）
DAILY_REPORT_HOUR   = 21    # 每天幾點推送報告（24小時制，預設晚上9點）
DAILY_REPORT_MINUTE = 0
TIMEZONE            = "Asia/Kuala_Lumpur"  # 時區設定（馬來西亞 UTC+8）
WEEKLY_REPORT_DAY   = 0     # 週報發送日：0=週一, 6=週日
MONTHLY_REPORT_DAY  = 1     # 月報發送日：每月幾號（1-28）
CALORIE_BURN_GOAL   = 500   # 每日目標消耗卡路里（kcal），可在代碼頂部修改
# ============================================================
import os
import asyncio
import json
import logging
import re
import pytz
from datetime import datetime, timedelta, time as dtime

import gspread
from google.oauth2.service_account import Credentials
import google.genai as genai
from google.genai import types as genai_types

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ── 日誌設定 ────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Gemini 初始化 ────────────────────────────────────────────
gemini_client   = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL_ID = "gemini-3-flash-preview"

# ── 肌肉解剖圖路徑 ───────────────────────────────────────────
import os as _os
ANATOMY_ANTERIOR_PATH = _os.path.join(_os.path.dirname(__file__), "anatomy_anterior.png")
ANATOMY_POSTERIOR_PATH = _os.path.join(_os.path.dirname(__file__), "anatomy_posterior.png")

# ── Google Sheets 初始化 ─────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_sheet_client():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds)

def ensure_worksheet(spreadsheet, title: str, headers: list[str]):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
    return ws


# ════════════════════════════════════════════════════════════
#  📦 Sheets 讀取快取層（避免 429 Quota Exceeded）
#  同一張表在 TTL 秒內只讀一次，寫入後自動失效
# ════════════════════════════════════════════════════════════

import time as _time
import threading as _threading

class _SheetCache:
    """
    線程安全的 Google Sheets 讀取快取。
    TTL 預設 45 秒（Sheets API 每分鐘 60 次讀取限制）。
    """
    TTL = 45  # 秒

    def __init__(self):
        self._cache: dict[str, tuple[float, list]] = {}  # key → (timestamp, data)
        self._lock  = _threading.Lock()

    def get(self, key: str) -> list | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry and (_time.time() - entry[0]) < self.TTL:
                return entry[1]
            return None

    def set(self, key: str, data: list):
        with self._lock:
            self._cache[key] = (_time.time(), data)

    def invalidate(self, *keys: str):
        """寫入後呼叫，清除相關快取使下次強制重新讀取"""
        with self._lock:
            for k in keys:
                self._cache.pop(k, None)

    def invalidate_all(self):
        with self._lock:
            self._cache.clear()

_sheet_cache = _SheetCache()

def _cached_get_all(sheet_key: str, ws) -> list:
    """從快取讀取，快取未命中時才呼叫 ws.get_all_values()"""
    cached = _sheet_cache.get(sheet_key)
    if cached is not None:
        return cached
    data = ws.get_all_values()[1:]  # 去掉標題行
    _sheet_cache.set(sheet_key, data)
    return data




# ════════════════════════════════════════════════════════════
#  工具函式
# ════════════════════════════════════════════════════════════

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def extract_json(text: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", cleaned)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"無法提取 JSON：\n{text}")

def escape_md(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


# ════════════════════════════════════════════════════════════
#  主選單鍵盤
# ════════════════════════════════════════════════════════════

# ── 主選單（大分類）────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🍎 飲食"),     KeyboardButton("🏃 運動")],
        [KeyboardButton("🌙 睡眠"),     KeyboardButton("🤖 AI教練")],
        [KeyboardButton("👤 個人資料"), KeyboardButton("💧 喝水")],
        [KeyboardButton("📊 查看"),     KeyboardButton("📈 分析")],
        [KeyboardButton("⚙️ 設定")],
    ],
    resize_keyboard=True,
)

SUB_KEYBOARD_COACH = ReplyKeyboardMarkup(
    [[KeyboardButton("💬 問教練"),      KeyboardButton("📊 本週分析")],
     [KeyboardButton("🏋️ 訓練計劃"),   KeyboardButton("🍽 今日飲食建議")],
     [KeyboardButton("📉 減重分析"),    KeyboardButton("💪 部位弱點分析")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)

# ── 子選單 ──────────────────────────────────────────────────────
SUB_KEYBOARD_FOOD = ReplyKeyboardMarkup(
    [[KeyboardButton("🍎 上傳食物")],
     [KeyboardButton("💡 今日飲食建議"), KeyboardButton("📖 食譜庫")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_RECIPES = ReplyKeyboardMarkup(
    [[KeyboardButton("🏋️ 增肌食譜"),   KeyboardButton("🔥 減脂食譜")],
     [KeyboardButton("⚡ 快速備餐"),    KeyboardButton("🥩 高蛋白零食")],
     [KeyboardButton("💡 根據缺口推薦食譜")],
     [KeyboardButton("🔙 飲食選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_EXERCISE = ReplyKeyboardMarkup(
    [[KeyboardButton("🏃 上傳運動記錄"), KeyboardButton("📱 上傳活動數據")],
     [KeyboardButton("📊 本週運動記錄"), KeyboardButton("💪 本週訓練進度")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_ACTIVITY = ReplyKeyboardMarkup(
    [[KeyboardButton("👟 上傳活動數據")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_WEIGHT = ReplyKeyboardMarkup(
    [[KeyboardButton("⚖️ 體重上傳")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_PROFILE = ReplyKeyboardMarkup(
    [[KeyboardButton("⚖️ 記錄體重"),    KeyboardButton("📸 上傳身材照")],
     [KeyboardButton("📊 體重趨勢"),    KeyboardButton("📏 我的身高/BMI")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_WATER = ReplyKeyboardMarkup(
    [[KeyboardButton("💧 250ml"), KeyboardButton("💧 500ml")],
     [KeyboardButton("💧 750ml"), KeyboardButton("💧 自訂ml")],
     [KeyboardButton("📊 今日飲水"), KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_VIEW = ReplyKeyboardMarkup(
    [[KeyboardButton("📅 今日資料"),  KeyboardButton("📋 本週資料")],
     [KeyboardButton("📆 週報"),      KeyboardButton("🗓 月報")],
     [KeyboardButton("📂 總資料")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_ANALYSIS = ReplyKeyboardMarkup(
    [[KeyboardButton("🏆 目標達成"),    KeyboardButton("📈 體重趨勢")],
     [KeyboardButton("📊 每週熱量圖"),  KeyboardButton("📉 BMI趨勢")],
     [KeyboardButton("💪 訓練部位統計")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_SLEEP = ReplyKeyboardMarkup(
    [[KeyboardButton("📷 上傳睡眠截圖"), KeyboardButton("🌙 今日睡眠")],
     [KeyboardButton("📊 睡眠趨勢"),     KeyboardButton("🤖 AI睡眠分析")],
     [KeyboardButton("📋 本週睡眠報告")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)
SUB_KEYBOARD_SETTINGS = ReplyKeyboardMarkup(
    [[KeyboardButton("🎯 設定熱量目標"),   KeyboardButton("🔥 設定消耗目標")],
     [KeyboardButton("🥩 設定蛋白質目標"), KeyboardButton("👟 設定步數目標")],
     [KeyboardButton("🌾 設定碳水目標"),   KeyboardButton("🌿 設定纖維目標")],
     [KeyboardButton("💧 設定飲水目標"),   KeyboardButton("⚖️ 設定體重目標")],
     [KeyboardButton("📏 設定身高"),       KeyboardButton("🎂 設定年齡/性別")],
     [KeyboardButton("💪 設定訓練計劃"),   KeyboardButton("📋 查看目前設定")],
     [KeyboardButton("🔙 返回主選單")]],
    resize_keyboard=True,
)

# ── 狀態機 ───────────────────────────────────────────────────
STATE_IDLE              = "idle"
STATE_AWAIT_FOOD        = "await_food"
STATE_AWAIT_BODY        = "await_body"
STATE_AWAIT_WEIGHT      = "await_weight"
STATE_AWAIT_EXERCISE    = "await_exercise"
STATE_AWAIT_GOAL        = "await_goal"
STATE_AWAIT_ACTIVITY    = "await_activity"
STATE_AWAIT_WEIGHT_GOAL = "await_weight_goal"
STATE_AWAIT_BURN_GOAL   = "await_burn_goal"
STATE_AWAIT_PROTEIN_GOAL= "await_protein_goal"
STATE_AWAIT_STEPS_GOAL  = "await_steps_goal"
STATE_AWAIT_WATER       = "await_water"
STATE_AWAIT_WATER_GOAL  = "await_water_goal"
STATE_AWAIT_MUSCLE_GOAL = "await_muscle_goal"
STATE_AWAIT_RECIPE_NUM  = "await_recipe_num"
STATE_AWAIT_HEIGHT      = "await_height"
STATE_AWAIT_AGE_GENDER  = "await_age_gender"
STATE_AWAIT_COACH_Q     = "await_coach_question"
STATE_AWAIT_CARB_GOAL   = "await_carb_goal"
STATE_AWAIT_FIBER_GOAL  = "await_fiber_goal"
STATE_AWAIT_SLEEP       = "await_sleep"


# ════════════════════════════════════════════════════════════
#  本地食物 & 食譜資料庫（靜態，零 AI 消耗）
# ════════════════════════════════════════════════════════════

# 食物資料：{名稱: {kcal, protein, per, tags}}
# per = 每份份量描述, tags = 分類標籤
FOOD_DB = {
    # ── 高蛋白主食 ──────────────────────────────────────────
    "雞胸肉 100g":       {"kcal": 165, "protein": 31, "carbs":  0, "fiber": 0, "per": "100g",    "tags": ["高蛋白","增肌","低脂"]},
    "水煮雞胸肉飯":      {"kcal": 420, "protein": 38, "carbs": 52, "fiber": 1, "per": "1份",     "tags": ["高蛋白","增肌","正餐"]},
    "鮭魚 100g":         {"kcal": 208, "protein": 20, "carbs":  0, "fiber": 0, "per": "100g",    "tags": ["高蛋白","增肌","健康脂肪"]},
    "吞拿魚罐頭（水浸）":{"kcal": 120, "protein": 25, "carbs":  0, "fiber": 0, "per": "1罐",     "tags": ["高蛋白","快速","零食"]},
    "水煮蛋":            {"kcal":  70, "protein":  6, "carbs":  0, "fiber": 0, "per": "1顆",     "tags": ["高蛋白","零食","快速"]},
    "炒蛋（2顆）":       {"kcal": 180, "protein": 12, "carbs":  1, "fiber": 0, "per": "2顆",     "tags": ["高蛋白","快速","正餐"]},
    "豆腐（板豆腐）100g":{"kcal":  76, "protein":  8, "carbs":  2, "fiber": 0, "per": "100g",    "tags": ["高蛋白","素食","低卡"]},
    "毛豆 100g":         {"kcal": 121, "protein": 11, "carbs":  9, "fiber": 4, "per": "100g",    "tags": ["高蛋白","素食","零食"]},
    "希臘乳酪 100g":     {"kcal":  59, "protein": 10, "carbs":  4, "fiber": 0, "per": "100g",    "tags": ["高蛋白","零食","早餐"]},
    "低脂牛奶 250ml":    {"kcal": 100, "protein":  8, "carbs": 12, "fiber": 0, "per": "250ml",   "tags": ["高蛋白","零食","飲品"]},
    "無糖豆漿 250ml":    {"kcal":  80, "protein":  7, "carbs":  4, "fiber": 1, "per": "250ml",   "tags": ["高蛋白","素食","飲品"]},
    "天貝 100g":         {"kcal": 193, "protein": 19, "carbs":  7, "fiber": 0, "per": "100g",    "tags": ["高蛋白","素食","增肌"]},
    "雞腿肉 100g":       {"kcal": 209, "protein": 26, "carbs":  0, "fiber": 0, "per": "100g",    "tags": ["高蛋白","增肌","正餐"]},
    "牛肉（瘦）100g":    {"kcal": 217, "protein": 26, "carbs":  0, "fiber": 0, "per": "100g",    "tags": ["高蛋白","增肌","正餐"]},
    "蝦 100g":           {"kcal":  85, "protein": 18, "carbs":  1, "fiber": 0, "per": "100g",    "tags": ["高蛋白","低卡","增肌"]},
    "鱸魚 100g":         {"kcal": 124, "protein": 24, "carbs":  0, "fiber": 0, "per": "100g",    "tags": ["高蛋白","低脂","增肌"]},
    "乳清蛋白粉（1勺）": {"kcal": 120, "protein": 24, "carbs":  3, "fiber": 0, "per": "1勺30g", "tags": ["高蛋白","增肌","補充劑"]},
    "豬里肌 100g":       {"kcal": 143, "protein": 22, "carbs":  0, "fiber": 0, "per": "100g",    "tags": ["高蛋白","增肌","正餐"]},
    "章魚 100g":         {"kcal":  82, "protein": 15, "carbs":  2, "fiber": 0, "per": "100g",    "tags": ["高蛋白","低卡","增肌"]},
    "低脂茅屋起司 100g": {"kcal":  98, "protein": 11, "carbs":  3, "fiber": 0, "per": "100g",    "tags": ["高蛋白","零食","減脂"]},
    "雞蛋白 100g":       {"kcal":  52, "protein": 11, "carbs":  1, "fiber": 0, "per": "100g",    "tags": ["高蛋白","低卡","增肌"]},

    # ── 碳水主食 ────────────────────────────────────────────
    "白飯 1碗":          {"kcal": 240, "protein":  4, "carbs": 53, "fiber": 0, "per": "180g",    "tags": ["碳水","正餐"]},
    "糙米飯 1碗":        {"kcal": 220, "protein":  5, "carbs": 46, "fiber": 2, "per": "180g",    "tags": ["碳水","減脂","正餐"]},
    "燕麥片 50g":        {"kcal": 190, "protein":  7, "carbs": 34, "fiber": 4, "per": "50g乾",   "tags": ["碳水","早餐","減脂","高纖"]},
    "全麥麵包 2片":      {"kcal": 160, "protein":  6, "carbs": 28, "fiber": 4, "per": "2片",     "tags": ["碳水","早餐","快速","高纖"]},
    "番薯 100g":         {"kcal":  86, "protein":  2, "carbs": 20, "fiber": 3, "per": "100g熟",  "tags": ["碳水","減脂","健康","高纖"]},
    "烏冬麵 1份":        {"kcal": 280, "protein":  8, "carbs": 58, "fiber": 2, "per": "1份",     "tags": ["碳水","正餐"]},
    "意大利麵 80g乾":    {"kcal": 286, "protein": 10, "carbs": 58, "fiber": 3, "per": "80g乾",   "tags": ["碳水","正餐"]},
    "雜糧麵包 1片":      {"kcal":  80, "protein":  3, "carbs": 14, "fiber": 2, "per": "1片",     "tags": ["碳水","早餐","快速"]},
    "藜麥 100g乾":       {"kcal": 368, "protein": 14, "carbs": 64, "fiber": 7, "per": "100g乾",  "tags": ["碳水","高蛋白","高纖","增肌"]},
    "馬鈴薯 100g":       {"kcal":  77, "protein":  2, "carbs": 17, "fiber": 2, "per": "100g",    "tags": ["碳水","正餐"]},
    "玉米 1條":          {"kcal": 132, "protein":  5, "carbs": 29, "fiber": 4, "per": "1條",     "tags": ["碳水","高纖","零食"]},
    "糙米粥 1碗":        {"kcal": 130, "protein":  3, "carbs": 28, "fiber": 1, "per": "1碗",     "tags": ["碳水","早餐","易消化"]},

    # ── 蔬菜（低卡高纖）────────────────────────────────────
    "西蘭花 100g":       {"kcal":  34, "protein":  3, "carbs":  7, "fiber": 3, "per": "100g",    "tags": ["低卡","蔬菜","高纖"]},
    "菠菜 100g":         {"kcal":  23, "protein":  3, "carbs":  4, "fiber": 2, "per": "100g",    "tags": ["低卡","蔬菜","高纖"]},
    "黃瓜 1條":          {"kcal":  16, "protein":  1, "carbs":  4, "fiber": 1, "per": "1條",     "tags": ["低卡","蔬菜","零食"]},
    "番茄 1個":          {"kcal":  22, "protein":  1, "carbs":  5, "fiber": 1, "per": "1個",     "tags": ["低卡","蔬菜","零食"]},
    "豆芽 100g":         {"kcal":  30, "protein":  3, "carbs":  6, "fiber": 2, "per": "100g",    "tags": ["低卡","蔬菜","快速"]},
    "生菜 100g":         {"kcal":  15, "protein":  1, "carbs":  2, "fiber": 1, "per": "100g",    "tags": ["低卡","蔬菜"]},
    "秋葵 100g":         {"kcal":  33, "protein":  2, "carbs":  7, "fiber": 3, "per": "100g",    "tags": ["低卡","蔬菜","高纖"]},
    "紅蘿蔔 100g":       {"kcal":  41, "protein":  1, "carbs": 10, "fiber": 3, "per": "100g",    "tags": ["低卡","蔬菜","高纖"]},
    "蘑菇 100g":         {"kcal":  22, "protein":  3, "carbs":  3, "fiber": 1, "per": "100g",    "tags": ["低卡","蔬菜","高蛋白"]},
    "椰菜花 100g":       {"kcal":  25, "protein":  2, "carbs":  5, "fiber": 2, "per": "100g",    "tags": ["低卡","蔬菜","高纖"]},
    "芹菜 100g":         {"kcal":  16, "protein":  1, "carbs":  3, "fiber": 2, "per": "100g",    "tags": ["低卡","蔬菜","高纖"]},
    "苦瓜 100g":         {"kcal":  17, "protein":  1, "carbs":  4, "fiber": 3, "per": "100g",    "tags": ["低卡","蔬菜","高纖","減脂"]},

    # ── 水果 ────────────────────────────────────────────────
    "香蕉 1條":          {"kcal":  89, "protein":  1, "carbs": 23, "fiber": 3, "per": "1條",     "tags": ["碳水","水果","運動前"]},
    "蘋果 1個":          {"kcal":  72, "protein":  0, "carbs": 19, "fiber": 3, "per": "1個",     "tags": ["低卡","水果","零食","高纖"]},
    "藍莓 100g":         {"kcal":  57, "protein":  1, "carbs": 14, "fiber": 2, "per": "100g",    "tags": ["低卡","水果","抗氧化"]},
    "木瓜 200g":         {"kcal":  78, "protein":  1, "carbs": 20, "fiber": 3, "per": "200g",    "tags": ["低卡","水果","消化","高纖"]},
    "橙子 1個":          {"kcal":  62, "protein":  1, "carbs": 15, "fiber": 3, "per": "1個",     "tags": ["低卡","水果","零食"]},
    "草莓 100g":         {"kcal":  32, "protein":  1, "carbs":  8, "fiber": 2, "per": "100g",    "tags": ["低卡","水果","零食"]},
    "奇異果 1個":        {"kcal":  61, "protein":  1, "carbs": 15, "fiber": 3, "per": "1個",     "tags": ["低卡","水果","高纖"]},

    # ── 健康脂肪 ─────────────────────────────────────────────
    "花生醬 1湯匙":      {"kcal":  94, "protein":  4, "carbs":  3, "fiber": 1, "per": "16g",     "tags": ["健康脂肪","零食","高熱量"]},
    "酪梨半個":          {"kcal": 120, "protein":  2, "carbs":  6, "fiber": 5, "per": "半個",    "tags": ["健康脂肪","減脂","高纖"]},
    "混合堅果 30g":      {"kcal": 175, "protein":  5, "carbs":  6, "fiber": 2, "per": "30g",     "tags": ["健康脂肪","零食","高熱量"]},
    "橄欖油 1湯匙":      {"kcal": 119, "protein":  0, "carbs":  0, "fiber": 0, "per": "14ml",    "tags": ["健康脂肪","烹飪"]},
    "奇亞籽 15g":        {"kcal":  73, "protein":  2, "carbs":  6, "fiber": 5, "per": "15g",     "tags": ["健康脂肪","高纖","增肌"]},
    "亞麻籽 15g":        {"kcal":  80, "protein":  3, "carbs":  4, "fiber": 4, "per": "15g",     "tags": ["健康脂肪","高纖"]},
}

# ── 訓練部位資料庫（AI教練用）────────────────────────────────
MUSCLE_TRAINING_DB = {
    "胸": {
        "emoji": "🫁",
        "exercises": ["槓鈴臥推", "啞鈴臥推", "上斜臥推", "下斜臥推", "蝴蝶機夾胸", "伏地挺身", "雙槓撐體", "纜繩飛鳥"],
        "tips": "胸肌訓練建議每週2次，訓練後休息48小時。注意肩胛骨收緊，保護肩關節。",
        "beginner": "從伏地挺身和啞鈴臥推開始，掌握動作模式後再增加重量。",
        "frequency": "每週2-3次",
    },
    "背": {
        "emoji": "🔙",
        "exercises": ["引體向上", "坐姿划船", "T槓划船", "單臂啞鈴划船", "高位下拉", "直腿硬拉", "早安式", "反向飛鳥"],
        "tips": "背部訓練要注重收縮感，避免用手臂代償。硬拉是最好的全背部動作。",
        "beginner": "先練高位下拉和坐姿划船，感受背部收縮，再進展到引體向上。",
        "frequency": "每週2次",
    },
    "肩": {
        "emoji": "🔺",
        "exercises": ["槓鈴肩推", "啞鈴肩推", "啞鈴側平舉", "前平舉", "反向飛鳥", "阿諾德推舉", "直立划船", "彈力帶外旋"],
        "tips": "肩膀三束（前/側/後）要均衡訓練，後束常被忽略。注意不要過度抬頭。",
        "beginner": "側平舉+啞鈴肩推+反向飛鳥是最安全的入門組合。",
        "frequency": "每週2次",
    },
    "手臂": {
        "emoji": "💪",
        "exercises": ["槓鈴彎舉", "啞鈴彎舉", "錘式彎舉", "集中彎舉", "三頭下壓", "窄距臥推", "法式彎舉", "過頭三頭伸展"],
        "tips": "手臂訓練量不需過多，複合動作已充分刺激。二頭和三頭要均衡。",
        "beginner": "每週1-2次即可，過多手臂訓練反而影響恢復。",
        "frequency": "每週1-2次",
    },
    "核心": {
        "emoji": "⚡",
        "exercises": ["捲腹", "反向捲腹", "平板支撐", "俄羅斯轉體", "懸掛舉腿", "木柴劈砍", "側平板", "滾輪"],
        "tips": "核心訓練要涵蓋屈曲、旋轉、側屈和抗旋轉。每天練也可以。",
        "beginner": "平板支撐+捲腹+反向捲腹是最好的入門組合，感受腹肌發力。",
        "frequency": "每週3-5次",
    },
    "腿": {
        "emoji": "🦵",
        "exercises": ["槓鈴深蹲", "腿推機", "分腿蹲", "羅馬尼亞硬拉", "腿彎舉", "腿伸展", "臀推", "弓步蹲"],
        "tips": "腿部是最大的肌群，訓練後代謝提升最明顯。深蹲是王者動作。",
        "beginner": "先練徒手深蹲確認動作正確，再加重量。注意膝蓋不要超過腳尖太多。",
        "frequency": "每週1-2次（恢復較慢）",
    },
    "全身": {
        "emoji": "🏋️",
        "exercises": ["硬拉", "壺鈴擺盪", "爆發上推", "波比跳", "農夫走路", "土耳其起立", "抓舉", "挺舉"],
        "tips": "全身動作效率最高，同時刺激多個肌群，燃脂效果強。",
        "beginner": "壺鈴擺盪和農夫走路是最安全的全身入門動作。",
        "frequency": "每週2-3次",
    },
    "有氧": {
        "emoji": "🏃",
        "exercises": ["跑步", "快走", "騎單車", "游泳", "跳繩", "划船機", "橢圓機", "爬樓梯"],
        "tips": "有氧配合力量訓練效果最好。HIIT效率高，穩態有氧適合恢復日。",
        "beginner": "從快走或低強度慢跑開始，逐漸增加時間和強度。",
        "frequency": "每週3-5次，每次20-45分鐘",
    },
}

# 食譜庫
RECIPE_DB = [
    # ══ 增肌食譜 (30個) ══════════════════════════════════════
    {"name":"雞胸肉炒西蘭花飯","category":"增肌","time":15,"kcal":520,"protein":45,"carbs":52,"fiber":3,
     "ingredients":["雞胸肉 150g","西蘭花 200g","糙米飯 1碗","蒜頭","醬油麻油少許"],
     "steps":["糙米飯預先煮好","雞胸肉切片醃5分鐘","熱鍋炒香蒜頭","炒熟雞肉加醬油","加西蘭花翻炒2分鐘","盛盤淋麻油"]},
    {"name":"水煮蛋燕麥早餐","category":"增肌","time":10,"kcal":380,"protein":28,"carbs":42,"fiber":4,
     "ingredients":["燕麥片 60g","雞蛋 2顆","低脂牛奶 200ml","香蕉半條"],
     "steps":["燕麥加牛奶微波3分鐘","水滾放入雞蛋煮8分鐘","雞蛋去殼燕麥加香蕉即可"]},
    {"name":"鮭魚牛油果沙拉","category":"增肌","time":10,"kcal":480,"protein":32,"carbs":8,"fiber":5,
     "ingredients":["鮭魚 150g","酪梨半個","生菜 100g","番茄 1個","橄欖油檸檬汁"],
     "steps":["鮭魚煎熟或180°C烤15分鐘","生菜番茄牛油果切好鋪盤","放上鮭魚淋橄欖油檸檬汁"]},
    {"name":"天貝炒飯","category":"增肌","time":15,"kcal":490,"protein":28,"carbs":58,"fiber":3,
     "ingredients":["天貝 150g","白飯 1碗","雞蛋 1顆","蔬菜任意","醬油胡椒粉"],
     "steps":["天貝切丁煎至金黃","推開炒蛋","加冷飯大火翻炒","加調味加蔬菜翻炒即可"]},
    {"name":"希臘乳酪蛋白碗","category":"增肌","time":5,"kcal":320,"protein":30,"carbs":28,"fiber":3,
     "ingredients":["希臘乳酪 200g","藍莓 80g","燕麥片 30g","花生醬 1湯匙"],
     "steps":["希臘乳酪倒入碗中","鋪上藍莓和燕麥片","淋上花生醬即可"]},
    {"name":"牛肉藜麥碗","category":"增肌","time":25,"kcal":550,"protein":42,"carbs":48,"fiber":6,
     "ingredients":["瘦牛肉 150g","藜麥 80g乾","菠菜 100g","番茄 1個","橄欖油鹽胡椒"],
     "steps":["藜麥煮20分鐘","牛肉煎熟切片","菠菜燙熟","全部擺盤淋橄欖油"]},
    {"name":"蛋白奶昔早餐","category":"增肌","time":3,"kcal":400,"protein":35,"carbs":40,"fiber":3,
     "ingredients":["乳清蛋白粉 1勺","牛奶 300ml","香蕉 1條","燕麥片 30g","花生醬 1湯匙"],
     "steps":["全部放入攪拌機","高速攪打30秒","即可飲用"]},
    {"name":"豆腐炒蛋飯","category":"增肌","time":15,"kcal":440,"protein":30,"carbs":50,"fiber":2,
     "ingredients":["板豆腐 200g","雞蛋 2顆","糙米飯 1碗","蔥花醬油"],
     "steps":["豆腐切丁炒至金黃","加蛋液炒熟","加醬油調味","配糙米飯"]},
    {"name":"烤雞腿藜麥沙拉","category":"增肌","time":30,"kcal":520,"protein":40,"carbs":40,"fiber":5,
     "ingredients":["雞腿 1隻","藜麥 60g","混合生菜","番茄黃瓜","油醋汁"],
     "steps":["雞腿200°C烤25分鐘","藜麥煮熟","蔬菜擺盤","雞腿切片放上淋醬"]},
    {"name":"鮪魚蛋炒飯","category":"增肌","time":10,"kcal":460,"protein":36,"carbs":52,"fiber":1,
     "ingredients":["鮪魚罐頭 1罐","雞蛋 2顆","白飯 1碗","蔥花醬油"],
     "steps":["熱鍋炒蛋","加飯大火翻炒","加鮪魚翻炒均勻","加醬油蔥花即可"]},
    {"name":"毛豆雞絲拌麵","category":"增肌","time":15,"kcal":500,"protein":38,"carbs":55,"fiber":5,
     "ingredients":["雞胸肉 120g","毛豆 100g","全麥麵條 80g","芝麻醬醬油"],
     "steps":["雞肉煮熟撕絲","毛豆煮熟","麵條煮熟","全部拌芝麻醬即可"]},
    {"name":"低脂奶昔+全麥吐司","category":"增肌","time":5,"kcal":380,"protein":28,"carbs":45,"fiber":4,
     "ingredients":["低脂牛奶 300ml","乳清蛋白 1勺","全麥吐司 2片","花生醬"],
     "steps":["牛奶加蛋白粉攪拌","吐司抹花生醬","搭配飲用"]},
    {"name":"蝦仁蛋炒糙米","category":"增肌","time":15,"kcal":480,"protein":35,"carbs":50,"fiber":2,
     "ingredients":["蝦仁 150g","雞蛋 1顆","糙米飯 1碗","蔥花蒜末醬油"],
     "steps":["蝦仁炒至變色","推開炒蛋","加糙米飯翻炒","加調味即可"]},
    {"name":"希臘乳酪燕麥隔夜","category":"增肌","time":5,"kcal":350,"protein":25,"carbs":42,"fiber":5,
     "ingredients":["燕麥片 60g","希臘乳酪 150g","牛奶 150ml","奇亞籽 10g","蜂蜜"],
     "steps":["燕麥加牛奶奇亞籽混合","加希臘乳酪拌勻","冰箱放一夜","早晨淋蜂蜜即可"]},
    {"name":"牛肉番薯烤盤","category":"增肌","time":35,"kcal":560,"protein":42,"carbs":48,"fiber":5,
     "ingredients":["瘦牛肉 180g","番薯 200g","西蘭花 150g","橄欖油鹽迷迭香"],
     "steps":["食材切塊拌橄欖油","200°C烤30分鐘","中途翻面一次"]},
    {"name":"豆漿燕麥蛋白早餐","category":"增肌","time":8,"kcal":360,"protein":26,"carbs":40,"fiber":5,
     "ingredients":["無糖豆漿 300ml","燕麥片 50g","乳清蛋白 0.5勺","堅果 20g"],
     "steps":["豆漿加熱","加燕麥片煮3分鐘","加蛋白粉攪勻","撒堅果"]},
    {"name":"烤鮭魚番薯飯","category":"增肌","time":25,"kcal":540,"protein":35,"carbs":52,"fiber":4,
     "ingredients":["鮭魚 150g","番薯 150g","白飯 1碗","醬油薑末蔥花"],
     "steps":["番薯切塊蒸熟","鮭魚180°C烤15分鐘","配飯淋醬油薑末"]},
    {"name":"雞蛋豆腐蓋飯","category":"增肌","time":15,"kcal":450,"protein":32,"carbs":50,"fiber":2,
     "ingredients":["雞蛋 3顆","嫩豆腐 150g","白飯 1碗","蔥花醬油味醂"],
     "steps":["醬油味醂煮滾","豆腐切塊加入","蛋液倒入半熟即可","鋪在白飯上"]},
    {"name":"增肌三明治","category":"增肌","time":8,"kcal":480,"protein":38,"carbs":42,"fiber":4,
     "ingredients":["全麥麵包 2片","雞胸肉 100g","雞蛋 2顆","生菜番茄","低脂起司"],
     "steps":["雞肉煎熟切片","炒蛋","麵包夾入全部食材即可"]},
    {"name":"章魚燙蔬菜沙拉","category":"增肌","time":12,"kcal":300,"protein":28,"carbs":15,"fiber":4,
     "ingredients":["章魚 150g","西蘭花 150g","紅蘿蔔 100g","芥末醬油"],
     "steps":["章魚燙熟切片","蔬菜燙熟","擺盤淋芥末醬油"]},
    {"name":"乳清蛋白燕麥餅","category":"增肌","time":10,"kcal":380,"protein":30,"carbs":38,"fiber":4,
     "ingredients":["燕麥片 80g","乳清蛋白 1勺","雞蛋 1顆","牛奶 50ml","香蕉半條"],
     "steps":["全部混合成麵糊","平底鍋煎至兩面金黃","配水果食用"]},
    {"name":"牛肉炒菠菜飯","category":"增肌","time":15,"kcal":500,"protein":40,"carbs":48,"fiber":3,
     "ingredients":["瘦牛肉 150g","菠菜 200g","糙米飯 1碗","蒜頭醬油麻油"],
     "steps":["牛肉切片醃製","熱鍋炒牛肉至熟","加菠菜翻炒","配飯淋麻油"]},
    {"name":"低脂起司蛋卷","category":"增肌","time":8,"kcal":320,"protein":26,"carbs":4,"fiber":1,
     "ingredients":["雞蛋 3顆","低脂起司 2片","菠菜 50g","鹽胡椒"],
     "steps":["蛋液加鹽打散","平底鍋倒入蛋液","放起司和菠菜","捲起即可"]},
    {"name":"雞腿飯便當","category":"增肌","time":30,"kcal":580,"protein":42,"carbs":55,"fiber":3,
     "ingredients":["雞腿 1隻","糙米飯 1碗","番茄 1個","生菜","醬油蒜頭"],
     "steps":["雞腿醃製30分鐘","200°C烤25分鐘","配飯和蔬菜"]},
    {"name":"蛋白質沙拉碗","category":"增肌","time":10,"kcal":420,"protein":36,"carbs":20,"fiber":6,
     "ingredients":["雞胸肉 120g","水煮蛋 2顆","酪梨半個","混合生菜","橄欖油醋"],
     "steps":["雞肉煎熟切片","水煮蛋切半","酪梨切片","擺盤淋橄欖油醋"]},
    {"name":"豬里肌炒蔬菜飯","category":"增肌","time":15,"kcal":480,"protein":35,"carbs":50,"fiber":4,
     "ingredients":["豬里肌 150g","西蘭花 150g","玉米粒","糙米飯 1碗","醬油薑末"],
     "steps":["里肌切片醃製","熱鍋炒熟","加蔬菜翻炒","配飯"]},
    {"name":"花生醬香蕉蛋白shake","category":"增肌","time":3,"kcal":450,"protein":32,"carbs":48,"fiber":4,
     "ingredients":["乳清蛋白 1勺","花生醬 1湯匙","香蕉 1條","牛奶 250ml","燕麥片 20g"],
     "steps":["全部放入攪拌機","攪打至順滑即可"]},
    {"name":"烤蝦藜麥飯","category":"增肌","time":20,"kcal":460,"protein":36,"carbs":45,"fiber":5,
     "ingredients":["大蝦 200g","藜麥 80g","蒜頭橄欖油迷迭香","檸檬汁"],
     "steps":["藜麥煮熟","大蝦拌調味200°C烤12分鐘","配藜麥淋檸檬汁"]},
    {"name":"高蛋白炒麵","category":"增肌","time":15,"kcal":520,"protein":38,"carbs":55,"fiber":3,
     "ingredients":["雞胸肉 120g","雞蛋 1顆","全麥麵條 80g","豆芽 100g","醬油麻油"],
     "steps":["麵條煮熟備用","炒雞肉和蛋","加麵條豆芽翻炒","加調味即可"]},
    {"name":"奶昔蛋白鬆餅","category":"增肌","time":12,"kcal":400,"protein":32,"carbs":40,"fiber":3,
     "ingredients":["乳清蛋白 1勺","燕麥粉 60g","雞蛋 1顆","牛奶 100ml","藍莓"],
     "steps":["混合麵糊","鍋中煎至兩面金黃","配藍莓食用"]},

    # ══ 減脂食譜 (30個) ══════════════════════════════════════
    {"name":"低卡雞絲蔬菜碗","category":"減脂","time":15,"kcal":280,"protein":30,"carbs":12,"fiber":4,
     "ingredients":["雞胸肉 120g","黃瓜 1條","番茄 1個","豆芽 100g","醬油醋芝麻"],
     "steps":["雞胸肉水煮撕絲","黃瓜拍扁番茄切塊","豆芽燙熟","全部混合淋醬"]},
    {"name":"番薯烤雞腿","category":"減脂","time":30,"kcal":380,"protein":32,"carbs":38,"fiber":5,
     "ingredients":["雞腿 1隻（去皮）","番薯 150g","蒜頭迷迭香","橄欖油少許"],
     "steps":["烤箱預熱200°C","雞腿去皮加調味","番薯切塊拌橄欖油","烤25分鐘"]},
    {"name":"西蘭花蒸蛋","category":"減脂","time":15,"kcal":180,"protein":16,"carbs":8,"fiber":3,
     "ingredients":["雞蛋 2顆","西蘭花 150g","低鈉醬油","芝麻油"],
     "steps":["西蘭花蒸5分鐘","蛋液加溫水1:1.5","蓋保鮮膜蒸10分鐘","加西蘭花淋醬油"]},
    {"name":"菠菜吞拿魚沙拉","category":"減脂","time":5,"kcal":200,"protein":28,"carbs":6,"fiber":3,
     "ingredients":["吞拿魚罐頭 1罐","菠菜 150g","番茄 1個","檸檬汁黑胡椒"],
     "steps":["菠菜洗淨","吞拿魚瀝乾","番茄切塊混合","淋檸檬汁撒黑胡椒"]},
    {"name":"豆腐蔬菜湯","category":"減脂","time":15,"kcal":150,"protein":12,"carbs":10,"fiber":3,
     "ingredients":["板豆腐 200g","菠菜 100g","番茄 1個","低鈉雞湯","薑片"],
     "steps":["煮滾雞湯加薑片","豆腐切塊煮3分鐘","加番茄再煮2分鐘","最後加菠菜調味"]},
    {"name":"蒸魚蔬菜套餐","category":"減脂","time":20,"kcal":250,"protein":28,"carbs":12,"fiber":4,
     "ingredients":["白身魚 150g","西蘭花 200g","薑絲蔥花醬油","麻油幾滴"],
     "steps":["魚加薑絲上鍋蒸10分鐘","西蘭花同時蒸熟","淋醬油麻油即可"]},
    {"name":"黃瓜蝦仁沙拉","category":"減脂","time":10,"kcal":180,"protein":22,"carbs":8,"fiber":2,
     "ingredients":["蝦仁 150g","黃瓜 2條","番茄 1個","醋醬油芝麻"],
     "steps":["蝦仁燙熟","黃瓜番茄切片","混合淋醬即可"]},
    {"name":"苦瓜炒蛋","category":"減脂","time":10,"kcal":160,"protein":12,"carbs":8,"fiber":3,
     "ingredients":["苦瓜 1條","雞蛋 2顆","蒜頭鹽"],
     "steps":["苦瓜去瓤切片汆燙","熱鍋炒蛋","加苦瓜翻炒","加鹽調味"]},
    {"name":"番茄蛋花湯","category":"減脂","time":10,"kcal":120,"protein":8,"carbs":10,"fiber":2,
     "ingredients":["番茄 2個","雞蛋 1顆","低鈉雞湯","蔥花鹽"],
     "steps":["雞湯煮滾","番茄切塊加入","蛋液打散淋入","加蔥花調味"]},
    {"name":"涼拌菠菜","category":"減脂","time":8,"kcal":80,"protein":5,"carbs":8,"fiber":3,
     "ingredients":["菠菜 200g","蒜末醬油","麻油醋少許"],
     "steps":["菠菜燙熟擠乾","蒜末醬油麻油醋混合","拌勻即可"]},
    {"name":"烤蔬菜雞肉串","category":"減脂","time":25,"kcal":280,"protein":28,"carbs":15,"fiber":4,
     "ingredients":["雞胸肉 150g","彩椒 2個","洋蔥 1個","橄欖油鹽迷迭香"],
     "steps":["食材切塊串串","拌橄欖油調味","200°C烤20分鐘"]},
    {"name":"低卡豆腐沙拉","category":"減脂","time":8,"kcal":160,"protein":14,"carbs":8,"fiber":2,
     "ingredients":["嫩豆腐 200g","番茄 1個","黃瓜 1條","日式醬油芝麻"],
     "steps":["豆腐切塊","番茄黃瓜切丁","擺盤淋醬油撒芝麻"]},
    {"name":"蒸蝦蔬菜盤","category":"減脂","time":15,"kcal":200,"protein":26,"carbs":10,"fiber":4,
     "ingredients":["大蝦 200g","西蘭花 200g","蒜末醬油"],
     "steps":["大蝦和西蘭花蒸熟","蒜末醬油拌勻","蘸醬食用"]},
    {"name":"雞肉蔬菜湯麵","category":"減脂","time":20,"kcal":320,"protein":30,"carbs":35,"fiber":4,
     "ingredients":["雞胸肉 100g","全麥麵條 60g","蔬菜任意","低鈉雞湯"],
     "steps":["雞湯煮開","加麵條煮熟","雞肉切片加入","加蔬菜煮熟調味"]},
    {"name":"涼拌黃瓜雞絲","category":"減脂","time":12,"kcal":180,"protein":22,"carbs":8,"fiber":2,
     "ingredients":["雞胸肉 100g","黃瓜 2條","蒜末醋醬油","芝麻"],
     "steps":["雞肉煮熟撕絲","黃瓜拍扁","混合調味拌勻","撒芝麻"]},
    {"name":"番薯蒸蛋","category":"減脂","time":20,"kcal":220,"protein":12,"carbs":32,"fiber":4,
     "ingredients":["番薯 150g","雞蛋 2顆","牛奶 100ml","少許鹽"],
     "steps":["番薯蒸熟壓泥","蛋液加牛奶鹽混合","加番薯泥蒸10分鐘"]},
    {"name":"秋葵炒蝦","category":"減脂","time":12,"kcal":190,"protein":20,"carbs":10,"fiber":4,
     "ingredients":["秋葵 150g","蝦仁 120g","蒜頭醬油"],
     "steps":["秋葵切段汆燙","蝦仁炒至變色","加秋葵翻炒調味"]},
    {"name":"減脂雜糧碗","category":"減脂","time":25,"kcal":300,"protein":12,"carbs":52,"fiber":8,
     "ingredients":["糙米 50g","番薯 100g","藜麥 30g","玉米半條","蔬菜"],
     "steps":["雜糧混合煮熟","蔬菜蒸熟","擺盤少鹽調味"]},
    {"name":"魚片豆腐湯","category":"減脂","time":15,"kcal":210,"protein":24,"carbs":6,"fiber":1,
     "ingredients":["白身魚片 150g","板豆腐 150g","薑片蔥花","低鈉高湯"],
     "steps":["高湯煮開加薑片","豆腐切塊加入","加魚片煮熟","加蔥花調味"]},
    {"name":"蘑菇雞蛋炒飯","category":"減脂","time":12,"kcal":300,"protein":16,"carbs":42,"fiber":3,
     "ingredients":["蘑菇 150g","雞蛋 2顆","糙米飯 1碗","醬油少許"],
     "steps":["蘑菇切片炒香","加蛋液炒熟","加飯翻炒","加少許醬油"]},
    {"name":"檸檬烤魚","category":"減脂","time":20,"kcal":220,"protein":28,"carbs":2,"fiber":0,
     "ingredients":["鱸魚 200g","檸檬 1個","橄欖油迷迭香鹽"],
     "steps":["魚身劃刀","塞入檸檬片","刷橄欖油","180°C烤18分鐘"]},
    {"name":"雞肉蔬菜炒糙米","category":"減脂","time":18,"kcal":350,"protein":30,"carbs":40,"fiber":4,
     "ingredients":["雞胸肉 100g","雜色蔬菜 200g","糙米飯 1碗","醬油少許"],
     "steps":["雞肉切丁炒熟","加蔬菜翻炒","加糙米飯翻炒均勻","調味"]},
    {"name":"低卡水果沙拉","category":"減脂","time":5,"kcal":120,"protein":4,"carbs":25,"fiber":4,
     "ingredients":["草莓 100g","藍莓 80g","奇異果 2個","希臘乳酪 100g"],
     "steps":["水果切塊","加希臘乳酪","輕輕拌勻即可"]},
    {"name":"番茄豆腐燒","category":"減脂","time":15,"kcal":170,"protein":12,"carbs":12,"fiber":2,
     "ingredients":["板豆腐 200g","番茄 2個","蒜頭醬油糖少許"],
     "steps":["豆腐煎至微黃","加番茄炒出汁","加調味燒至入味"]},
    {"name":"海鮮蔬菜鍋","category":"減脂","time":20,"kcal":250,"protein":28,"carbs":12,"fiber":4,
     "ingredients":["蝦仁 100g","魚片 100g","各式蔬菜","低鈉高湯"],
     "steps":["高湯煮開","加蔬菜煮熟","加海鮮煮2分鐘","調味即可"]},
    {"name":"豆腐蒸蛋羹","category":"減脂","time":15,"kcal":155,"protein":14,"carbs":4,"fiber":0,
     "ingredients":["嫩豆腐 150g","雞蛋 2顆","低鈉醬油","蔥花"],
     "steps":["豆腐搗碎和蛋液混合","加水攪勻","蒸12分鐘","加蔥花醬油"]},
    {"name":"芹菜炒牛肉","category":"減脂","time":12,"kcal":220,"protein":24,"carbs":8,"fiber":2,
     "ingredients":["瘦牛肉 100g","芹菜 150g","蒜頭醬油少許"],
     "steps":["牛肉切薄片醃製","大火炒牛肉","加芹菜翻炒","調味出鍋"]},
    {"name":"黑木耳炒雞蛋","category":"減脂","time":10,"kcal":150,"protein":10,"carbs":8,"fiber":4,
     "ingredients":["黑木耳 100g","雞蛋 2顆","蔥花鹽"],
     "steps":["黑木耳泡發切片","炒蛋半熟盛起","炒木耳","混合翻炒調味"]},
    {"name":"蔬菜蛋餅","category":"減脂","time":10,"kcal":200,"protein":14,"carbs":15,"fiber":3,
     "ingredients":["雞蛋 2顆","菠菜 100g","番茄 1個","少許麵粉鹽"],
     "steps":["蛋液加少許麵粉攪勻","加切碎蔬菜","平底鍋煎至兩面熟"]},
    {"name":"低熱量蕎麥麵","category":"減脂","time":10,"kcal":280,"protein":14,"carbs":48,"fiber":4,
     "ingredients":["蕎麥麵 80g","黃瓜絲 1條","雞蛋 1顆","醬油麻油醋"],
     "steps":["蕎麥麵煮熟冷水沖涼","雞蛋煮熟切半","加黃瓜絲","淋冷麵醬汁"]},

    # ══ 快速備餐 ≤15分鐘 (30個) ═══════════════════════════════
    {"name":"吞拿魚全麥三文治","category":"快速","time":5,"kcal":320,"protein":30,"carbs":34,"fiber":4,
     "ingredients":["全麥麵包 2片","吞拿魚罐頭 1罐","生菜 2片","番茄半個","低脂美乃滋少許"],
     "steps":["吞拿魚瀝乾拌美乃滋","麵包夾生菜番茄吞拿魚即可"]},
    {"name":"雞蛋炒飯","category":"快速","time":10,"kcal":420,"protein":18,"carbs":60,"fiber":1,
     "ingredients":["白飯 1碗","雞蛋 2顆","蔥花醬油鹽"],
     "steps":["熱鍋炒散雞蛋","加冷飯大火翻炒","加醬油鹽撒蔥花"]},
    {"name":"燕麥香蕉蛋白早餐","category":"快速","time":5,"kcal":350,"protein":20,"carbs":50,"fiber":4,
     "ingredients":["即食燕麥 60g","香蕉 1條","乳清蛋白粉 1勺","牛奶 200ml"],
     "steps":["燕麥加牛奶微波2分鐘","撒蛋白粉攪拌","加切片香蕉"]},
    {"name":"番茄炒蛋","category":"快速","time":10,"kcal":220,"protein":14,"carbs":12,"fiber":2,
     "ingredients":["雞蛋 2顆","番茄 2個","蒜頭鹽糖少許"],
     "steps":["番茄切塊蛋液打散","炒蛋半熟盛起","炒番茄加蛋翻炒調味"]},
    {"name":"微波鮭魚飯","category":"快速","time":10,"kcal":450,"protein":32,"carbs":52,"fiber":1,
     "ingredients":["鮭魚切片 150g","白飯 1碗","醬油薑末蔥花"],
     "steps":["鮭魚淋醬油薑末","放在飯上微波4-5分鐘","確認熟透撒蔥花"]},
    {"name":"水煮蛋沙拉碗","category":"快速","time":12,"kcal":280,"protein":20,"carbs":10,"fiber":3,
     "ingredients":["雞蛋 3顆","混合生菜","番茄 1個","黃瓜 1條","油醋醬"],
     "steps":["雞蛋煮8分鐘","蔬菜洗切","擺盤淋油醋醬"]},
    {"name":"花生醬燕麥碗","category":"快速","time":5,"kcal":380,"protein":16,"carbs":48,"fiber":6,
     "ingredients":["燕麥片 60g","花生醬 2湯匙","香蕉 1條","蜂蜜少許"],
     "steps":["燕麥微波3分鐘","加花生醬拌勻","加香蕉片淋蜂蜜"]},
    {"name":"希臘乳酪果昔","category":"快速","time":3,"kcal":300,"protein":22,"carbs":35,"fiber":3,
     "ingredients":["希臘乳酪 150g","冷凍藍莓 100g","香蕉半條","牛奶 100ml"],
     "steps":["全部放入攪拌機","攪打順滑即可"]},
    {"name":"起司蛋卷","category":"快速","time":8,"kcal":280,"protein":20,"carbs":4,"fiber":1,
     "ingredients":["雞蛋 3顆","低脂起司 2片","鹽胡椒"],
     "steps":["蛋液打散調味","平底鍋倒入煎至半熟","放起司捲起"]},
    {"name":"蒸豆腐醬汁","category":"快速","time":8,"kcal":120,"protein":12,"carbs":4,"fiber":0,
     "ingredients":["嫩豆腐 1盒","醬油蔥花麻油","薑末少許"],
     "steps":["豆腐微波1分鐘","瀝乾","淋調味醬料即可"]},
    {"name":"花生醬蘋果片","category":"快速","time":3,"kcal":170,"protein":5,"carbs":22,"fiber":4,
     "ingredients":["蘋果 1個","花生醬 2湯匙"],
     "steps":["蘋果切片","沾花生醬食用"]},
    {"name":"全麥蛋餅","category":"快速","time":8,"kcal":340,"protein":18,"carbs":38,"fiber":4,
     "ingredients":["全麥麵粉 60g","雞蛋 2顆","水","蔥花鹽"],
     "steps":["麵粉加水蛋液調成糊","平底鍋薄煎","加蔥花捲起"]},
    {"name":"豆漿+全麥吐司","category":"快速","time":3,"kcal":280,"protein":14,"carbs":38,"fiber":4,
     "ingredients":["無糖豆漿 300ml","全麥吐司 2片","花生醬或酪梨"],
     "steps":["豆漿加熱","吐司抹上花生醬或酪梨","搭配飲用"]},
    {"name":"即食鮪魚飯糰","category":"快速","time":5,"kcal":320,"protein":20,"carbs":50,"fiber":1,
     "ingredients":["白飯 1碗","鮪魚罐頭半罐","海苔","少許醬油"],
     "steps":["鮪魚瀝乾加醬油","飯加鮪魚捏成飯糰","包海苔即可"]},
    {"name":"蔬菜豆腐味增湯","category":"快速","time":8,"kcal":120,"protein":8,"carbs":10,"fiber":2,
     "ingredients":["豆腐 100g","味增 1湯匙","海帶芽","蔥花"],
     "steps":["水煮開加味增攪勻","加豆腐海帶芽","煮開加蔥花"]},
    {"name":"涼拌豆腐","category":"快速","time":5,"kcal":140,"protein":10,"carbs":4,"fiber":0,
     "ingredients":["嫩豆腐 200g","皮蛋 1個","醬油麻油蔥花"],
     "steps":["豆腐切塊擺盤","皮蛋切塊放上","淋醬油麻油撒蔥花"]},
    {"name":"雞蛋蔬菜炒麵","category":"快速","time":12,"kcal":380,"protein":18,"carbs":52,"fiber":3,
     "ingredients":["雞蛋 2顆","意大利麵 60g","番茄 1個","菠菜 100g","橄欖油蒜頭"],
     "steps":["麵條煮熟","炒香蒜頭","加番茄炒出汁","加蛋和麵條翻炒"]},
    {"name":"奇亞籽燕麥杯","category":"快速","time":5,"kcal":300,"protein":12,"carbs":42,"fiber":8,
     "ingredients":["燕麥片 50g","奇亞籽 15g","牛奶 200ml","蜂蜜藍莓"],
     "steps":["燕麥奇亞籽加牛奶","微波2分鐘或泡10分鐘","加藍莓蜂蜜"]},
    {"name":"墨西哥雞肉卷","category":"快速","time":12,"kcal":380,"protein":28,"carbs":38,"fiber":4,
     "ingredients":["全麥薄餅 1張","雞胸肉 100g","生菜番茄黃瓜","希臘乳酪"],
     "steps":["雞肉煎熟切片","薄餅放上所有食材","捲起即可"]},
    {"name":"毛豆蛋炒飯","category":"快速","time":10,"kcal":420,"protein":22,"carbs":58,"fiber":4,
     "ingredients":["毛豆 100g","雞蛋 2顆","白飯 1碗","醬油鹽"],
     "steps":["炒蛋","加毛豆翻炒","加冷飯大火炒","加調味"]},
    {"name":"吞拿魚沙律碗","category":"快速","time":5,"kcal":220,"protein":26,"carbs":8,"fiber":3,
     "ingredients":["吞拿魚罐頭 1罐","混合生菜","番茄","玉米粒","油醋醬"],
     "steps":["吞拿魚瀝乾","蔬菜擺盤","放上吞拿魚淋醬"]},
    {"name":"全麥土司蛋","category":"快速","time":5,"kcal":280,"protein":16,"carbs":32,"fiber":4,
     "ingredients":["全麥吐司 2片","雞蛋 2顆","番茄 1個","鹽"],
     "steps":["蛋煎熟（荷包蛋或炒蛋）","番茄切片","疊在吐司上"]},
    {"name":"番茄蛋湯麵","category":"快速","time":12,"kcal":320,"protein":16,"carbs":45,"fiber":3,
     "ingredients":["麵條 70g","番茄 2個","雞蛋 2顆","低鈉雞湯"],
     "steps":["雞湯煮開","加番茄煮軟","加麵條煮熟","加蛋液攪散"]},
    {"name":"簡易親子丼","category":"快速","time":12,"kcal":400,"protein":24,"carbs":52,"fiber":1,
     "ingredients":["雞腿肉 100g","雞蛋 2顆","白飯 1碗","醬油味醂洋蔥"],
     "steps":["洋蔥炒軟","加雞肉炒熟","加醬油味醂","倒蛋液半熟","鋪在白飯上"]},
    {"name":"鮮蝦炒時蔬","category":"快速","time":10,"kcal":200,"protein":20,"carbs":10,"fiber":3,
     "ingredients":["蝦仁 150g","彩椒 2個","蒜頭醬油"],
     "steps":["大火炒香蒜頭","加蝦仁炒變色","加彩椒翻炒","調味出鍋"]},
    {"name":"奶油蘑菇湯","category":"快速","time":12,"kcal":180,"protein":6,"carbs":14,"fiber":2,
     "ingredients":["蘑菇 200g","低脂牛奶 200ml","洋蔥半個","橄欖油鹽胡椒"],
     "steps":["炒香洋蔥和蘑菇","加牛奶煮開","調味即可（可攪打）"]},
    {"name":"冷凍蔬菜炒蛋","category":"快速","time":8,"kcal":240,"protein":14,"carbs":18,"fiber":4,
     "ingredients":["冷凍什錦蔬菜 200g","雞蛋 2顆","醬油鹽"],
     "steps":["蔬菜直接入鍋炒熱","推開加蛋液","炒熟調味"]},
    {"name":"蔥花起司蛋餅","category":"快速","time":8,"kcal":300,"protein":18,"carbs":24,"fiber":2,
     "ingredients":["雞蛋 2顆","蔥花","低脂起司 2片","薄餅皮 1張"],
     "steps":["蛋液加蔥花打散","薄餅鍋中煎","倒蛋液煎熟","加起司捲起"]},
    {"name":"豆腐蒸蛋快速版","category":"快速","time":8,"kcal":150,"protein":14,"carbs":4,"fiber":0,
     "ingredients":["嫩豆腐 150g","雞蛋 2顆","醬油蔥花"],
     "steps":["豆腐搗碎","加蛋液混合","微波3-4分鐘","淋醬油撒蔥花"]},
    {"name":"即食燕麥蛋白早餐碗","category":"快速","time":3,"kcal":320,"protein":24,"carbs":38,"fiber":5,
     "ingredients":["即食燕麥 50g","蛋白粉 1勺","奇亞籽 10g","牛奶 200ml","草莓"],
     "steps":["燕麥奇亞籽加牛奶微波","加蛋白粉攪勻","放草莓即可"]},
    {"name":"韓式石鍋拌飯（簡易）","category":"快速","time":12,"kcal":400,"protein":18,"carbs":60,"fiber":4,
     "ingredients":["白飯 1碗","雞蛋 1顆","各色蔬菜","韓式辣醬少許","麻油"],
     "steps":["蔬菜各自炒或燙熟","飯上擺蔬菜","中間放荷包蛋","淋麻油辣醬拌勻"]},

    # ══ 高蛋白零食 (30個) ═════════════════════════════════════
    {"name":"水煮蛋+毛豆","category":"零食","time":10,"kcal":191,"protein":17,"carbs":9,"fiber":4,
     "ingredients":["雞蛋 2顆","毛豆 100g","少許鹽"],
     "steps":["雞蛋冷水煮8分鐘","毛豆煮5分鐘","撒鹽即可"]},
    {"name":"希臘乳酪水果杯","category":"零食","time":3,"kcal":170,"protein":12,"carbs":18,"fiber":2,
     "ingredients":["希臘乳酪 150g","藍莓或草莓 80g","蜂蜜少許"],
     "steps":["乳酪倒入杯","放水果淋蜂蜜"]},
    {"name":"花生醬蘋果片","category":"零食","time":3,"kcal":170,"protein":5,"carbs":22,"fiber":4,
     "ingredients":["蘋果 1個","花生醬 2湯匙"],
     "steps":["蘋果切片沾花生醬"]},
    {"name":"豆腐泥蔬菜沾醬","category":"零食","time":5,"kcal":120,"protein":10,"carbs":6,"fiber":2,
     "ingredients":["嫩豆腐 150g","醬油麻油蔥花","黃瓜紅蘿蔔條"],
     "steps":["豆腐壓碎拌調味","用蔬菜條沾食"]},
    {"name":"吞拿魚脆餅","category":"零食","time":5,"kcal":180,"protein":18,"carbs":14,"fiber":1,
     "ingredients":["吞拿魚罐頭半罐","全麥脆餅","番茄片","黑胡椒"],
     "steps":["吞拿魚瀝乾","鋪在脆餅上加番茄","撒黑胡椒"]},
    {"name":"蛋白棒自製版","category":"零食","time":15,"kcal":220,"protein":18,"carbs":24,"fiber":3,
     "ingredients":["燕麥粉 60g","蛋白粉 1勺","花生醬 2湯匙","蜂蜜 1湯匙"],
     "steps":["全部混合成糰","壓成方形","冰箱冷藏30分鐘切塊"]},
    {"name":"煙燻鮭魚黃瓜卷","category":"零食","time":5,"kcal":130,"protein":14,"carbs":4,"fiber":1,
     "ingredients":["煙燻鮭魚 80g","黃瓜 1條","低脂奶油起司","黑胡椒"],
     "steps":["黃瓜切薄片","抹奶油起司","捲上鮭魚即可"]},
    {"name":"蘑菇炒蛋小食","category":"零食","time":8,"kcal":150,"protein":12,"carbs":4,"fiber":1,
     "ingredients":["蘑菇 100g","雞蛋 2顆","蒜頭鹽"],
     "steps":["蘑菇炒軟","加蛋液炒熟","調味"]},
    {"name":"乳清蛋白布丁","category":"零食","time":5,"kcal":180,"protein":20,"carbs":14,"fiber":0,
     "ingredients":["乳清蛋白粉 1勺","牛奶 150ml","奇亞籽 10g"],
     "steps":["牛奶加蛋白粉攪勻","加奇亞籽","冰箱靜置30分鐘"]},
    {"name":"水煮毛豆撒鹽","category":"零食","time":8,"kcal":120,"protein":11,"carbs":9,"fiber":4,
     "ingredients":["毛豆 150g","鹽少許"],
     "steps":["毛豆加鹽水煮5分鐘","瀝乾撒鹽"]},
    {"name":"全麥起司吐司","category":"零食","time":3,"kcal":200,"protein":10,"carbs":22,"fiber":3,
     "ingredients":["全麥吐司 1片","低脂起司 2片"],
     "steps":["吐司放起司","烤箱或烤麵包機烤2分鐘"]},
    {"name":"希臘乳酪堅果","category":"零食","time":2,"kcal":200,"protein":12,"carbs":10,"fiber":2,
     "ingredients":["希臘乳酪 120g","混合堅果 20g","肉桂粉少許"],
     "steps":["乳酪加堅果","撒肉桂粉即可"]},
    {"name":"煙燻鮭魚沙律","category":"零食","time":5,"kcal":160,"protein":16,"carbs":4,"fiber":1,
     "ingredients":["煙燻鮭魚 80g","酪梨半個","檸檬汁","黑胡椒"],
     "steps":["酪梨切丁","加鮭魚切片","淋檸檬汁撒黑胡椒"]},
    {"name":"天貝脆片","category":"零食","time":15,"kcal":200,"protein":16,"carbs":8,"fiber":2,
     "ingredients":["天貝 100g","橄欖油鹽醬油"],
     "steps":["天貝切薄片","拌橄欖油醬油","200°C烤12分鐘至脆"]},
    {"name":"豆漿蛋白飲","category":"零食","time":3,"kcal":150,"protein":15,"carbs":8,"fiber":1,
     "ingredients":["無糖豆漿 250ml","乳清蛋白半勺","可可粉少許"],
     "steps":["全部混合攪拌均勻","即可飲用"]},
    {"name":"起司雞蛋卷","category":"零食","time":8,"kcal":220,"protein":18,"carbs":2,"fiber":0,
     "ingredients":["雞蛋 2顆","低脂起司 2片","鹽"],
     "steps":["蛋液煎薄片","放起司捲起","切段即可"]},
    {"name":"燕麥蛋白球","category":"零食","time":10,"kcal":160,"protein":10,"carbs":18,"fiber":3,
     "ingredients":["燕麥片 40g","蛋白粉半勺","花生醬 1湯匙","蜂蜜少許"],
     "steps":["全部混合","揉成小球","冰箱定型30分鐘"]},
    {"name":"鮪魚黃瓜船","category":"零食","time":5,"kcal":120,"protein":16,"carbs":4,"fiber":1,
     "ingredients":["黃瓜 1條","鮪魚罐頭半罐","低脂美乃滋","黑胡椒"],
     "steps":["黃瓜切半挖空","鮪魚拌美乃滋","填入黃瓜船撒黑胡椒"]},
    {"name":"雞蛋豆腐小食","category":"零食","time":8,"kcal":160,"protein":16,"carbs":4,"fiber":0,
     "ingredients":["板豆腐 150g","雞蛋 1顆","醬油少許蔥花"],
     "steps":["豆腐切塊煎香","加蛋液","淋醬油撒蔥花"]},
    {"name":"奇亞籽蛋白果凍","category":"零食","time":5,"kcal":140,"protein":12,"carbs":14,"fiber":5,
     "ingredients":["奇亞籽 20g","牛奶 200ml","蛋白粉半勺","藍莓"],
     "steps":["牛奶加蛋白粉奇亞籽攪勻","冰箱靜置4小時","加藍莓"]},
    {"name":"煮毛豆蝦仁","category":"零食","time":12,"kcal":190,"protein":22,"carbs":8,"fiber":3,
     "ingredients":["毛豆 100g","蝦仁 100g","鹽少許"],
     "steps":["蝦仁燙熟","毛豆煮熟","撒鹽混合即可"]},
    {"name":"花生醬香蕉燕麥條","category":"零食","time":20,"kcal":250,"protein":10,"carbs":35,"fiber":4,
     "ingredients":["燕麥片 80g","香蕉 1條","花生醬 2湯匙","蜂蜜"],
     "steps":["香蕉壓泥加所有材料","鋪烤盤","180°C烤15分鐘切條"]},
    {"name":"低脂茅屋起司碗","category":"零食","time":2,"kcal":140,"protein":14,"carbs":6,"fiber":0,
     "ingredients":["低脂茅屋起司 150g","番茄切塊","黑胡椒橄欖油"],
     "steps":["起司倒入碗","加番茄淋橄欖油撒胡椒"]},
    {"name":"即食鮪魚沙律","category":"零食","time":3,"kcal":150,"protein":20,"carbs":4,"fiber":1,
     "ingredients":["鮪魚罐頭 1罐","番茄 1個","檸檬汁黑胡椒"],
     "steps":["鮪魚瀝乾","加切塊番茄","淋檸檬汁撒黑胡椒"]},
    {"name":"堅果蛋白能量球","category":"零食","time":10,"kcal":200,"protein":8,"carbs":20,"fiber":3,
     "ingredients":["混合堅果 40g","椰棗 4顆","燕麥片 30g","可可粉"],
     "steps":["椰棗去核食物機打碎","加堅果燕麥可可","揉成球冷藏"]},
    {"name":"豆腐蘸芝麻醬","category":"零食","time":3,"kcal":160,"protein":12,"carbs":6,"fiber":1,
     "ingredients":["嫩豆腐 200g","芝麻醬 1湯匙","醬油蔥花"],
     "steps":["豆腐切塊","調芝麻醬汁","淋上即可"]},
    {"name":"雞胸肉小食盒","category":"零食","time":15,"kcal":170,"protein":28,"carbs":0,"fiber":0,
     "ingredients":["雞胸肉 120g","鹽胡椒檸檬汁","橄欖油"],
     "steps":["雞肉醃製","煎熟切塊","加檸檬汁即可"]},
    {"name":"草莓希臘乳酪杯","category":"零食","time":3,"kcal":140,"protein":10,"carbs":14,"fiber":2,
     "ingredients":["希臘乳酪 100g","草莓 100g","奇亞籽 5g"],
     "steps":["乳酪倒入杯","切草莓放上","撒奇亞籽"]},
    {"name":"炒毛豆豆腐","category":"零食","time":10,"kcal":180,"protein":14,"carbs":10,"fiber":3,
     "ingredients":["毛豆 80g","板豆腐 100g","醬油少許鹽"],
     "steps":["豆腐切丁煎至金黃","加毛豆翻炒","加醬油調味"]},
    {"name":"藍莓蛋白慕斯","category":"零食","time":5,"kcal":160,"protein":16,"carbs":12,"fiber":2,
     "ingredients":["希臘乳酪 150g","蛋白粉半勺","藍莓 80g"],
     "steps":["乳酪加蛋白粉攪勻","倒入杯中","加藍莓"]},
    {"name":"鹽烤堅果","category":"零食","time":8,"kcal":190,"protein":5,"carbs":8,"fiber":2,
     "ingredients":["混合堅果 40g","鹽少許","迷迭香可選"],
     "steps":["堅果拌鹽","180°C烤6分鐘","冷卻即可"]},
]

# ── 智能飲食建議邏輯 ─────────────────────────────────────
def get_food_suggestions(kcal_gap: float, protein_gap: float) -> str:
    """
    根據熱量缺口和蛋白質缺口，從食物資料庫推薦合適食物。
    kcal_gap > 0 代表還可以吃，< 0 代表超標
    protein_gap > 0 代表蛋白質不足
    """
    lines = []

    # 已超標
    if kcal_gap <= 0 and protein_gap <= 0:
        return "✅ 今日熱量和蛋白質均已達標，無需額外補充\\!"

    if kcal_gap <= 0 and protein_gap > 0:
        # 熱量超標但蛋白質不足 → 推薦低卡高蛋白
        lines.append(f"⚠️ 熱量已達標，但蛋白質還差 *{int(protein_gap)}g*\n")
        lines.append("推薦低卡高蛋白補充：")
        candidates = [
            (name, f) for name, f in FOOD_DB.items()
            if "高蛋白" in f["tags"] and f["kcal"] < 150 and f["protein"] >= 6
        ]
        candidates.sort(key=lambda x: -x[1]["protein"] / max(x[1]["kcal"], 1))
        for name, f in candidates[:5]:
            lines.append(f"  • {escape_md(name)}  {f['kcal']}kcal \\| 蛋白{f['protein']}g \\({escape_md(f['per'])}\\)")
        return "\n".join(lines)

    # 熱量不足
    lines.append(f"📊 今日還可攝取 *{int(kcal_gap)} kcal*")
    if protein_gap > 0:
        lines.append(f"🥩 蛋白質還差 *{int(protein_gap)}g*\n")
    else:
        lines.append("")

    # 判斷場景
    if kcal_gap >= 400:
        # 缺口大 → 推薦正餐
        lines.append("🍽 *正餐推薦（由高到低蛋白質）：*")
        candidates = [
            (name, f) for name, f in FOOD_DB.items()
            if "正餐" in f["tags"] and f["kcal"] <= kcal_gap * 1.1
        ]
        candidates.sort(key=lambda x: -x[1]["protein"])
        for name, f in candidates[:5]:
            lines.append(f"  `{candidates.index((name,f))+1}` {escape_md(name)}")
            lines.append(f"     🔥{f['kcal']}kcal \\| 🥩{f['protein']}g \\({escape_md(f['per'])}\\)")

    elif kcal_gap >= 150:
        # 中等缺口 → 推薦零食或輕食
        lines.append("🥗 *輕食 \\/ 零食推薦：*")
        candidates = [
            (name, f) for name, f in FOOD_DB.items()
            if f["kcal"] <= kcal_gap and f["kcal"] >= 60
        ]
        # 蛋白質不足則優先高蛋白
        if protein_gap > 0:
            candidates.sort(key=lambda x: -x[1]["protein"])
        else:
            candidates.sort(key=lambda x: x[1]["kcal"])
        for name, f in candidates[:6]:
            lines.append(f"  • {escape_md(name)}  {f['kcal']}kcal \\| 蛋白{f['protein']}g")

    else:
        # 缺口小 → 推薦低卡選項
        lines.append("🌿 *低卡補充（缺口小）：*")
        candidates = [
            (name, f) for name, f in FOOD_DB.items()
            if f["kcal"] <= kcal_gap
        ]
        candidates.sort(key=lambda x: x[1]["kcal"])
        for name, f in candidates[:5]:
            lines.append(f"  • {escape_md(name)}  {f['kcal']}kcal")

    # 獨立蛋白質建議
    if protein_gap > 0:
        lines.append(f"\n🥩 *補充蛋白質（目標 \\+{int(protein_gap)}g）：*")
        prot_candidates = [
            (name, f) for name, f in FOOD_DB.items()
            if f["protein"] >= 6 and "高蛋白" in f["tags"]
        ]
        prot_candidates.sort(key=lambda x: -x[1]["protein"])
        for name, f in prot_candidates[:4]:
            lines.append(f"  • {escape_md(name)}  蛋白{f['protein']}g \\| {f['kcal']}kcal")

    return "\n".join(lines)


def get_recipe_list(category: str, page: int = 1) -> str:
    """回傳指定類別的食譜列表文字（每頁5個）"""
    cat_map = {"增肌": "增肌", "減脂": "減脂", "快速": "快速", "零食": "零食"}
    cat = cat_map.get(category, category)
    recipes = [r for r in RECIPE_DB if r["category"] == cat]
    if not recipes:
        return f"*{escape_md(category)} 食譜*\n\n暫無食譜"

    PER_PAGE = 5
    total_pages = (len(recipes) + PER_PAGE - 1) // PER_PAGE
    page = max(1, min(page, total_pages))
    start = (page - 1) * PER_PAGE
    page_recipes = recipes[start:start + PER_PAGE]

    EMOJI = {"增肌": "🏋️", "減脂": "🔥", "快速": "⚡", "零食": "🥩"}
    emoji = EMOJI.get(cat, "📖")
    lines = [f"{emoji} *{escape_md(category)} 食譜* \\({escape_md(str(page))}/{escape_md(str(total_pages))} 頁\\)\n"]
    for i, r in enumerate(page_recipes, start + 1):
        carbs_str = f" \\| 🌾{escape_md(str(r.get('carbs',0)))}g" if r.get('carbs') else ""
        lines.append(
            f"`{i:02d}` *{escape_md(r['name'])}*\n"
            f"     ⏱{escape_md(str(r['time']))}分 \\| 🔥{escape_md(str(r['kcal']))}kcal \\| 🥩{escape_md(str(r['protein']))}g{carbs_str}"
        )
    nav = []
    if page > 1:            nav.append(f"◀️ 上一頁：輸入 `食譜{escape_md(category)}{escape_md(str(page-1))}`")
    if page < total_pages:  nav.append(f"▶️ 下一頁：輸入 `食譜{escape_md(category)}{escape_md(str(page+1))}`")
    lines.append("\n_輸入食譜編號查看詳細步驟，例如：`01`_")
    if nav:
        lines.append("\n".join(nav))
    return "\n\n".join(lines)


def get_recipe_detail(category: str, index: int) -> str:
    """回傳食譜詳細內容"""
    cat_map = {"增肌": "增肌", "減脂": "減脂", "快速": "快速", "零食": "零食"}
    cat = cat_map.get(category, category)
    recipes = [r for r in RECIPE_DB if r["category"] == cat]
    if index < 1 or index > len(recipes):
        return "❌ 食譜編號不存在"
    r = recipes[index - 1]
    ingr = "\n".join(f"  • {escape_md(i)}" for i in r["ingredients"])
    steps = "\n".join(f"  `{j}.` {escape_md(s)}" for j, s in enumerate(r["steps"], 1))
    return (
        f"📖 *{escape_md(r['name'])}*\n\n"
        f"⏱ 時間：{escape_md(str(r['time']))} 分鐘\n"
        f"🔥 熱量：{escape_md(str(r['kcal']))} kcal\n"
        f"🥩 蛋白質：{escape_md(str(r['protein']))} g\n\n"
        f"🛒 *材料：*\n{ingr}\n\n"
        f"👨‍🍳 *步驟：*\n{steps}"
    )


def get_recipe_by_gap(kcal_gap: float, protein_gap: float) -> str:
    """根據缺口自動推薦食譜類別和具體食譜"""
    lines = []

    # 決定推薦類別
    if kcal_gap > 300 and protein_gap > 15:
        rec_cat = "增肌"
        reason  = f"熱量缺口 {int(kcal_gap)}kcal \\+ 蛋白質不足 {int(protein_gap)}g，推薦增肌食譜"
    elif kcal_gap > 300:
        rec_cat = "快速"
        reason  = f"熱量缺口 {int(kcal_gap)}kcal，推薦快速補充"
    elif kcal_gap <= 0 and protein_gap > 10:
        rec_cat = "零食"
        reason  = f"熱量已達標，蛋白質不足 {int(protein_gap)}g，推薦高蛋白零食"
    elif kcal_gap > 0 and kcal_gap <= 300:
        rec_cat = "減脂"
        reason  = f"熱量缺口小（{int(kcal_gap)}kcal），推薦低卡食譜"
    else:
        return "✅ 今日飲食已均衡，無需特別推薦\\!"

    lines.append(f"💡 *根據今日狀況推薦*\n_{escape_md(reason)}_\n")
    lines.append(get_recipe_list(rec_cat))
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
#  Gemini 異步調用
# ════════════════════════════════════════════════════════════

async def analyze_food(image_bytes: bytes | None, text_desc: str | None) -> dict:
    prompt = (
        "你是專業營養師。分析食物並以純 JSON 回覆，不含任何多餘文字或 markdown。\n"
        '格式：{"food_name":"xxx","calories":000,"protein":0.0,"carbs":0.0,"fiber":0.0}\n'
        "calories(kcal), protein(g), carbs碳水化合物(g), fiber膳食纖維(g)。\n"
        "所有數值必須是數字，不可為 null。\n"
    )
    if text_desc:
        prompt += f"食物描述：{text_desc}\n"

    parts: list = [prompt]
    if image_bytes:
        parts.append(genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

    def _call():
        return gemini_client.models.generate_content(model=GEMINI_MODEL_ID, contents=parts).text

    return extract_json(await asyncio.to_thread(_call))


async def analyze_activity_image(image_bytes: bytes) -> dict:
    """
    用 Gemini Vision 分析手環/健康App截圖，提取步數和卡路里消耗。
    回傳 {"steps": int, "calories_burned": int, "source": str}
    """
    prompt = (
        "你是一個數據提取專家。請仔細分析這張手環或健康App的截圖，"
        "提取以下數據並以純 JSON 回覆，不含任何多餘文字或 markdown：\n"
        '{"steps": 整數步數, "calories_burned": 整數卡路里消耗, "source": "識別到的App或設備名稱"}\n\n'
        "規則：\n"
        "- steps：找截圖中的步數（步/Steps/歩數），若無則填 0\n"
        "- calories_burned：找活動消耗卡路里（kcal/Cal/千卡），不是BMR，若無則填 0\n"
        "- 若截圖中有多個卡路里數值，選「活動消耗」或「運動消耗」，不選「基礎代謝」\n"
        "- source：如 'Garmin', 'Apple Health', 'Google Fit', 'Xiaomi Band', 'Samsung Health' 等\n"
        "- 所有數值必須是整數"
    )
    def _call():
        parts = [prompt, genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")]
        return gemini_client.models.generate_content(model=GEMINI_MODEL_ID, contents=parts).text
    return extract_json(await asyncio.to_thread(_call))


async def get_food_nutrition_cache(food_name: str) -> dict | None:
    """
    從 Diet 表找同名食物的平均營養數據，避免重複呼叫 AI。
    回傳 dict 或 None（找不到）。
    """
    def _read():
        try:
            gc = get_sheet_client()
            ss = gc.open_by_key(SHEET_ID)
            ws = ensure_worksheet(ss, "Diet",
                ["Timestamp", "FoodName", "Calories(kcal)", "Protein(g)", "Carbs(g)", "Fiber(g)", "Notes"])
            rows = ws.get_all_values()[1:]
            matched = [r for r in rows if len(r) > 4 and r[1].strip() == food_name.strip()]
            if not matched:
                return None
            cals  = [float(r[2]) for r in matched if r[2]]
            prots = [float(r[3]) for r in matched if r[3]]
            carbs = [float(r[4]) for r in matched if len(r) > 4 and r[4]]
            fibs  = [float(r[5]) for r in matched if len(r) > 5 and r[5]]
            return {
                "food_name": food_name,
                "calories":  round(sum(cals)  / len(cals),  1) if cals  else 0,
                "protein":   round(sum(prots) / len(prots), 1) if prots else 0,
                "carbs":     round(sum(carbs) / len(carbs), 1) if carbs else 0,
                "fiber":     round(sum(fibs)  / len(fibs),  1) if fibs  else 0,
                "from_cache": True,
            }
        except Exception:
            return None
    return await asyncio.to_thread(_read)


async def analyze_exercise(text_desc: str | None, image_bytes: bytes | None = None) -> dict:
    """
    分析運動內容，輸出：運動名稱、時長、訓練部位分類（支援多部位）、動作列表。
    不計算卡路里。
    """
    prompt = (
        "你是專業健身教練。分析以下運動內容，以純 JSON 回覆，不含任何多餘文字或 markdown。\n"
        "格式：\n"
        '{"exercise_name":"完整運動名稱","duration_min":60,'
        '"muscle_categories":["胸","肩","手臂"],'
        '"muscle_category":"胸",'
        '"muscles":["胸大肌","三角肌","肱三頭肌"],'
        '"movements":["史密斯臥推","坐姿夾胸","側平舉"]}\n'
        "重要規則：\n"
        "- muscle_categories：列出【所有】被訓練到的部位，從以下選擇：胸、背、肩、手臂、核心、腿、全身、有氧、柔韌\n"
        "- muscle_category：muscle_categories 中最主要的單一部位（用於主分類）\n"
        "- 例：胸肩三頭訓練 → muscle_categories:[\"胸\",\"肩\",\"手臂\"], muscle_category:\"胸\"\n"
        "- 例：Push Day → muscle_categories:[\"胸\",\"肩\",\"手臂\"], muscle_category:\"胸\"\n"
        "- 例：Pull Day → muscle_categories:[\"背\",\"手臂\"], muscle_category:\"背\"\n"
        "- 例：Leg Day → muscle_categories:[\"腿\",\"核心\"], muscle_category:\"腿\"\n"
        "- muscles：必須從以下固定清單中選擇（繁體中文，選3-6個）：\n"
        "  斜方肌、胸大肌、胸小肌、三角肌、前三角肌、側三角肌、後三角肌、"
        "  背闊肌、豎脊肌、菱形肌、肱二頭肌、肱三頭肌、前臂肌、"
        "  腹直肌、腹斜肌、腹橫肌、股四頭肌、腿後肌、臀大肌、臀中肌、小腿肌、髖屈肌\n"
        "- movements：所有動作名稱（從圖片或描述中提取）\n"
        "- 若為有氧運動，muscle_categories:[\"有氧\"], muscle_category:\"有氧\", muscles:[\"腿後肌\",\"股四頭肌\"]\n"
    )
    if text_desc:
        prompt += f"運動描述：{text_desc}\n"
    if image_bytes and not text_desc:
        prompt += "請根據圖片中的動作列表識別所有訓練部位。\n"

    parts: list = [prompt]
    if image_bytes:
        parts.append(genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))

    def _call():
        return gemini_client.models.generate_content(model=GEMINI_MODEL_ID, contents=parts).text

    result = extract_json(await asyncio.to_thread(_call))
    # 確保 muscle_categories 欄位存在（向後相容）
    if "muscle_categories" not in result:
        result["muscle_categories"] = [result.get("muscle_category", "全身")]
    return result


async def analyze_body_photo(image_bytes: bytes, prev_assessment: str | None = None) -> dict:
    if prev_assessment:
        prompt = (
            "你是私人健身教練。觀察身材照，並與上次評估對比，純 JSON 回覆，不含 markdown：\n"
            '{"assessment":"當前體態評估","comparison":"與上次相比的變化（進步/退步/持平）","suggestions":"建議"}\n'
            f"上次評估：{prev_assessment}\n"
        )
    else:
        prompt = (
            "你是私人健身教練。觀察身材照給出簡短體態評估，純 JSON 回覆，不含 markdown：\n"
            '{"assessment":"...","comparison":"（首次記錄）","suggestions":"..."}'
        )
    def _call():
        return gemini_client.models.generate_content(
            model=GEMINI_MODEL_ID,
            contents=[prompt, genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")],
        ).text
    return extract_json(await asyncio.to_thread(_call))


async def ai_daily_advice(diet_rows: list, exercise_rows: list, weight_rows: list, goal_kcal: int) -> str:
    total_in   = sum(float(r[2]) for r in diet_rows     if len(r) > 2 and r[2])
    # 運動已不記錄卡路里，消耗統計為 0
    total_burn = 0.0
    net        = total_in - total_burn
    latest_wt  = weight_rows[-1][1] if weight_rows else "未記錄"

    prompt = (
        f"今日熱量攝取：{total_in:.0f} kcal，運動消耗：{total_burn:.0f} kcal，"
        f"淨熱量：{net:.0f} kcal，目標：{goal_kcal} kcal，最新體重：{latest_wt} kg。\n"
        "請用繁體中文給出 2-3 句簡短的今日健康建議，不超過 100 字，純文字回覆。"
    )
    def _call():
        return gemini_client.models.generate_content(model=GEMINI_MODEL_ID, contents=[prompt]).text

    return await asyncio.to_thread(_call)


def analyze_weight_trend_local(weight_rows: list) -> str:
    """本地計算體重趨勢，不需呼叫 AI。"""
    valid = [(r[0], float(r[1])) for r in weight_rows if len(r) > 1 and r[1] and float(r[1]) > 0]
    if len(valid) < 2:
        return "數據不足，請至少記錄 2 次體重後再查看趨勢分析。"

    weights = [w for _, w in valid[-14:]]
    dates   = [d for d, _ in valid[-14:]]

    first_w, last_w = weights[0], weights[-1]
    diff    = last_w - first_w
    days    = max(1, (datetime.strptime(dates[-1][:10], "%Y-%m-%d") -
                      datetime.strptime(dates[0][:10], "%Y-%m-%d")).days)
    weekly  = diff / days * 7

    # 趨勢判斷
    slope_per_day = diff / days
    if slope_per_day > 0.05:
        trend = "📈 持續上升"
        trend_tip = "攝取熱量可能偏高，建議檢視飲食紀錄。"
    elif slope_per_day < -0.05:
        trend = "📉 持續下降"
        trend_tip = "減重進展良好，注意攝取足夠蛋白質保護肌肉。"
    elif abs(slope_per_day) <= 0.02:
        trend = "➡️ 維持穩定"
        trend_tip = "體重控制良好，繼續保持現有習慣。"
    else:
        trend = "〰️ 小幅波動"
        trend_tip = "屬正常生理波動，持續觀察即可。"

    sign = "+" if diff >= 0 else ""
    result = (
        f"趨勢：{trend}\n"
        f"變化：{sign}{diff:.1f} kg（{days}天）\n"
        f"週均：{sign}{weekly:.2f} kg/週\n"
        f"最低：{min(weights):.1f} kg　最高：{max(weights):.1f} kg\n\n"
        f"💡 {trend_tip}"
    )
    return result


async def ai_weekly_summary(diet_rows, exercise_rows, activity_rows, weight_rows, goal_kcal) -> str:
    total_in   = sum(float(r[2]) for r in diet_rows     if len(r) > 2 and r[2])
    # 運動已不記錄卡路里，消耗統計為 0
    total_burn = 0.0
    total_steps= sum(float(r[1]) for r in activity_rows if len(r) > 1 and r[1])
    wt_list    = [float(r[1]) for r in weight_rows if len(r) > 1 and r[1] and float(r[1]) > 0]
    wt_str     = f"{wt_list[0]}kg → {wt_list[-1]}kg" if len(wt_list) >= 2 else (f"{wt_list[0]}kg" if wt_list else "無記錄")

    prompt = (
        f"本週數據：攝取熱量={total_in:.0f}kcal，消耗={total_burn:.0f}kcal，"
        f"淨熱量={total_in-total_burn:.0f}kcal，每日目標={goal_kcal}kcal，"
        f"總步數={total_steps:.0f}步，體重變化={wt_str}。\n"
        "請用繁體中文給出本週健康總結，包含表現評估和下週建議，不超過 150 字，純文字。"
    )
    def _call():
        return gemini_client.models.generate_content(model=GEMINI_MODEL_ID, contents=[prompt]).text
    return await asyncio.to_thread(_call)


async def ai_monthly_summary(diet_rows, exercise_rows, activity_rows, weight_rows, goal_kcal) -> str:
    total_in   = sum(float(r[2]) for r in diet_rows     if len(r) > 2 and r[2])
    # 運動已不記錄卡路里，消耗統計為 0
    total_burn = 0.0
    total_steps= sum(float(r[1]) for r in activity_rows if len(r) > 1 and r[1])
    wt_list    = [float(r[1]) for r in weight_rows if len(r) > 1 and r[1] and float(r[1]) > 0]
    wt_str     = f"{wt_list[0]}kg → {wt_list[-1]}kg" if len(wt_list) >= 2 else (f"{wt_list[0]}kg" if wt_list else "無記錄")
    days       = len(set(r[0][:10] for r in diet_rows if r))

    prompt = (
        f"本月數據：記錄天數={days}天，攝取熱量={total_in:.0f}kcal，消耗={total_burn:.0f}kcal，"
        f"淨熱量={total_in-total_burn:.0f}kcal，每日目標={goal_kcal}kcal，"
        f"總步數={total_steps:.0f}步，體重變化={wt_str}。\n"
        "請用繁體中文給出本月健康總結，包含整體表現、進步之處和下月目標建議，不超過 200 字，純文字。"
    )
    def _call():
        return gemini_client.models.generate_content(model=GEMINI_MODEL_ID, contents=[prompt]).text
    return await asyncio.to_thread(_call)


def parse_activity_input(text: str) -> dict:
    """
    解析活動數據輸入，格式靈活：
    支援：步數/消耗/心率點/活動點
    例如：「步數8000 消耗350 心率點12 活動點30」
    或分行輸入，或只輸入部分數值。
    """
    result = {
        "steps": None,
        "calories_burned": None,
        "heart_points": None,
        "active_minutes": None,
    }
    # 步數
    m = re.search(r"步[數数]?\s*[：:=]?\s*(\d+)", text)
    if m: result["steps"] = int(m.group(1))
    m = re.search(r"(\d+)\s*步", text)
    if m and not result["steps"]: result["steps"] = int(m.group(1))

    # 消耗熱量
    m = re.search(r"(?:消耗|卡路里|calories?)\s*[：:=]?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m: result["calories_burned"] = float(m.group(1))

    # 心率點 Heart Points
    m = re.search(r"(?:心率點|heart\s*points?|hp)\s*[：:=]?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m: result["heart_points"] = float(m.group(1))

    # 活躍分鐘 / 活動點
    m = re.search(r"(?:活動點|活躍分鐘|active\s*minutes?|move\s*minutes?)\s*[：:=]?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m: result["active_minutes"] = float(m.group(1))

    return result


# ════════════════════════════════════════════════════════════
#  Google Sheets 讀寫
# ════════════════════════════════════════════════════════════

async def append_diet_row(data: dict):
    def _write():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Diet",
            ["Timestamp", "FoodName", "Calories(kcal)", "Protein(g)", "Carbs(g)", "Fiber(g)", "Notes"])
        ws.append_row([now_str(), data.get("food_name", "未知"),
                       data.get("calories", 0), data.get("protein", 0),
                       data.get("carbs", 0),    data.get("fiber", 0),
                       data.get("notes", "")])
        total_rows = len(ws.get_all_values())
        return total_rows
    row_num = await asyncio.to_thread(_write)
    _sheet_cache.invalidate("Diet")           # ← 寫入後清除快取
    await _save_last_action("diet", row_num, data.get("food_name", "未知"))


async def append_exercise_row(data: dict):
    def _write():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Exercise",
            ["Timestamp", "ExerciseName", "Duration(min)", "MuscleCategory", "Muscles", "Movements"])
        ws.append_row([
            now_str(),
            data.get("exercise_name", "未知"),
            data.get("duration_min", 0),
            data.get("muscle_category", ""),
            ", ".join(data.get("muscles", [])),
            ", ".join(data.get("movements", [])),
        ])
        return len(ws.get_all_values())
    row_num = await asyncio.to_thread(_write)
    _sheet_cache.invalidate("Exercise")       # ← 寫入後清除快取
    await _save_last_action("exercise", row_num, data.get("exercise_name", "未知"))


async def append_weight_row(weight: float, note: str = ""):
    def _write():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Weight", ["Timestamp", "Weight(kg)", "Note"])
        ws.append_row([now_str(), weight, note])
        return len(ws.get_all_values())
    row_num = await asyncio.to_thread(_write)
    _sheet_cache.invalidate("Weight")         # ← 寫入後清除快取
    await _save_last_action("weight", row_num, f"{weight} kg")


async def _save_last_action(sheet: str, row_num: int, desc: str):
    """儲存最後一次寫入動作，供 /undo 使用"""
    val = json.dumps({"sheet": sheet, "row": row_num, "desc": desc, "time": now_str()})
    await _make_setting_saver("last_action")(val)


async def _load_last_action() -> dict | None:
    """讀取最後一次寫入動作"""
    raw = await _make_setting_loader("last_action", default="{}", cast=str)()
    try:
        d = json.loads(raw)
        return d if d.get("sheet") else None
    except Exception:
        return None


async def undo_last_action() -> str:
    """撤銷最後一筆記錄，從對應 Sheet 刪除最後一行"""
    action = await _load_last_action()
    if not action:
        return "❌ 沒有可撤銷的記錄"

    sheet_map = {
        "diet":     ("Diet",     ["Timestamp", "FoodName", "Calories(kcal)", "Protein(g)", "Carbs(g)", "Fiber(g)", "Notes"]),
        "exercise": ("Exercise", ["Timestamp", "ExerciseName", "Duration(min)", "MuscleCategory", "Muscles", "Movements"]),
        "weight":   ("Weight",   ["Timestamp", "Weight(kg)", "Note"]),
    }
    if action["sheet"] not in sheet_map:
        return "❌ 不支援撤銷此類型記錄"

    sheet_name, headers = sheet_map[action["sheet"]]
    row_num = action["row"]
    desc    = action["desc"]

    def _delete():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, sheet_name, headers)
        total = len(ws.get_all_values())
        # 確認行號仍是最後一行（防止誤刪）
        if row_num != total:
            return False
        ws.delete_rows(row_num)
        return True

    ok = await asyncio.to_thread(_delete)
    if ok:
        _sheet_cache.invalidate(sheet_name)   # ← 刪除後清除快取
        # 清除 last_action
        await _make_setting_saver("last_action")("{}")
        return f"✅ 已撤銷：{desc}（{action['time']}）"
    else:
        return "❌ 撤銷失敗，該記錄可能已被修改或不是最後一筆"


async def save_goal(goal_kcal: int):
    def _write():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Settings", ["Key", "Value", "UpdatedAt"])
        rows = ws.get_all_values()
        for i, r in enumerate(rows):
            if r and r[0] == "daily_calorie_goal":
                ws.update(f"A{i+1}:C{i+1}", [["daily_calorie_goal", str(goal_kcal), now_str()]])
                return
        ws.append_row(["daily_calorie_goal", str(goal_kcal), now_str()])
    await asyncio.to_thread(_write)


async def load_goal() -> int:
    def _read():
        try:
            gc = get_sheet_client()
            ss = gc.open_by_key(SHEET_ID)
            ws = ensure_worksheet(ss, "Settings", ["Key", "Value", "UpdatedAt"])
            for r in ws.get_all_values():
                if r and r[0] == "daily_calorie_goal":
                    return int(r[1])
        except Exception:
            pass
        return 2000
    return await asyncio.to_thread(_read)


async def save_weight_goal(target_kg: float):
    """儲存體重目標。"""
    def _write():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Settings", ["Key", "Value", "UpdatedAt"])
        rows = ws.get_all_values()
        for i, r in enumerate(rows):
            if r and r[0] == "weight_goal":
                ws.update(f"A{i+1}:C{i+1}", [["weight_goal", str(target_kg), now_str()]])
                return
        ws.append_row(["weight_goal", str(target_kg), now_str()])
    await asyncio.to_thread(_write)


async def load_weight_goal() -> float | None:
    """讀取體重目標，未設定則回傳 None。"""
    def _read():
        try:
            gc = get_sheet_client()
            ss = gc.open_by_key(SHEET_ID)
            ws = ensure_worksheet(ss, "Settings", ["Key", "Value", "UpdatedAt"])
            for r in ws.get_all_values():
                if r and r[0] == "weight_goal" and r[1]:
                    return float(r[1])
        except Exception:
            pass
        return None
    return await asyncio.to_thread(_read)


def _make_setting_saver(key: str):
    """通用設定儲存工廠函式。"""
    async def _save(value):
        def _write():
            gc = get_sheet_client()
            ss = gc.open_by_key(SHEET_ID)
            ws = ensure_worksheet(ss, "Settings", ["Key", "Value", "UpdatedAt"])
            rows = ws.get_all_values()
            for i, r in enumerate(rows):
                if r and r[0] == key:
                    ws.update(f"A{i+1}:C{i+1}", [[key, str(value), now_str()]])
                    return
            ws.append_row([key, str(value), now_str()])
        await asyncio.to_thread(_write)
    return _save


def _make_setting_loader(key: str, default, cast=float):
    """通用設定讀取工廠函式。"""
    async def _load():
        def _read():
            try:
                gc = get_sheet_client()
                ss = gc.open_by_key(SHEET_ID)
                ws = ensure_worksheet(ss, "Settings", ["Key", "Value", "UpdatedAt"])
                for r in ws.get_all_values():
                    if r and r[0] == key and r[1]:
                        return cast(r[1])
            except Exception:
                pass
            return default
        return await asyncio.to_thread(_read)
    return _load


save_protein_goal = _make_setting_saver("protein_goal")
load_protein_goal = _make_setting_loader("protein_goal", default=60, cast=int)

save_steps_goal   = _make_setting_saver("steps_goal")
load_steps_goal   = _make_setting_loader("steps_goal", default=8000, cast=int)

save_burn_goal    = _make_setting_saver("burn_goal")
load_burn_goal    = _make_setting_loader("burn_goal", default=500, cast=int)

save_water_goal   = _make_setting_saver("water_goal")
load_water_goal   = _make_setting_loader("water_goal", default=2000, cast=int)

save_hp_goal      = _make_setting_saver("hp_goal")
load_hp_goal      = _make_setting_loader("hp_goal", default=22, cast=int)  # WHO: 150/week ÷ 7

save_carb_goal    = _make_setting_saver("carb_goal")
load_carb_goal    = _make_setting_loader("carb_goal", default=250, cast=int)  # g/day

save_fiber_goal   = _make_setting_saver("fiber_goal")
load_fiber_goal   = _make_setting_loader("fiber_goal", default=30, cast=int)  # g/day 男性建議38g

save_height       = _make_setting_saver("height_cm")
load_height       = _make_setting_loader("height_cm", default=0, cast=float)

save_age          = _make_setting_saver("age")
load_age          = _make_setting_loader("age", default=22, cast=int)

save_gender       = _make_setting_saver("gender")
load_gender       = _make_setting_loader("gender", default="male", cast=str)


async def ensure_default_profile():
    """首次啟動時寫入預設個人資料（21歲/男/174cm/86kg）"""
    age    = await load_age()
    height = await load_height()
    if age == 22 and height == 0:  # 未設定過身高代表全新用戶
        await save_age(21)
        await save_gender("male")
        await save_height(174)
        # 體重需從試算表讀取，預設不自動寫入
        logger.info("預設個人資料已設定：21歲男174cm")


async def get_coach_context() -> dict:
    """收集所有用戶數據給AI教練使用"""
    from datetime import date, timedelta
    td    = today_str()
    week  = [(date.today() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    diet_rows     = await fetch_diet_rows()
    exercise_rows = await fetch_exercise_rows()
    activity_rows = await fetch_activity_rows()
    weight_rows   = await fetch_weight_rows()

    goal_kcal    = await load_goal()
    protein_goal = await load_protein_goal()
    carb_goal    = await load_carb_goal()
    fiber_goal   = await load_fiber_goal()
    burn_goal    = await load_burn_goal()
    steps_goal   = await load_steps_goal()
    weight_goal  = await load_weight_goal()
    height_cm    = await load_height()
    age          = await load_age()
    gender       = await load_gender()
    bmr          = await calc_bmr()

    # 最新體重
    valid_wts = [float(r[1]) for r in weight_rows if len(r) > 1 and r[1]]
    latest_wt = valid_wts[-1] if valid_wts else None

    # 本週訓練部位統計
    muscle_stats = await fetch_weekly_category_stats()

    # 今日數據
    td_diet = [r for r in diet_rows if r and r[0][:10] == td]
    td_act  = [r for r in activity_rows if r and r[0][:10] == td]
    today_in    = sum(float(r[2]) for r in td_diet if len(r) > 2 and r[2])
    today_prot  = sum(float(r[3]) for r in td_diet if len(r) > 3 and r[3])
    today_burn  = sum(float(r[2]) for r in td_act  if len(r) > 2 and r[2])
    today_steps = sum(float(r[1]) for r in td_act  if len(r) > 1 and r[1])

    # 本週平均
    week_diet = [r for r in diet_rows if r and r[0][:10] in week]
    week_days = len(set(r[0][:10] for r in week_diet)) or 1
    avg_kcal  = sum(float(r[2]) for r in week_diet if len(r) > 2 and r[2]) / week_days
    avg_prot  = sum(float(r[3]) for r in week_diet if len(r) > 3 and r[3]) / week_days

    # 本週運動記錄
    week_ex = [r for r in exercise_rows if r and r[0][:10] in week]
    ex_summary = []
    for r in week_ex[-5:]:  # 最近5筆
        ex_summary.append(f"{r[0][:10]}: {r[1] if len(r) > 1 else ''} {r[2] if len(r) > 2 else ''}")

    bmi = round(latest_wt / ((height_cm / 100) ** 2), 1) if latest_wt and height_cm else None

    return {
        "profile": {
            "age": age, "gender": "男" if gender == "male" else "女",
            "height_cm": height_cm, "weight_kg": latest_wt,
            "bmi": bmi, "weight_goal": weight_goal, "bmr": bmr,
        },
        "goals": {
            "kcal": goal_kcal, "protein_g": protein_goal,
            "carbs_g": carb_goal, "fiber_g": fiber_goal,
            "burn_kcal": burn_goal, "steps": steps_goal,
        },
        "today": {
            "intake_kcal": round(today_in), "protein_g": round(today_prot),
            "burn_kcal": round(today_burn), "steps": round(today_steps),
            "net_kcal": round(today_in - today_burn - bmr),
        },
        "week_avg": {"kcal": round(avg_kcal), "protein_g": round(avg_prot)},
        "muscle_this_week": muscle_stats,
        "recent_exercises": ex_summary,
        "weight_trend": [float(r[1]) for r in weight_rows[-7:] if len(r) > 1 and r[1]],
        "sleep_7d": [
            {"date": r[1], "total_h": round(int(r[4] or 0)/60, 1),
             "deep_min": int(r[5] or 0), "rem_min": int(r[7] or 0), "score": int(r[9] or 0)}
            for r in (await fetch_sleep_rows(days=7))
            if r and len(r) >= 10
        ],
    }


async def ask_ai_coach(question: str, ctx: dict) -> str:
    """呼叫 Gemini 扮演 AI 健身教練回答問題"""
    import json as _json
    ctx_str = _json.dumps(ctx, ensure_ascii=False, indent=2)
    prompt = (
        "你是一位專業的健身和營養教練，說繁體中文，風格積極鼓勵但直接務實。\n\n"
        f"以下是用戶的健康數據：\n{ctx_str}\n\n"
        "用戶問題：" + question + "\n\n"
        "請基於用戶的真實數據給出個人化建議，回答要具體、實用，100-250字，"
        "重點放在最重要的1-3個行動建議。如果建議訓練，請從以下部位選擇："
        "胸、背、肩、手臂、核心、腿、全身、有氧。"
        "如果建議飲食，請給出具體食物名稱和份量。"
    )
    try:
        resp = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL_ID,
            contents=[prompt]
        )
        return resp.text.strip()
    except Exception as e:
        logger.error("AI教練失敗：%s", e)
        return "❌ AI教練暫時無法回應，請稍後再試。"


async def generate_weekly_coach_report(ctx: dict) -> str:
    """生成每週AI教練分析報告"""
    import json as _json
    ctx_str = _json.dumps(ctx, ensure_ascii=False, indent=2)
    prompt = (
        "你是專業健身教練，說繁體中文，語氣積極專業。\n\n"
        f"用戶本週健康數據：\n{ctx_str}\n\n"
        "請生成一份本週健身分析報告，包含：\n"
        "1. 📊 本週整體表現評分（1-10分）和簡評\n"
        "2. 💪 訓練分析：哪些部位訓練充足，哪些需要加強\n"
        "3. 🍽 飲食分析：熱量/蛋白質/碳水達標情況\n"
        "4. 🎯 下週3個具體目標（訓練+飲食各至少1個）\n"
        "5. 💡 1個最重要的改善建議\n\n"
        "每個部分簡潔有力，總字數控制在300字以內。"
    )
    try:
        resp = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL_ID,
            contents=[prompt]
        )
        return resp.text.strip()
    except Exception as e:
        logger.error("AI週報失敗：%s", e)
        return "❌ AI週報生成失敗，請稍後再試。"


async def calc_bmr() -> float:
    """
    Mifflin-St Jeor 公式計算 BMR（基礎代謝率）
    男：BMR = 10×體重(kg) + 6.25×身高(cm) - 5×年齡 + 5
    女：BMR = 10×體重(kg) + 6.25×身高(cm) - 5×年齡 - 161
    如資料不足則回傳 0
    """
    height_cm  = await load_height()
    age        = await load_age()
    gender     = await load_gender()
    all_weights = await fetch_weight_rows()
    valid_wts   = [float(r[1]) for r in all_weights if len(r) > 1 and r[1] and float(r[1]) > 0]
    if not (height_cm and age and valid_wts):
        return 0.0
    weight_kg = valid_wts[-1]
    if gender == "female":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    return round(bmr, 1)

# 訓練部位週目標：存為 JSON，key=部位名, value=每週目標次數
MUSCLE_CATEGORIES = ["胸", "背", "肩", "手臂", "核心", "腿", "全身", "有氧"]
MUSCLE_EMOJI = {
    "胸": "🫁", "背": "🔙", "肩": "🔺", "手臂": "💪",
    "核心": "⚡", "腿": "🦵", "全身": "🏋️", "有氧": "🏃"
}

async def save_muscle_plan(plan: dict):
    """儲存訓練計劃，plan = {'腿': 2, '胸': 2, ...}"""
    await _make_setting_saver("muscle_plan")(json.dumps(plan, ensure_ascii=False))

async def load_muscle_plan() -> dict:
    """讀取訓練計劃"""
    raw = await _make_setting_loader("muscle_plan", default="{}", cast=str)()
    try:
        return json.loads(raw)
    except Exception:
        return {}

# ── 喝水記錄 ─────────────────────────────────────────────────
async def upsert_water_row(ml: int) -> tuple[int, bool]:
    """今日飲水累加，回傳 (今日總量, was_updated)"""
    td = today_str()[:10]
    def _write():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Water", ["Date", "TotalML", "LastUpdated"])
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0] == td:
                prev = int(float(row[1])) if row[1] else 0
                new_total = prev + ml
                ws.update(f"A{i}:C{i}", [[td, str(new_total), now_str()]])
                return new_total, True
        ws.append_row([td, str(ml), now_str()])
        return ml, False
    result = await asyncio.to_thread(_write)
    _sheet_cache.invalidate("Water")          # ← 寫入後清除快取
    return result

async def fetch_water_today() -> int:
    """讀取今日飲水量（ml）"""
    td = today_str()[:10]
    def _read():
        try:
            gc = get_sheet_client()
            ss = gc.open_by_key(SHEET_ID)
            ws = ensure_worksheet(ss, "Water", ["Date", "TotalML", "LastUpdated"])
            rows = _cached_get_all("Water", ws)
            for row in rows:
                if row and row[0] == td:
                    return int(float(row[1])) if row[1] else 0
        except Exception:
            pass
        return 0
    return await asyncio.to_thread(_read)

async def fetch_water_week() -> dict:
    """讀取本週每日飲水量，回傳 {date: ml}"""
    from datetime import date, timedelta
    today = date.today()
    week_dates = set((today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7))
    def _read():
        try:
            gc = get_sheet_client()
            ss = gc.open_by_key(SHEET_ID)
            ws = ensure_worksheet(ss, "Water", ["Date", "TotalML", "LastUpdated"])
            rows = _cached_get_all("Water", ws)
            result = {}
            for row in rows:
                if row and row[0] in week_dates:
                    result[row[0]] = int(float(row[1])) if row[1] else 0
            return result
        except Exception:
            return {}
    return await asyncio.to_thread(_read)

# ── 本週訓練部位統計 ─────────────────────────────────────────
async def fetch_weekly_muscle_stats() -> dict:
    """
    統計本週各具體肌肉訓練次數。
    讀取 Exercise 表的 Muscles 欄（col 4），回傳 {'胸大肌': 3, '三角肌': 2, ...}
    包含別名對應，確保 AI 各種寫法都能正確歸類。
    """
    # 別名對應表：把 AI 可能返回的各種寫法統一到清單標準名
    ALIAS_MAP = {
        # 胸
        "胸肌": "胸大肌", "胸": "胸大肌", "大胸肌": "胸大肌", "pec": "胸大肌",
        # 斜方肌
        "斜方": "斜方肌", "trap": "斜方肌", "上斜方肌": "斜方肌",
        # 三角肌
        "肩膀": "三角肌", "肩部肌肉": "三角肌", "三角": "三角肌",
        "三角肌前束": "前三角肌", "前束": "前三角肌",
        "三角肌側束": "側三角肌", "側束": "側三角肌",
        "三角肌後束": "後三角肌", "後束": "後三角肌", "後三角": "後三角肌",
        # 背
        "背肌": "背闊肌", "闊背肌": "背闊肌", "lat": "背闊肌",
        "下背": "豎脊肌", "下背肌": "豎脊肌", "脊柱豎肌": "豎脊肌",
        # 手臂
        "二頭肌": "肱二頭肌", "二頭": "肱二頭肌", "bicep": "肱二頭肌",
        "三頭肌": "肱三頭肌", "三頭": "肱三頭肌", "tricep": "肱三頭肌",
        "前臂": "前臂肌",
        # 核心
        "腹肌": "腹直肌", "腹部": "腹直肌", "abs": "腹直肌",
        "側腹": "腹斜肌", "腹外斜肌": "腹斜肌",
        # 腿
        "四頭肌": "股四頭肌", "大腿前側": "股四頭肌", "quad": "股四頭肌",
        "腿後側": "腿後肌", "大腿後側": "腿後肌", "hamstring": "腿後肌",
        "臀部": "臀大肌", "臀肌": "臀大肌", "glute": "臀大肌",
        "小腿": "小腿肌", "腓腸肌": "小腿肌", "calf": "小腿肌",
    }

    from datetime import date, timedelta
    today      = date.today()
    week_dates = set((today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7))
    exercise_rows = await fetch_exercise_rows()
    muscle_counts: dict = {}

    for r in exercise_rows:
        if not r or r[0][:10] not in week_dates:
            continue
        if len(r) > 4 and r[4]:
            for m in r[4].split(","):
                m = m.strip()
                if not m:
                    continue
                # 別名正規化
                m = ALIAS_MAP.get(m, m)
                muscle_counts[m] = muscle_counts.get(m, 0) + 1
        elif len(r) > 3 and r[3]:
            cat = r[3].strip()
            if cat:
                muscle_counts[cat] = muscle_counts.get(cat, 0) + 1
    return muscle_counts


async def fetch_weekly_category_stats() -> dict:
    """統計本週各大類部位訓練次數，供訓練計劃達標判斷使用。"""
    from datetime import date, timedelta
    today      = date.today()
    week_dates = set((today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7))
    exercise_rows = await fetch_exercise_rows()
    counts = {}
    for r in exercise_rows:
        if r and len(r) > 3 and r[0][:10] in week_dates:
            cat = r[3].strip() if r[3] else ""
            if cat:
                counts[cat] = counts.get(cat, 0) + 1
    return counts


async def load_frequent_foods(top_n: int = 5) -> list[str]:
    """
    從 Diet 表統計出現最多次的食物名稱（≥3次），回傳前 top_n 個。
    用於快捷食物記錄。
    """
    def _read():
        try:
            gc = get_sheet_client()
            ss = gc.open_by_key(SHEET_ID)
            ws = ensure_worksheet(ss, "Diet",
                ["Timestamp", "FoodName", "Calories(kcal)", "Protein(g)", "Carbs(g)", "Fiber(g)", "Notes"])
            rows = ws.get_all_values()[1:]
            from collections import Counter
            counter = Counter(r[1].strip() for r in rows if len(r) > 1 and r[1].strip())
            return [name for name, cnt in counter.most_common(top_n) if cnt >= 3]
        except Exception:
            return []
    return await asyncio.to_thread(_read)


async def fetch_diet_rows(filter_date: str | None = None) -> list[list]:
    def _read():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Diet",
            ["Timestamp", "FoodName", "Calories(kcal)", "Protein(g)", "Carbs(g)", "Fiber(g)", "Notes"])
        return _cached_get_all("Diet", ws)
    rows = await asyncio.to_thread(_read)
    if filter_date:
        rows = [r for r in rows if r and r[0].startswith(filter_date)]
    return rows


async def fetch_exercise_rows(filter_date: str | None = None) -> list[list]:
    def _read():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Exercise",
            ["Timestamp", "ExerciseName", "Duration(min)", "MuscleCategory", "Muscles", "Movements"])
        return _cached_get_all("Exercise", ws)
    rows = await asyncio.to_thread(_read)
    if filter_date:
        rows = [r for r in rows if r and r[0].startswith(filter_date)]
    return rows


async def fetch_weight_rows(filter_date: str | None = None) -> list[list]:
    def _read():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Weight", ["Timestamp", "Weight(kg)", "Note"])
        return _cached_get_all("Weight", ws)
    rows = await asyncio.to_thread(_read)
    if filter_date:
        rows = [r for r in rows if r and r[0].startswith(filter_date)]
    return rows


async def upsert_activity_row(data: dict) -> bool:
    """
    更新今日活動數據（若今日已有記錄則覆蓋，否則新增）。
    回傳 True = 更新已有記錄，False = 新增
    """
    td = today_str()[:10]  # YYYY-MM-DD

    def _write():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Activity",
            ["Timestamp", "Steps", "CaloriesBurned(kcal)", "HeartPoints", "ActiveMinutes", "Notes"])
        all_rows = ws.get_all_values()  # 含標題行
        # 找今日已有的列（從第2列開始，index 1）
        for i, row in enumerate(all_rows[1:], start=2):
            if row and row[0].startswith(td):
                # 更新此列，保留原有值若新輸入為空
                old_steps = row[1] if len(row) > 1 else ""
                old_cal   = row[2] if len(row) > 2 else ""
                old_hp    = row[3] if len(row) > 3 else ""
                old_am    = row[4] if len(row) > 4 else ""
                ws.update(f"A{i}:F{i}", [[
                    now_str(),
                    str(data["steps"])          if data["steps"]           is not None else old_steps,
                    str(data["calories_burned"]) if data["calories_burned"] is not None else old_cal,
                    str(data["heart_points"])    if data["heart_points"]    is not None else old_hp,
                    str(data["active_minutes"])  if data["active_minutes"]  is not None else old_am,
                    data.get("notes", ""),
                ]])
                return True  # updated
        # 今日無記錄，新增
        ws.append_row([
            now_str(),
            data.get("steps", ""),
            data.get("calories_burned", ""),
            data.get("heart_points", ""),
            data.get("active_minutes", ""),
            data.get("notes", ""),
        ])
        return False  # inserted

    result = await asyncio.to_thread(_write)
    _sheet_cache.invalidate("Activity")       # ← 寫入後清除快取
    return result


async def fetch_activity_rows(filter_date: str | None = None) -> list[list]:
    def _read():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Activity",
            ["Timestamp", "Steps", "CaloriesBurned(kcal)", "HeartPoints", "ActiveMinutes", "Notes"])
        return _cached_get_all("Activity", ws)
    rows = await asyncio.to_thread(_read)
    if filter_date:
        rows = [r for r in rows if r and r[0].startswith(filter_date)]
    return rows

# ════════════════════════════════════════════════════════════════
#  🌙 睡眠追蹤模塊
# ════════════════════════════════════════════════════════════════

SLEEP_HEADERS = [
    "Timestamp", "Date", "BedTime", "WakeTime",
    "TotalMin", "DeepMin", "LightMin", "REMMin", "AwakeMin",
    "Score", "Notes"
]
_SLEEP_DEBT_GOAL = 420   # 7小時 = 420 分鐘目標


async def append_sleep_row(data: dict) -> None:
    def _write():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Sleep", SLEEP_HEADERS)
        ws.append_row([
            now_str(),
            data.get("date", today_str()),
            data.get("bed_time", ""),
            data.get("wake_time", ""),
            int(data.get("total_min", 0)),
            int(data.get("deep_min", 0)),
            int(data.get("light_min", 0)),
            int(data.get("rem_min", 0)),
            int(data.get("awake_min", 0)),
            int(data.get("score", 0)),
            data.get("notes", ""),
        ])
    await asyncio.to_thread(_write)


async def fetch_sleep_rows(filter_date: str | None = None,
                           days: int | None = None) -> list[list]:
    def _read():
        gc = get_sheet_client()
        ss = gc.open_by_key(SHEET_ID)
        ws = ensure_worksheet(ss, "Sleep", SLEEP_HEADERS)
        return ws.get_all_values()[1:]
    rows = await asyncio.to_thread(_read)
    if filter_date:
        rows = [r for r in rows if r and len(r) > 1 and r[1] == filter_date]
    if days:
        from datetime import date as _d, timedelta as _td
        cutoff = (_d.today() - _td(days=days)).strftime("%Y-%m-%d")
        rows = [r for r in rows if r and len(r) > 1 and r[1] >= cutoff]
    return rows


async def analyze_sleep_image(image_bytes: bytes) -> dict:
    """Gemini OCR 識別 Zepp/Mi Fitness 睡眠截圖。"""
    prompt = (
        "你是睡眠數據提取專家。仔細分析這張手環或睡眠 App 截圖，"
        "提取以下欄位並以純 JSON 回覆（不含任何多餘文字或 markdown）：\n"
        "{\n"
        '  "bed_time": "HH:MM",   // 入睡時間（24小時制）\n'
        '  "wake_time": "HH:MM",  // 起床時間（24小時制）\n'
        '  "total_min": 整數,      // 總睡眠分鐘數\n'
        '  "deep_min":  整數,      // 深睡分鐘數（Deep Sleep）\n'
        '  "light_min": 整數,      // 淺睡分鐘數（Light Sleep）\n'
        '  "rem_min":   整數,      // REM 分鐘數（無則填 0）\n'
        '  "awake_min": 整數,      // 清醒分鐘數（無則填 0）\n'
        '  "score":     整數,      // 睡眠評分 0-100（無則填 0）\n'
        '  "source":    "App名稱"  // 如 Mi Fitness、Zepp、Xiaomi Band\n'
        "}\n\n"
        "規則：時間統一 HH:MM 24小時制；若只有總時長如 7h20m，"
        "total_min=440，其他填 0；若無某欄位填 0。"
    )
    def _call():
        parts = [prompt, genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")]
        return gemini_client.models.generate_content(model=GEMINI_MODEL_ID, contents=parts).text
    return extract_json(await asyncio.to_thread(_call))


def _fmt_dur(minutes: int) -> str:
    h, m = divmod(max(0, int(minutes)), 60)
    return f"{h}h {m}m" if h and m else (f"{h}h" if h else f"{m}m")


def _sleep_quality(total_min: int, deep_min: int, score: int) -> str:
    if score >= 85 or (total_min >= 420 and deep_min >= 90):  return "😴 優秀"
    if score >= 70 or (total_min >= 360 and deep_min >= 60):  return "🙂 良好"
    if score >= 55 or total_min >= 300:                        return "😐 一般"
    return "😞 不足"


def format_sleep_summary(row: list) -> str:
    """格式化單日睡眠摘要（row = Sheet 行，含 Timestamp 在 [0]）。"""
    # 支援帶或不帶 Timestamp 欄位
    offset = 1 if len(row) >= 11 else 0
    try:
        date_str  = row[offset + 0]
        bed       = row[offset + 1] or "—"
        wake      = row[offset + 2] or "—"
        total_min = int(row[offset + 3] or 0)
        deep_min  = int(row[offset + 4] or 0)
        light_min = int(row[offset + 5] or 0)
        rem_min   = int(row[offset + 6] or 0)
        awake_min = int(row[offset + 7] or 0)
        score     = int(row[offset + 8] or 0)
    except Exception:
        return "_資料格式錯誤_"

    quality   = _sleep_quality(total_min, deep_min, score)
    deep_pct  = round(deep_min  / total_min * 100) if total_min else 0
    rem_pct   = round(rem_min   / total_min * 100) if total_min else 0
    score_ln  = f"🏅 睡眠評分：*{escape_md(str(score))}* 分\n" if score else ""
    awake_ln  = f"⚡ 清醒：{escape_md(_fmt_dur(awake_min))}\n" if awake_min else ""
    rem_ln    = (f"💜 REM：{escape_md(_fmt_dur(rem_min))} "
                 f"\\({escape_md(str(rem_pct))}%\\)\n") if rem_min else ""
    debt_min  = _SLEEP_DEBT_GOAL - total_min
    debt_ln   = (f"⚠️ 睡眠債：\\-{escape_md(_fmt_dur(abs(debt_min)))}"
                 if debt_min > 0 else
                 f"✅ 達標 \\+{escape_md(_fmt_dur(-debt_min))}")

    return (
        f"🌙 *{escape_md(date_str)} 睡眠記錄*\n\n"
        f"{score_ln}"
        f"⏰ 入睡：{escape_md(bed)}　起床：{escape_md(wake)}\n"
        f"😴 總計：*{escape_md(_fmt_dur(total_min))}*　{escape_md(quality)}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🔵 深睡：{escape_md(_fmt_dur(deep_min))} \\({escape_md(str(deep_pct))}%\\)\n"
        f"🟦 淺睡：{escape_md(_fmt_dur(light_min))}\n"
        f"{rem_ln}"
        f"{awake_ln}"
        f"━━━━━━━━━━━━━━━━\n"
        f"{debt_ln}"
    )


def format_sleep_week_report(rows: list[list]) -> str:
    if not rows:
        return "📊 本週尚無睡眠記錄，請先上傳 Mi Band 截圖。"

    totals, deeps, rems, scores = [], [], [], []
    wake_hours = []

    for r in rows:
        if not r or len(r) < 10: continue
        try:
            totals.append(int(r[4] or 0))
            deeps.append(int(r[5] or 0))
            rems.append(int(r[7] or 0))
            if r[9]: scores.append(int(r[9]))
            if r[3] and ":" in r[3]:
                h, m = map(int, r[3].split(":"))
                wake_hours.append(h + m/60)
        except Exception:
            continue

    if not totals: return "📊 本週睡眠數據解析失敗。"

    n = len(totals)
    avg_tot  = sum(totals) / n
    avg_deep = sum(deeps) / n
    avg_rem  = sum(rems) / n
    avg_sc   = round(sum(scores)/len(scores)) if scores else 0
    deep_pct = round(avg_deep / avg_tot * 100) if avg_tot else 0
    rem_pct  = round(avg_rem  / avg_tot * 100) if avg_tot else 0
    total_debt = sum(_SLEEP_DEBT_GOAL - t for t in totals)
    debt_str = (f"⚠️ 本週累計睡眠債 {escape_md(_fmt_dur(abs(total_debt)))}"
                if total_debt > 0 else "✅ 本週睡眠全部達標")

    import statistics as _stats
    reg_str = ""
    if len(wake_hours) >= 3:
        std = _stats.stdev(wake_hours)
        reg_str = ("🟢 起床時間非常規律" if std < 0.5 else
                   "🟡 起床時間尚算規律" if std < 1.0 else
                   "🔴 起床時間不規律（差異超過 1 小時）")

    sc_ln = f"🏅 平均評分：*{escape_md(str(avg_sc))}* 分\n" if avg_sc else ""
    rem_ln = (f"💜 平均 REM：{escape_md(_fmt_dur(avg_rem))} "
              f"\\({escape_md(str(rem_pct))}%\\)\n") if any(rems) else ""

    lines = [
        f"📋 *本週睡眠報告* \\({escape_md(str(n))} 天\\)\n\n",
        sc_ln,
        f"😴 平均時長：*{escape_md(_fmt_dur(avg_tot))}*\n",
        f"🔵 平均深睡：{escape_md(_fmt_dur(avg_deep))} \\({escape_md(str(deep_pct))}%\\)\n",
        rem_ln,
        f"\n{escape_md(debt_str)}\n",
    ]
    if reg_str:
        lines.append(f"{escape_md(reg_str)}\n")

    lines.append("\n━━━━━━━━━━━━━━━━\n📅 *每日明細*\n")
    for r in rows[-7:]:
        if not r or len(r) < 5: continue
        try:
            d  = r[1][5:] if len(r[1]) >= 7 else r[1]
            t  = int(r[4] or 0)
            dp = int(r[5] or 0)
            sc = int(r[9] or 0) if len(r) > 9 else 0
            deep_p = round(dp/t*100) if t else 0
            bar = "█" * min(int(t/30), 14)
            sc_s = f" {sc}分" if sc else ""
            lines.append(
                f"  {escape_md(d)} `{escape_md(bar.ljust(14))}` "
                f"{escape_md(_fmt_dur(t))}{escape_md(sc_s)} "
                f"深{escape_md(str(deep_p))}%\n"
            )
        except Exception:
            continue
    return "".join(lines)


async def get_sleep_ai_analysis(rows_7d: list[list], coach_ctx: dict) -> str:
    import json as _json
    parsed = []
    for r in rows_7d:
        if not r or len(r) < 10: continue
        try:
            parsed.append({
                "date":     r[1],
                "bed":      r[2],
                "wake":     r[3],
                "total_h":  round(int(r[4] or 0)/60, 1),
                "deep_min": int(r[5] or 0),
                "light_min":int(r[6] or 0),
                "rem_min":  int(r[7] or 0),
                "score":    int(r[9] or 0),
            })
        except Exception:
            continue

    if not parsed:
        return "📊 睡眠數據不足，請先上傳幾天的截圖再分析。"

    avg_tot  = sum(p["total_h"] for p in parsed) / len(parsed)
    avg_deep = sum(p["deep_min"] for p in parsed) / len(parsed)

    sleep_info = {
        "days": len(parsed),
        "avg_total_h": round(avg_tot, 1),
        "avg_deep_min": round(avg_deep),
        "records": parsed,
    }
    fitness_info = {
        "weight_kg":     coach_ctx.get("profile", {}).get("weight_kg"),
        "exercises":     coach_ctx.get("recent_exercises", [])[-5:],
        "week_avg_kcal": coach_ctx.get("week_avg", {}).get("kcal"),
        "muscles":       coach_ctx.get("muscle_this_week", {}),
    }

    prompt = (
        f"你是專業睡眠與運動恢復顧問。用戶有 {len(parsed)} 天睡眠記錄：\n"
        f"{_json.dumps(sleep_info, ensure_ascii=False)}\n\n"
        f"用戶健身數據（交叉分析用）：\n"
        f"{_json.dumps(fitness_info, ensure_ascii=False)}\n\n"
        "請用繁體中文做詳細分析，格式如下（各段落間空一行）：\n\n"
        "**🔍 整體評估**\n"
        "給出整體評分（優/良/一般/不足）及 1-2 句說明原因\n\n"
        "**📊 深度分析**\n"
        "• 深睡比例（正常應 20-25%）\n"
        "• REM 分析（正常應 20-25%）\n"
        "• 睡眠規律性（入睡/起床時間一致性）\n\n"
        "**⚠️ 主要問題（2-3 個）**\n"
        "每個問題說明對健康或運動表現的具體影響\n\n"
        "**💡 改善行動（3-4 條）**\n"
        "要具體可執行，帶時間點或數字\n\n"
        "**🏋️ 睡眠與訓練關聯**\n"
        "根據用戶的訓練強度，分析睡眠對肌肉恢復的影響\n\n"
        "總字數控制在 350-450 字之間，不要加 markdown 格式符號。"
    )
    def _call():
        return gemini_client.models.generate_content(model=GEMINI_MODEL_ID, contents=[prompt]).text
    try:
        return await asyncio.to_thread(_call)
    except Exception as e:
        logger.error("Sleep AI analysis error: %s", e)
        return "❌ AI 分析暫時無法使用。"


async def process_sleep_image(update, context, image_bytes: bytes):
    thinking = await update.message.reply_text(
        "🌙 AI 識別睡眠數據中... ⏳")
    try:
        result = await analyze_sleep_image(image_bytes)
    except Exception as e:
        logger.error("睡眠截圖識別失敗：%s", e)
        await thinking.edit_text("❌ 無法識別截圖，請確認是睡眠數據頁面。")
        return

    total_min = int(result.get("total_min", 0))
    if total_min == 0:
        await thinking.edit_text(
            "❌ 未能識別睡眠時長，請確認截圖含有睡眠記錄頁面。")
        return

    data = {
        "date":      today_str(),
        "bed_time":  result.get("bed_time", ""),
        "wake_time": result.get("wake_time", ""),
        "total_min": total_min,
        "deep_min":  int(result.get("deep_min", 0)),
        "light_min": int(result.get("light_min", 0)),
        "rem_min":   int(result.get("rem_min", 0)),
        "awake_min": int(result.get("awake_min", 0)),
        "score":     int(result.get("score", 0)),
        "notes":     result.get("source", "Mi Band"),
    }
    try:
        await append_sleep_row(data)
    except Exception as e:
        logger.error("睡眠寫入失敗：%s", e)
        await thinking.edit_text("❌ 資料庫寫入失敗。")
        return

    context.user_data["state"] = STATE_IDLE
    src = result.get("source", "Mi Band")
    display_row = [
        "", data["date"], data["bed_time"], data["wake_time"],
        data["total_min"], data["deep_min"], data["light_min"],
        data["rem_min"], data["awake_min"], data["score"], src
    ]
    summary = format_sleep_summary(display_row)
    # edit_text 不支援 Reply Keyboard，先 edit 再 send
    await thinking.edit_text(f"✅ 睡眠已記錄！（{src}）")
    await update.message.reply_text(
        summary,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=SUB_KEYBOARD_SLEEP)
async def morning_sleep_report_job(context):
    """每天早上 8:00 自動推送前一晚睡眠摘要。"""
    if not USER_CHAT_ID:
        return
    from datetime import date, timedelta
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = await fetch_sleep_rows(filter_date=yesterday)
    if not rows:
        return   # 無數據則不推送
    row = rows[-1]
    display_row = [""] + list(row)
    summary = format_sleep_summary(display_row)
    # Quick AI tip (short version)
    rows_7 = await fetch_sleep_rows(days=7)
    parsed = []
    for r in rows_7:
        if not r or len(r) < 10: continue
        try:
            parsed.append({
                "date": r[1],
                "total_h": round(int(r[4] or 0)/60, 1),
                "deep_min": int(r[5] or 0),
            })
        except Exception:
            continue
    tip = ""
    if parsed:
        avg_deep = sum(p["deep_min"] for p in parsed) / len(parsed)
        avg_tot_h = sum(p["total_h"] for p in parsed) / len(parsed)
        import json as _j
        prompt = (
            f"用戶最近 {len(parsed)} 天的睡眠數據：{_j.dumps(parsed, ensure_ascii=False)}\n"
            "請給出 1-2 句（最多 50 字）的今日恢復建議，重點放在今天的訓練/飲食調整，"
            "語氣簡潔直接。用繁體中文，不加標題或格式符號。"
        )
        def _call():
            return gemini_client.models.generate_content(
                model=GEMINI_MODEL_ID, contents=[prompt]).text
        try:
            tip_text = await asyncio.to_thread(_call)
            tip = f"\n\n💡 *今日建議*\n{escape_md(tip_text.strip())}"
        except Exception:
            pass
    try:
        await context.bot.send_message(
            chat_id=USER_CHAT_ID,
            text=f"🌅 *早安！昨晚睡眠摘要*\n\n{summary}{tip}",
            parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error("Morning sleep report failed: %s", e)



def get_streak_and_stats(diet_rows, exercise_rows, activity_rows, goal_kcal) -> dict:
    """
    計算簽到天數（有記錄任何東西就算）及各項統計。
    """
    from collections import defaultdict
    daily_intake  = defaultdict(float)
    daily_burn    = defaultdict(float)
    checkin_dates = set()   # 有任何記錄的日期

    for r in diet_rows:
        if r and len(r) > 2 and r[2]:
            daily_intake[r[0][:10]] += float(r[2])
            checkin_dates.add(r[0][:10])
    for r in exercise_rows:
        if r and r[0]:
            checkin_dates.add(r[0][:10])
    for r in activity_rows:
        if r and len(r) > 2 and r[2]:
            try:
                daily_burn[r[0][:10]] += float(r[2])
                checkin_dates.add(r[0][:10])
            except (ValueError, TypeError):
                pass

    all_dates    = sorted(checkin_dates, reverse=True)
    total_days   = len(checkin_dates)

    # 連續簽到（從今天往回數）
    from datetime import date, timedelta
    streak = 0
    check  = date.today()
    while check.strftime("%Y-%m-%d") in checkin_dates:
        streak += 1
        check  -= timedelta(days=1)

    # 歷史最長連續
    best_streak = cur = 0
    prev        = None
    for d in sorted(checkin_dates):
        dt = date.fromisoformat(d)
        if prev and (dt - prev).days == 1:
            cur += 1
        else:
            cur = 1
        best_streak = max(best_streak, cur)
        prev = dt

    # 熱量達標天數（攝取 <= 目標，僅供參考）
    goal_days = sum(
        1 for d in checkin_dates
        if daily_intake[d] > 0 and daily_intake[d] <= goal_kcal
    )
    rate = round(goal_days / total_days * 100) if total_days else 0

    return {
        "streak":      streak,
        "total_days":  total_days,
        "goal_days":   goal_days,
        "best_streak": best_streak,
        "rate":        rate,
        "daily_intake": dict(daily_intake),
        "daily_burn":   dict(daily_burn),
    }


# 35句激勵語（7情境 × 5句），完全本地，零 AI 消耗
import random as _random

_MOTIVATIONS = {
    "start": [          # 第1-2天
        "🌱 萬事起頭難，您已邁出最重要的第一步！",
        "🌟 每個健身達人都是從今天這樣開始的！",
        "💪 開始記錄就是成功的一半，繼續保持！",
        "🎯 好的習慣從第一天培養，您做到了！",
        "🚀 今天的堅持，是明天更好自己的基礎！",
    ],
    "building": [       # 3-6天
        "🔥 連續記錄中！習慣正在悄悄養成！",
        "📈 每天記錄一點點，健康進步看得見！",
        "💡 堅持是最難的事，您正在做最難的事！",
        "🏃 身體正在記住您的努力，繼續加油！",
        "⚡ 幾天的堅持已超過大多數人，繼續！",
    ],
    "week": [           # 7-13天
        "🎉 一週達成！您已建立初步的健康習慣！",
        "🏅 連續7天！意志力比肌肉更需要訓練！",
        "🌈 一週的堅持證明了您的決心，繼續燃燒！",
        "💎 七天不間斷，比99%的人做得更好！",
        "🔑 一週習慣是改變人生的第一把鑰匙！",
    ],
    "twoweeks": [       # 14-29天
        "🔥🔥 兩週連續！您的身體正在改變！",
        "🦁 兩週的鐵打習慣，沒有什麼能阻止您！",
        "💪 14天以上的堅持者，最終都看到了改變！",
        "🌟 超過兩週！您的毅力令人敬佩，繼續！",
        "🚀 兩週連續，您已經是真正的健康達人！",
    ],
    "month": [          # 30天以上
        "🏆 一個月以上！您是真正的健身冠軍！",
        "👑 30天連續記錄，習慣已刻入您的DNA！",
        "🌟 一個月的堅持，改變的不只是身體！",
        "🔥🔥🔥 月度達成！99%的人無法做到！",
        "💎 您的堅持將成為他人的激勵，了不起！",
    ],
    "today_good": [     # 今日進度良好（>60%）
        "📊 今日進度很棒！保持節奏，完美收官！",
        "🎯 距離今日目標不遠了，衝刺一下！",
        "✨ 今天表現優秀，繼續保持這個狀態！",
        "💪 今日數據亮眼，身體在說謝謝您！",
        "🔥 今天做得很好！明天的您會感謝現在的堅持！",
    ],
    "today_start": [    # 今日剛開始或未記錄
        "☀️ 新的一天，新的機會，開始記錄吧！",
        "💫 今天還沒開始？現在就是最好的時機！",
        "🎯 每天的第一筆記錄，是給自己最好的禮物！",
        "🌅 今日尚未開始，但您看到了這裡，說明您在意！",
        "⚡ 別讓今天成為空白，隨手記錄一筆！",
    ],
}

def get_motivation(streak: int, today_progress_pct: float) -> str:
    """根據連續天數和今日進度選擇激勵語"""
    if today_progress_pct < 10:
        pool = _MOTIVATIONS["today_start"]
    elif today_progress_pct >= 60:
        pool = _MOTIVATIONS["today_good"]
    elif streak >= 30:
        pool = _MOTIVATIONS["month"]
    elif streak >= 14:
        pool = _MOTIVATIONS["twoweeks"]
    elif streak >= 7:
        pool = _MOTIVATIONS["week"]
    elif streak >= 3:
        pool = _MOTIVATIONS["building"]
    else:
        pool = _MOTIVATIONS["start"]
    return _random.choice(pool)


# ════════════════════════════════════════════════════════════
#  報表格式化（MarkdownV2）
# ════════════════════════════════════════════════════════════

def progress_bar(current: float, total: float, length: int = 8) -> str:
    """生成文字進度條，例如 ████░░░░"""
    if total <= 0:
        return "░" * length
    ratio = min(current / total, 1.0)
    filled = int(ratio * length)
    bar = "█" * filled + "░" * (length - filled)
    pct = int(ratio * 100)
    return f"{bar} {pct}%"


def short_time(ts: str) -> str:
    """從時間戳取出 HH:MM，例如 '2026-02-24 08:42:17' → '08:42'"""
    try:
        return ts[11:16]
    except Exception:
        return ts


def truncate(name: str, max_len: int = 14) -> str:
    """截斷過長名稱"""
    return name if len(name) <= max_len else name[:max_len] + "…"


def format_diet_section(rows: list[list], goal_kcal: int = 0) -> tuple[str, float, float, float, float]:
    """
    返回 (飲食區段文字, total_cal, total_prot, total_carb, total_fib)
    不含標題，供合併消息使用。
    """
    total_cal = total_prot = total_carb = total_fib = 0.0
    lines = []

    for i, r in enumerate(rows, 1):
        ts   = short_time(r[0]) if len(r) > 0 else "?"
        name = truncate(r[1]) if len(r) > 1 else "?"
        cal  = float(r[2]) if len(r) > 2 and r[2] else 0
        prot = float(r[3]) if len(r) > 3 and r[3] else 0
        carb = float(r[4]) if len(r) > 4 and r[4] else 0
        fib  = float(r[5]) if len(r) > 5 and r[5] else 0
        total_cal += cal; total_prot += prot; total_carb += carb; total_fib += fib
        lines.append(
            f"  `{i:02d}` {escape_md(name)}  "
            f"🔥{escape_md(str(int(cal)))} \\| 🥩{escape_md(f'{prot:.0f}')}g  "
            f"_{escape_md(ts)}_"
        )

    return "\n".join(lines), total_cal, total_prot, total_carb, total_fib


def format_exercise_section(rows: list[list]) -> tuple[str, int]:
    """返回 (運動區段文字, total_min)"""
    CATEGORY_EMOJI = {
        "胸": "🫁", "背": "🔙", "肩": "🔺", "手臂": "💪",
        "核心": "⚡", "腿": "🦵", "全身": "🏋️", "有氧": "🏃", "柔韌": "🧘"
    }
    total_dur = 0
    lines = []
    for i, r in enumerate(rows, 1):
        name     = truncate(r[1]) if len(r) > 1 else "?"
        dur      = r[2] if len(r) > 2 else "?"
        category = r[3] if len(r) > 3 else ""
        try:
            total_dur += int(float(dur))
        except Exception:
            pass
        emoji = CATEGORY_EMOJI.get(category, "🏋️")
        lines.append(f"  `{i:02d}` {emoji} {escape_md(name)}  ⏱{escape_md(str(dur))}分鐘")
    return "\n".join(lines), total_dur


def format_activity_section(rows: list[list]) -> tuple[str, float, float, float, float]:
    """返回 (活動區段文字, total_steps, total_cal, total_hp, total_am)"""
    total_steps = total_cal = total_hp = total_am = 0.0
    lines = []
    for i, r in enumerate(rows, 1):
        ts  = short_time(r[0]) if len(r) > 0 else "?"
        stp = r[1] if len(r) > 1 else ""
        cal = r[2] if len(r) > 2 else ""
        hp  = r[3] if len(r) > 3 else ""
        am  = r[4] if len(r) > 4 else ""
        if stp: total_steps += float(stp)
        if cal: total_cal   += float(cal)
        if hp:  total_hp    += float(hp)
        if am:  total_am    += float(am)
        parts = []
        if stp: parts.append(f"👟{escape_md(stp)}")
        if cal: parts.append(f"🔥{escape_md(cal)}kcal")
        if hp:  parts.append(f"❤️{escape_md(hp)}HP")
        sep = " \\| "
        lines.append(f"  `{i:02d}` _{escape_md(ts)}_  {sep.join(parts)}")
    return "\n".join(lines), total_steps, total_cal, total_hp, total_am


def build_today_msg1(diet_rows, goal_kcal, protein_goal, total_in, total_burn, td,
                     water_ml=0, water_goal=2000, bmr=0.0,
                     carb_goal=250, fiber_goal=30) -> str:
    """第1條消息：飲食記錄 + 熱量總覽 + 飲水"""
    diet_text, total_cal, total_prot, total_carb, total_fib = format_diet_section(diet_rows, goal_kcal)

    bar_intake = progress_bar(total_in, goal_kcal)
    bar_prot   = progress_bar(total_prot, protein_goal)
    bar_carb   = progress_bar(total_carb, carb_goal)
    bar_fiber  = progress_bar(total_fib, fiber_goal)
    bar_water  = progress_bar(water_ml, water_goal)

    # 真實總消耗 = 活動消耗 + BMR
    real_burn = total_burn + bmr if bmr > 0 else total_burn
    available = goal_kcal - total_in + total_burn  # 只用活動消耗計算可額外攝取量
    if available < 0:
        gap_line = f"🔴 *熱量盈餘 \\+{escape_md(str(int(-available)))} kcal* _超出今日配額_"
    else:
        gap_line = f"🟢 *還可攝取 {escape_md(str(int(available)))} kcal*"

    net = total_in - real_burn
    diet_section = (diet_text + "\n") if diet_rows else "  _尚無飲食記錄_\n"

    # 消耗顯示：有BMR就分開顯示
    if bmr > 0:
        burn_section = (
            f"🔥 活動消耗　{escape_md(str(int(total_burn)))} kcal\n"
            f"🧬 基礎代謝　{escape_md(str(int(bmr)))} kcal\n"
            f"📊 真實總消耗　*{escape_md(str(int(real_burn)))} kcal*\n"
        )
    else:
        burn_section = f"🔥 消耗　{escape_md(str(int(total_burn)))} kcal\n"

    return (
        f"📅 *今日資料 \\({escape_md(td)}\\)*\n\n"
        f"━━ 🍽 *飲食記錄* ━━\n"
        f"{diet_section}\n"
        f"━━ ⚡ *熱量總覽* ━━\n"
        f"🍽 攝取　`{escape_md(bar_intake)}`\n"
        f"   {escape_md(str(int(total_in)))} ／ {escape_md(str(goal_kcal))} kcal\n"
        f"🥩 蛋白質　`{escape_md(bar_prot)}`\n"
        f"   {escape_md(f'{total_prot:.0f}')} ／ {escape_md(str(protein_goal))} g\n"
        f"🌾 碳水　`{escape_md(bar_carb)}`\n"
        f"   {escape_md(f'{total_carb:.0f}')} ／ {escape_md(str(carb_goal))} g\n"
        f"🌿 纖維　`{escape_md(bar_fiber)}`\n"
        f"   {escape_md(f'{total_fib:.0f}')} ／ {escape_md(str(fiber_goal))} g\n"
        f"{burn_section}"
        f"⚡ 淨熱量　{escape_md(str(int(net)))} kcal\n"
        f"💧 飲水　`{escape_md(bar_water)}`\n"
        f"   {escape_md(str(water_ml))} ／ {escape_md(str(water_goal))} ml\n\n"
        f"{gap_line}"
    )


def build_today_msg2(exercise_rows, activity_rows, weight_rows,
                     total_burn, burn_goal, total_steps, steps_goal, td) -> str:
    """第2條消息：運動 + 活動 + 體重"""
    sections = []

    if exercise_rows:
        ex_text, total_dur = format_exercise_section(exercise_rows)
        sections.append(
            f"━━ 🏃 *運動記錄* ━━\n"
            f"{ex_text}\n"
            f"  ⏱ 總計 {escape_md(str(total_dur))} 分鐘"
        )

    if activity_rows:
        ac_text, ac_steps, ac_cal, ac_hp, ac_am = format_activity_section(activity_rows)
        burn_bar  = progress_bar(total_burn, burn_goal)
        steps_bar = progress_bar(total_steps, steps_goal)
        ac_summary = []
        if ac_steps: ac_summary.append(f"👟 {escape_md(str(int(ac_steps)))} 步")
        if ac_cal:   ac_summary.append(f"🔥 {escape_md(str(int(ac_cal)))} kcal")
        if ac_hp:    ac_summary.append(f"❤️ {escape_md(str(int(ac_hp)))} HP")
        sep = "  "
        sections.append(
            f"━━ 👟 *活動數據* ━━\n"
            f"{ac_text}\n"
            f"  {sep.join(ac_summary)}\n"
            f"  步數　`{escape_md(steps_bar)}`\n"
            f"  {escape_md(str(int(total_steps)))} ／ {escape_md(str(steps_goal))} 步\n"
            f"  消耗　`{escape_md(burn_bar)}`\n"
            f"  {escape_md(str(int(total_burn)))} ／ {escape_md(str(burn_goal))} kcal"
        )

    if weight_rows:
        valid_wt = [r for r in weight_rows if len(r) > 1 and r[1] and float(r[1]) > 0]
        if valid_wt:
            latest = valid_wt[-1]
            wt_ts  = short_time(latest[0])
            wt_val = latest[1]
            sections.append(
                f"━━ ⚖️ *體重* ━━\n"
                f"  {escape_md(str(wt_val))} kg  _{escape_md(wt_ts)}_"
            )

    if not sections:
        return "🏃 *今日運動／活動／體重*\n\n_尚無記錄_"

    return "\n\n".join(sections)


# 保留舊格式函式供週報/月報/總資料使用
def format_diet_report(rows: list[list], title: str, goal_kcal: int = 0) -> str:
    if not rows:
        return f"*{escape_md(title)}*\n\n📭 尚無飲食記錄\\."

    total_cal = total_prot = total_carb = total_fib = 0.0
    lines = [f"*{escape_md(title)}*\n"]

    for i, r in enumerate(rows, 1):
        ts   = short_time(r[0]) if len(r) > 0 else "?"
        name = truncate(r[1]) if len(r) > 1 else "?"
        cal  = float(r[2]) if len(r) > 2 and r[2] else 0
        prot = float(r[3]) if len(r) > 3 and r[3] else 0
        carb = float(r[4]) if len(r) > 4 and r[4] else 0
        fib  = float(r[5]) if len(r) > 5 and r[5] else 0
        total_cal += cal; total_prot += prot; total_carb += carb; total_fib += fib
        lines.append(
            f"  `{i:02d}` {escape_md(name)}  "
            f"🔥{escape_md(str(int(cal)))} \\| 🥩{escape_md(f'{prot:.0f}')}g  _{escape_md(ts)}_"
        )

    bar = progress_bar(total_cal, goal_kcal) if goal_kcal > 0 else ""
    available = goal_kcal - total_cal
    if goal_kcal > 0:
        gap_line = (
            f"\n  🔴 盈餘 \\+{escape_md(str(int(-available)))} kcal"
            if available < 0 else
            f"\n  🟢 還可攝取 {escape_md(str(int(available)))} kcal"
        )
    else:
        gap_line = ""

    lines.append(
        f"\n━━━━━━━━━━━━━━━━\n"
        f"📊 *合計*\n"
        f"  🔥 {escape_md(str(int(total_cal)))} ／ {escape_md(str(goal_kcal))} kcal"
        f"  `{escape_md(bar)}`{gap_line}\n"
        f"  🥩 蛋白質 {escape_md(f'{total_prot:.0f}')} g  "
        f"🌾 碳水 {escape_md(f'{total_carb:.0f}')} g  "
        f"🌿 纖維 {escape_md(f'{total_fib:.0f}')} g"
    )
    return "\n".join(lines)


def format_exercise_report(rows: list[list], title: str) -> str:
    if not rows:
        return f"*{escape_md(title)}*\n\n📭 尚無運動記錄\\."
    ex_text, total_dur = format_exercise_section(rows)
    return (
        f"*{escape_md(title)}*\n\n"
        f"{ex_text}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⏱ *總計 {escape_md(str(total_dur))} 分鐘*"
    )


def format_weight_report(rows: list[list], title: str) -> str:
    if not rows:
        return f"*{escape_md(title)}*\n\n📭 尚無體重記錄\\."
    lines = [f"*{escape_md(title)}*\n"]
    for i, r in enumerate(rows, 1):
        ts   = short_time(r[0]) if len(r) > 0 else "?"
        wt   = r[1] if len(r) > 1 else "?"
        note = r[2] if len(r) > 2 else ""
        note_str = f"  {escape_md(note[:20])}" if note else ""
        lines.append(f"  `{i:02d}` ⚖️ {escape_md(wt)} kg  _{escape_md(ts)}_{note_str}")
    return "\n".join(lines)


def format_activity_report(rows: list[list], title: str) -> str:
    if not rows:
        return f"*{escape_md(title)}*\n\n📭 尚無活動數據記錄\\."
    ac_text, total_steps, total_cal, total_hp, total_am = format_activity_section(rows)
    summary = []
    if total_steps: summary.append(f"👟 {escape_md(str(int(total_steps)))} 步")
    if total_cal:   summary.append(f"🔥 {escape_md(str(int(total_cal)))} kcal")
    if total_hp:    summary.append(f"❤️ {escape_md(str(int(total_hp)))} HP")
    sep = "  \\|  "
    return (
        f"*{escape_md(title)}*\n\n"
        f"{ac_text}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{sep.join(summary)}"
    )



async def show_water_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示今日飲水進度"""
    water_ml   = await fetch_water_today()
    water_goal = await load_water_goal()
    bar        = progress_bar(water_ml, water_goal)
    remaining  = water_goal - water_ml

    if water_ml >= water_goal:
        status = f"✅ *今日飲水已達標\\！超出 {escape_md(str(water_ml - water_goal))} ml*"
    else:
        status = f"💧 *還需補充 {escape_md(str(remaining))} ml*"

    await update.message.reply_text(
        f"💧 *今日飲水記錄*\n\n"
        f"`{escape_md(bar)}`\n"
        f"{escape_md(str(water_ml))} ／ {escape_md(str(water_goal))} ml\n\n"
        f"{status}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=SUB_KEYBOARD_WATER
    )


# ════════════════════════════════════════════════════════════
#  💪 Muscle Heatmap — SVG-based intensity map
#
#  Requires (install once on your machine):
#    pip install cairosvg lxml
#
#  Place muscle_body.svg in the same folder as main.py.
#  The SVG has one <path id="肌肉名"> per muscle group.
#  Python reads training counts, maps them to a blue gradient,
#  rewrites fill colors via lxml, then rasterises with cairosvg.
# ════════════════════════════════════════════════════════════

import os as _os_svg

MUSCLE_SVG_PATH = _os_svg.path.join(_os_svg.path.dirname(__file__), "muscle_body.svg")

# Which SVG element IDs map to which Chinese muscle name.
# Each entry: (svg_id, chinese_name)
# If a muscle appears on both front/back, list all relevant IDs.
_MUSCLE_SVG_IDS: list[tuple[str, str]] = [
    # ── Front ────────────────────────────────────────────────
    ("斜方肌_front",      "斜方肌"),
    ("胸大肌_L",          "胸大肌"),
    ("胸大肌_R",          "胸大肌"),
    ("胸小肌_L",          "胸小肌"),
    ("胸小肌_R",          "胸小肌"),
    ("前三角肌_L",        "前三角肌"),
    ("前三角肌_R",        "前三角肌"),
    ("側三角肌_L",        "側三角肌"),
    ("側三角肌_R",        "側三角肌"),
    ("肱二頭肌_L",        "肱二頭肌"),
    ("肱二頭肌_R",        "肱二頭肌"),
    ("肱三頭肌_L_front",  "肱三頭肌"),
    ("肱三頭肌_R_front",  "肱三頭肌"),
    ("前臂肌_L",          "前臂肌"),
    ("前臂肌_R",          "前臂肌"),
    ("腹直肌_1L",         "腹直肌"),
    ("腹直肌_1R",         "腹直肌"),
    ("腹直肌_2L",         "腹直肌"),
    ("腹直肌_2R",         "腹直肌"),
    ("腹直肌_3L",         "腹直肌"),
    ("腹直肌_3R",         "腹直肌"),
    ("腹直肌_4L",         "腹直肌"),
    ("腹直肌_4R",         "腹直肌"),
    ("腹斜肌_L",          "腹斜肌"),
    ("腹斜肌_R",          "腹斜肌"),
    ("腹橫肌",            "腹橫肌"),
    ("髖屈肌_L",          "髖屈肌"),
    ("髖屈肌_R",          "髖屈肌"),
    ("股四頭肌_L",        "股四頭肌"),
    ("股四頭肌_R",        "股四頭肌"),
    ("臀中肌_front_L",    "臀中肌"),
    ("臀中肌_front_R",    "臀中肌"),
    ("小腿肌_front_L",    "小腿肌"),
    ("小腿肌_front_R",    "小腿肌"),
    # ── Back ─────────────────────────────────────────────────
    ("斜方肌_back",       "斜方肌"),
    ("斜方肌_lower",      "斜方肌"),
    ("後三角肌_L",        "後三角肌"),
    ("後三角肌_R",        "後三角肌"),
    ("菱形肌",            "菱形肌"),
    ("背闊肌_L",          "背闊肌"),
    ("背闊肌_R",          "背闊肌"),
    ("豎脊肌_L",          "豎脊肌"),
    ("豎脊肌_R",          "豎脊肌"),
    ("肱三頭肌_L",        "肱三頭肌"),
    ("肱三頭肌_R",        "肱三頭肌"),
    ("前臂肌_back_L",     "前臂肌"),
    ("前臂肌_back_R",     "前臂肌"),
    ("臀大肌_L",          "臀大肌"),
    ("臀大肌_R",          "臀大肌"),
    ("臀中肌_back_L",     "臀中肌"),
    ("臀中肌_back_R",     "臀中肌"),
    ("腿後肌_L",          "腿後肌"),
    ("腿後肌_R",          "腿後肌"),
    ("小腿肌_back_L",     "小腿肌"),
    ("小腿肌_back_R",     "小腿肌"),
]


def _intensity_to_color(count: int, max_count: int) -> str:
    """
    Map training count → hex fill color.
    0 = dim slate (#1e2235)
    max = bright cyan-blue (#5bc8ff)
    Uses perceptual blue gradient.
    """
    if count <= 0 or max_count <= 0:
        return "#1e2235"   # untrained — near-black
    ratio = min(count / max_count, 1.0)
    # Interpolate: dark navy → vivid cyan-blue
    # R: 0x1e → 0x35  (30 → 53)
    # G: 0x22 → 0xb0  (34 → 176)
    # B: 0x35 → 0xff  (53 → 255)
    r = int(30  + ratio * (53  - 30 ))
    g = int(34  + ratio * (176 - 34 ))
    b = int(53  + ratio * (255 - 53 ))
    # Opacity encoded as alpha in RGBA isn't used here; brightness carries intensity
    return f"#{r:02x}{g:02x}{b:02x}"


def _generate_muscle_heatmap_png(muscle_counts: dict) -> bytes | None:
    """
    Reads muscle_body.svg, rewrites fill colors based on muscle_counts,
    converts to PNG via cairosvg, returns PNG bytes.
    Returns None if SVG missing or cairosvg not installed.
    """
    try:
        import cairosvg
        from lxml import etree
        import copy, io

        if not _os_svg.path.exists(MUSCLE_SVG_PATH):
            logger.error("muscle_body.svg not found at %s", MUSCLE_SVG_PATH)
            return None

        # Parse SVG
        tree = etree.parse(MUSCLE_SVG_PATH)
        root = tree.getroot()
        ns   = {"svg": "http://www.w3.org/2000/svg"}

        # Find max count for normalization
        max_count = max(muscle_counts.values(), default=1)
        if max_count == 0:
            max_count = 1

        # Build id → color map
        color_map: dict[str, str] = {}
        for svg_id, zh_name in _MUSCLE_SVG_IDS:
            count = muscle_counts.get(zh_name, 0)
            color_map[svg_id] = _intensity_to_color(count, max_count)

        # Rewrite fill colors in SVG elements
        # Elements can be <path>, <rect>, <ellipse>, <polygon> with id=
        for elem in root.iter():
            elem_id = elem.get("id", "")
            if elem_id in color_map:
                # Update fill attribute directly
                elem.set("fill", color_map[elem_id])
                # Also update style if it contains fill
                style = elem.get("style", "")
                if "fill:" in style:
                    import re as _re
                    style = _re.sub(
                        r"fill\s*:\s*[^;]+",
                        f"fill:{color_map[elem_id]}",
                        style
                    )
                    elem.set("style", style)

        # Add "Muscle Intensity This Week" dynamic title with date
        from datetime import date as _date
        today_label = _date.today().strftime("%b %d, %Y")
        for txt in root.iter("{http://www.w3.org/2000/svg}text"):
            if "Muscle Intensity" in (txt.text or ""):
                txt.text = f"Muscle Intensity — Week of {today_label}"
                break

        # Serialise modified SVG to bytes
        svg_bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8")

        # Rasterise to PNG at 2x scale (1600×1720) for crisp Telegram image
        png_bytes = cairosvg.svg2png(
            bytestring=svg_bytes,
            output_width=800,
            output_height=860,
        )
        return png_bytes

    except ImportError:
        logger.error(
            "cairosvg or lxml not installed. "
            "Run: pip install cairosvg lxml"
        )
        return None
    except Exception as e:
        logger.error("Muscle heatmap generation failed: %s", e)
        return None


async def generate_muscle_heatmap(muscle_counts: dict) -> bytes | None:
    """Async wrapper — runs SVG processing in thread pool."""
    return await asyncio.to_thread(_generate_muscle_heatmap_png, muscle_counts)


async def show_muscle_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示本週所有肌肉訓練次數（含0次），次數即進度條長度"""
    muscle_counts = await fetch_weekly_muscle_stats()

    # 完整22個肌肉部位清單（固定順序）
    MUSCLE_LIST = [
        ("斜方肌",   "Trapezius / Traps",       "🦴"),
        ("胸大肌",   "Chest / Pecs",             "🫁"),
        ("胸小肌",   "Pectoralis Minor",          "🫁"),
        ("三角肌",   "Deltoids / Delts",          "🔺"),
        ("前三角肌", "Anterior Deltoid",          "🔺"),
        ("側三角肌", "Lateral Deltoid",           "🔺"),
        ("後三角肌", "Rear Deltoid",              "🔺"),
        ("背闊肌",   "Latissimus Dorsi / Lats",   "🔙"),
        ("豎脊肌",   "Erector Spinae",            "🔙"),
        ("菱形肌",   "Rhomboids",                 "🔙"),
        ("肱二頭肌", "Biceps",                    "💪"),
        ("肱三頭肌", "Triceps",                   "💪"),
        ("前臂肌",   "Forearms",                  "💪"),
        ("腹直肌",   "Abs / Rectus Abdominis",    "⚡"),
        ("腹斜肌",   "Obliques",                  "⚡"),
        ("腹橫肌",   "Transverse Abdominis",      "⚡"),
        ("股四頭肌", "Quadriceps / Quads",        "🦵"),
        ("腿後肌",   "Hamstrings",                "🦵"),
        ("臀大肌",   "Glutes",                    "🦵"),
        ("臀中肌",   "Gluteus Medius",            "🦵"),
        ("小腿肌",   "Calves",                    "🦵"),
        ("髖屈肌",   "Hip Flexors",               "🦵"),
    ]

    def count_bar(n: int) -> str:
        """0次=░░，n次=n*2個█，純粹反映次數"""
        if n == 0:
            return "░░"
        return "█" * (n * 2)

    lines_out = ["💪 *本週肌肉訓練紀錄*\n"]

    for zh, en, emoji in MUSCLE_LIST:
        count = muscle_counts.get(zh, 0)
        bar   = count_bar(count)
        count_str = f"{count} 次" if count > 0 else "0 次"
        lines_out.append(
            f"{emoji} *{escape_md(zh)}* _{escape_md(en)}_\n"
            f"   `{bar}` {escape_md(count_str)}"
        )

    # 記錄了但不在清單裡的肌肉（AI 返回的其他名稱）
    known = {zh for zh, _, _ in MUSCLE_LIST}
    extras = [(m, c) for m, c in muscle_counts.items() if m not in known and c > 0]
    if extras:
        extras.sort(key=lambda x: -x[1])
        lines_out.append("\n_其他：_")
        for m, c in extras:
            lines_out.append(f"🏋️ *{escape_md(m)}*\n   `{count_bar(c)}` {escape_md(str(c))} 次")

    trained = sum(1 for _, c in [(zh, muscle_counts.get(zh,0)) for zh,_,_ in MUSCLE_LIST] if c > 0)
    total   = sum(muscle_counts.values())
    lines_out.append(
        f"\n_本週訓練了 {escape_md(str(trained))}/22 個肌群，合計 {escape_md(str(total))} 次_"
    )

    await update.message.reply_text(
        "\n\n".join(lines_out),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=SUB_KEYBOARD_EXERCISE
    )

    # ── Muscle intensity heatmap (SVG → PNG) ─────────────────
    heatmap_bytes = await generate_muscle_heatmap(muscle_counts)
    if heatmap_bytes:
        import io as _io_hm
        await update.message.reply_photo(
            photo=_io_hm.BytesIO(heatmap_bytes),
            caption="💪 Muscle Intensity Heatmap — This Week"
        )
    else:
        # Fallback: send the static anatomy images if SVG fails
        import os as _os2
        from telegram import InputMediaPhoto
        if _os2.path.exists(ANATOMY_ANTERIOR_PATH) and _os2.path.exists(ANATOMY_POSTERIOR_PATH):
            try:
                with open(ANATOMY_ANTERIOR_PATH, "rb") as f_ant, \
                     open(ANATOMY_POSTERIOR_PATH, "rb") as f_post:
                    await update.message.reply_media_group([
                        InputMediaPhoto(media=f_ant,  caption="Anterior View"),
                        InputMediaPhoto(media=f_post, caption="Posterior View"),
                    ])
            except Exception as e:
                logger.error("Fallback anatomy image send failed: %s", e)

async def check_and_send_smart_alerts(bot, chat_id: int):
    """智能推送：進度里程碑（30/50/80/100%）+ 警告 + 未記錄提醒（每種每天只發一次）"""
    if not chat_id:
        return
    td = today_str()

    # ── 80句里程碑激勵語（4指標 × 4階段 × 5句）──────────────
    MILESTONE_MSG = {
        # 熱量攝取
        "kcal_30": [
            "🍽 熱量完成30%！早餐吃得好，一天精力充沛！",
            "🌅 今日熱量進度30%，開局不錯，繼續保持！",
            "✨ 攝取30%達成！身體正在吸收今天第一波能量！",
            "🎯 熱量記錄中，30%完成！養成記錄習慣很重要！",
            "💪 今日攝取剛起步，30%！保持節奏！",
        ],
        "kcal_50": [
            "🍽 熱量攝取過半！下午繼續加油，保持平衡！",
            "⚡ 今日熱量50%！中途補充得好，後半段加油！",
            "🎯 過半了！注意下午和晚餐的分配，別超標！",
            "💡 攝取50%，記得多喝水幫助消化！",
            "🌟 熱量進度一半！你對自己的身體越來越了解了！",
        ],
        "kcal_80": [
            "⚠️ 今日熱量已到80%，晚餐注意份量！",
            "🔔 攝取快到上限了！80%！剩下空間留給健康食物！",
            "💛 快達標了！最後20%選高蛋白低脂的食物最好！",
            "📊 80%警戒線！今晚可以考慮輕食或蔬菜為主！",
            "🍵 攝取80%到了！可以喝杯茶，讓身體感受飽足感！",
        ],
        "kcal_100": [
            "✅ 今日熱量目標達成！完美收官，繼續保持！",
            "🏆 熱量目標100%！今天的飲食管理非常出色！",
            "🎉 達標了！你的自律值得被讚賞！",
            "💎 熱量剛好達標！這就是健康生活的藝術！",
            "🌟 完成今日熱量目標！身體謝謝你的用心！",
        ],
        # 蛋白質
        "prot_30": [
            "🥩 蛋白質攝取30%！肌肉在等這些原料呢！",
            "💪 蛋白質進度30%，繼續補充，幫助肌肉恢復！",
            "🥚 今日蛋白質剛起跑，30%！雞胸肉在向你招手！",
            "⚡ 蛋白質30%到位，身體開始進入修復模式！",
            "🏋️ 蛋白質30%！訓練後的肌肉合成需要你繼續補！",
        ],
        "prot_50": [
            "🥩 蛋白質過半！肌肉在感謝你！",
            "💪 蛋白質50%達成！繼續補充，今日訓練效果更好！",
            "🎯 蛋白質進度一半！你的肌肉正在慢慢成長！",
            "🥗 蛋白質50%！午餐選高蛋白食物繼續衝刺！",
            "⚡ 蛋白質補充得不錯，50%！身體修復中！",
        ],
        "prot_80": [
            "🥩 蛋白質快到了！80%！最後一把衝刺！",
            "💪 蛋白質80%！再一份雞胸或豆腐就完美了！",
            "🔥 蛋白質快達標！80%！肌肉合成效率最高的時刻！",
            "🎯 80%蛋白質達成！今天的訓練效果會非常好！",
            "⚡ 差一點就滿了！蛋白質80%，衝最後一里路！",
        ],
        "prot_100": [
            "🥩 蛋白質目標完成！今天的肌肉一定會有感！",
            "💪 蛋白質100%！訓練+充足蛋白質=完美增肌公式！",
            "🏆 蛋白質達標！你的身體正在高效率地修復肌肉！",
            "🎉 蛋白質完成！你比大多數人更懂得照顧自己！",
            "⚡ 蛋白質100%達成！今晚睡眠時肌肉修復效果極佳！",
        ],
        # 步數
        "steps_30": [
            "👟 步數完成30%！好的開始，繼續走！",
            "🚶 步數30%到了！每一步都是對心臟的呵護！",
            "🌿 走了30%的路程！保持節奏，今天一定達標！",
            "💫 步數進度30%！這些步伐都是在燃燒卡路里！",
            "🎯 步數剛起步，30%！午休時多走走！",
        ],
        "steps_50": [
            "👟 步數過半！一半的路程一半的健康！",
            "🚶 步數50%達成！下午繼續，今天肯定達標！",
            "⚡ 走了一半了！步數50%，你的心肺在變強！",
            "🌟 步數中途站50%！繼續走，目標近了！",
            "💪 步數一半！每天走夠步數，長壽秘訣之一！",
        ],
        "steps_80": [
            "👟 步數快到了！80%！最後一段衝刺！",
            "🏃 步數80%！再一點點就達標，加快腳步！",
            "🔥 快走完了！步數80%！去廁所多繞一圈也算！",
            "🎯 步數80%！這個速度今天肯定能達標！",
            "💫 差一點！步數80%，隨便散個步就完成了！",
        ],
        "steps_100": [
            "👟 步數達標！今天走了很多路，腿部在感謝你！",
            "🏆 步數100%完成！你的心血管健康又進步了一點！",
            "🎉 今天步數達標！WHO說每天10000步，你做到了！",
            "💎 步數目標完成！堅持每天走夠步，身體變化肉眼可見！",
            "🌟 步數達標！這些積累的步伐正在改善你的代謝！",
        ],
        # 運動消耗
        "burn_30": [
            "🔥 消耗達到30%！身體開始燃燒模式了！",
            "⚡ 卡路里消耗30%！繼續動，脂肪在慢慢分解！",
            "💪 消耗進度30%！今天的訓練效果正在累積！",
            "🏋️ 消耗30%到位！每一滴汗都是進步！",
            "🎯 消耗剛起步，30%！繼續保持運動強度！",
        ],
        "burn_50": [
            "🔥 消耗過半！身體正在高效燃脂！",
            "⚡ 消耗50%達成！你的代謝率正在提升！",
            "💪 消耗一半！運動的後燃效應讓你繼續燃燒！",
            "🏃 消耗50%！繼續，後半段更有成就感！",
            "🌟 消耗中途站50%！你比昨天的自己更進一步！",
        ],
        "burn_80": [
            "🔥 消耗快達標！80%！最後衝刺！",
            "⚡ 消耗80%！再一組訓練就完美了！",
            "💪 快到了！消耗80%，今天的努力看得見！",
            "🏋️ 消耗80%！你的身體正在高速燃燒卡路里！",
            "🎯 差一點點！消耗80%，收尾的感覺最好！",
        ],
        "burn_100": [
            "🔥 消耗目標達成！今天的訓練非常成功！",
            "🏆 消耗100%！你的努力正在重塑你的身體！",
            "🎉 消耗達標！每次完成目標都是在突破自己！",
            "💎 消耗完成！今天的汗水是明天身材的本錢！",
            "⚡ 消耗100%達成！身體正在進入恢復和成長模式！",
        ],
    }

    try:
        diet_rows     = await fetch_diet_rows(filter_date=td)
        activity_rows = await fetch_activity_rows(filter_date=td)
        goal_kcal     = await load_goal()
        protein_goal  = await load_protein_goal()
        burn_goal_v   = await load_burn_goal()
        steps_goal    = await load_steps_goal()
        water_goal    = await load_water_goal()
        water_ml      = await fetch_water_today()

        total_in    = sum(float(r[2]) for r in diet_rows     if len(r) > 2 and r[2])
        total_prot  = sum(float(r[3]) for r in diet_rows     if len(r) > 3 and r[3])
        total_burn  = sum(float(r[2]) for r in activity_rows if len(r) > 2 and r[2])
        total_steps = sum(float(r[1]) for r in activity_rows if len(r) > 1 and r[1])

        tz       = pytz.timezone(TIMEZONE)
        hour_now = datetime.now(tz).hour

        # ── 今日已發送的通知類型（防重複）─────────────────────
        def _load_sent():
            try:
                gc = get_sheet_client()
                ss = gc.open_by_key(SHEET_ID)
                ws = ensure_worksheet(ss, "Settings", ["Key", "Value", "UpdatedAt"])
                for r in ws.get_all_values():
                    if r and r[0] == f"alert_sent_{td}":
                        return set(json.loads(r[1]))
            except Exception:
                pass
            return set()

        def _save_sent(sent_set: set):
            try:
                gc = get_sheet_client()
                ss = gc.open_by_key(SHEET_ID)
                ws = ensure_worksheet(ss, "Settings", ["Key", "Value", "UpdatedAt"])
                key = f"alert_sent_{td}"
                val = json.dumps(list(sent_set))
                rows = ws.get_all_values()
                for i, r in enumerate(rows):
                    if r and r[0] == key:
                        ws.update(f"A{i+1}:C{i+1}", [[key, val, now_str()]])
                        return
                ws.append_row([key, val, now_str()])
            except Exception as e:
                logger.error("儲存推送記錄失敗：%s", e)

        sent_today = await asyncio.to_thread(_load_sent)
        new_alerts = []   # (key, message)

        def _milestone_msg(key: str) -> str:
            """從對應清單隨機選一句，用 sent_today 避免重複"""
            pool = MILESTONE_MSG.get(key, [])
            if not pool:
                return ""
            # 找出今天還沒發過的句子（用 key_0, key_1 等記錄索引）
            used = {int(k.split("_idx_")[1]) for k in sent_today
                    if k.startswith(f"{key}_idx_")}
            avail = [i for i in range(len(pool)) if i not in used]
            if not avail:
                avail = list(range(len(pool)))  # 全部用完就重置
            idx = _random.choice(avail)
            sent_today.add(f"{key}_idx_{idx}")
            return pool[idx]

        def _check_milestone(metric_key: str, value: float, goal: float):
            """檢查30/50/80/100%里程碑，尚未發送就加入隊列"""
            if goal <= 0 or value <= 0:
                return
            pct = value / goal * 100
            for threshold, label in [(100, "100"), (80, "80"), (50, "50"), (30, "30")]:
                alert_key = f"{metric_key}_{label}"
                if pct >= threshold and alert_key not in sent_today:
                    msg = _milestone_msg(alert_key)
                    if msg:
                        new_alerts.append((alert_key, msg))
                    break  # 只發最高達到的那個（避免一次發多條）

        # ── 四個指標里程碑 ─────────────────────────────────────
        _check_milestone("kcal",  total_in,    goal_kcal)
        _check_milestone("prot",  total_prot,  protein_goal)
        _check_milestone("steps", total_steps, steps_goal)
        _check_milestone("burn",  total_burn,  burn_goal_v)

        # ── 原有警告/提醒（保留）──────────────────────────────
        if "kcal_warn" not in sent_today:
            if goal_kcal > 0 and total_in >= goal_kcal * 0.85 and total_in < goal_kcal:
                remaining = int(goal_kcal - total_in)
                new_alerts.append(("kcal_warn",
                    f"⚠️ 今日已攝取 {int(total_in)} kcal，距目標只剩 *{remaining} kcal*，注意控制\\!"))

        if "kcal_over" not in sent_today:
            if total_in > goal_kcal and goal_kcal > 0:
                over = int(total_in - goal_kcal)
                new_alerts.append(("kcal_over",
                    f"🔴 今日熱量超標 *{over} kcal*\\!"))

        if "water_warn" not in sent_today and hour_now >= 18:
            if water_goal > 0 and water_ml < water_goal * 0.5:
                new_alerts.append(("water_warn",
                    f"💧 今日飲水只有 *{water_ml} ml*，記得多喝水\\!"))

        if "no_diet" not in sent_today and hour_now >= 14:
            if not diet_rows:
                new_alerts.append(("no_diet",
                    f"📝 今天還沒有記錄飲食，記得補記喔\\!"))

        if not new_alerts:
            return

        for key, msg in new_alerts:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN_V2)
            sent_today.add(key)

        await asyncio.to_thread(_save_sent, sent_today)

    except Exception as e:
        logger.error("智能推送失敗：%s", e)


# ════════════════════════════════════════════════════════════
#  每日定時推送報告
# ════════════════════════════════════════════════════════════

async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    if not USER_CHAT_ID:
        return
    td = today_str()
    try:
        diet_rows      = await fetch_diet_rows(filter_date=td)
        exercise_rows  = await fetch_exercise_rows(filter_date=td)
        weight_rows_td = await fetch_weight_rows(filter_date=td)
        activity_rows  = await fetch_activity_rows(filter_date=td)
        goal_kcal      = await load_goal()
        protein_goal   = await load_protein_goal()
        burn_goal_v    = await load_burn_goal()
        steps_goal     = await load_steps_goal()
        water_goal     = await load_water_goal()
        weight_goal    = await load_weight_goal()
        height_cm      = await load_height()
        water_ml       = await fetch_water_today()
        muscle_plan    = await load_muscle_plan()
        muscle_stats   = await fetch_weekly_category_stats()

        total_in    = sum(float(r[2]) for r in diet_rows     if len(r) > 2 and r[2])
        total_prot  = sum(float(r[3]) for r in diet_rows     if len(r) > 3 and r[3])
        total_burn  = sum(float(r[2]) for r in activity_rows if len(r) > 2 and r[2])
        total_steps = sum(float(r[1]) for r in activity_rows if len(r) > 1 and r[1])

        diff     = total_in - goal_kcal
        diff_str = f"超出 {int(diff)} kcal ⚠️" if diff > 0 else f"低於目標 {int(-diff)} kcal ✅"

        bar_kcal  = progress_bar(total_in,    goal_kcal)
        bar_prot  = progress_bar(total_prot,  protein_goal)
        bar_water = progress_bar(water_ml,    water_goal)
        bar_burn  = progress_bar(total_burn,  burn_goal_v)
        bar_steps = progress_bar(total_steps, steps_goal)

        # ── BMI ──────────────────────────────────────────────
        bmi_line = ""
        if height_cm and height_cm > 0:
            all_weights = await fetch_weight_rows()
            valid_wts   = [float(r[1]) for r in all_weights if len(r) > 1 and r[1] and float(r[1]) > 0]
            if valid_wts:
                latest_wt = valid_wts[-1]
                bmi = latest_wt / ((height_cm / 100) ** 2)
                bmi_label = (
                    "偏輕 🔵" if bmi < 18.5 else
                    "正常 🟢" if bmi < 23   else
                    "過重 🟡" if bmi < 27.5 else
                    "肥胖 🔴"
                )
                bmi_line = f"📊 BMI：{escape_md(f'{bmi:.1f}')} {escape_md(bmi_label)}\n"

        # ── 體重目標進度 ──────────────────────────────────────
        weight_str = ""
        if weight_goal:
            all_weights = await fetch_weight_rows()
            valid_wts   = [float(r[1]) for r in all_weights if len(r) > 1 and r[1] and float(r[1]) > 0]
            if valid_wts:
                latest_wt = valid_wts[-1]
                diff_wt   = latest_wt - weight_goal
                if abs(diff_wt) < 0.1:
                    weight_str = f"🎉 體重目標達成\\! {escape_md(str(latest_wt))} kg\n"
                elif diff_wt > 0:
                    weight_str = f"⚖️ 體重：{escape_md(str(latest_wt))} kg　距目標還差 {escape_md(f'{diff_wt:.1f}')} kg\n"
                else:
                    weight_str = f"⚖️ 體重：{escape_md(str(latest_wt))} kg　已低於目標 {escape_md(f'{abs(diff_wt):.1f}')} kg ✅\n"

        # ── 連續打卡提醒 ──────────────────────────────────────
        all_exercise   = await fetch_exercise_rows()
        yesterday      = (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
        day_before     = (datetime.now().date() - timedelta(days=2)).strftime("%Y-%m-%d")
        has_yesterday  = any(r[0].startswith(yesterday)  for r in all_exercise if r)
        has_day_before = any(r[0].startswith(day_before) for r in all_exercise if r)
        streak_note    = ""
        if not has_yesterday and not has_day_before:
            streak_note = "💪 *已超過2天未記錄運動，今天去動一動吧\\!*\n"
        elif not has_yesterday:
            streak_note = "🏃 *昨天沒有運動記錄，今天繼續加油\\!*\n"

        # ── 本週訓練部位 ──────────────────────────────────────
        muscle_str = ""
        if muscle_plan:
            done_list    = [f"{MUSCLE_EMOJI.get(c,'🏋️')} {c}" for c in muscle_plan if muscle_stats.get(c, 0) >= muscle_plan[c]]
            missing_list = [f"{MUSCLE_EMOJI.get(c,'🏋️')} {c}（{muscle_stats.get(c,0)}/{muscle_plan[c]}）" for c in muscle_plan if muscle_stats.get(c, 0) < muscle_plan[c]]
            if done_list:
                muscle_str += f"✅ {escape_md('、'.join(done_list))}\n"
            if missing_list:
                muscle_str += f"📋 未達標：{escape_md('、'.join(missing_list))}\n"

        try:
            advice = await ai_daily_advice(diet_rows, exercise_rows, weight_rows_td, goal_kcal)
        except Exception:
            advice = "（AI 建議暫時無法取得）"

        report = (
            f"🌙 *今日健康日報 \— {escape_md(td)}*\n\n"
            f"━━ 🍽 *飲食* ━━\n"
            f"攝取　`{escape_md(bar_kcal)}`\n"
            f"　{escape_md(str(int(total_in)))} ／ {escape_md(str(goal_kcal))} kcal  {escape_md(diff_str)}\n"
            f"蛋白質　`{escape_md(bar_prot)}`\n"
            f"　{escape_md(f'{total_prot:.0f}')} ／ {escape_md(str(protein_goal))} g\n\n"
            f"━━ 🏃 *活動* ━━\n"
            f"消耗　`{escape_md(bar_burn)}`\n"
            f"　{escape_md(str(int(total_burn)))} ／ {escape_md(str(burn_goal_v))} kcal\n"
            f"步數　`{escape_md(bar_steps)}`\n"
            f"　{escape_md(str(int(total_steps)))} ／ {escape_md(str(steps_goal))} 步\n\n"
            f"━━ 💧 *飲水* ━━\n"
            f"`{escape_md(bar_water)}`  {escape_md(str(water_ml))} ／ {escape_md(str(water_goal))} ml\n"
        )
        if weight_str or bmi_line:
            report += f"\n━━ ⚖️ *體重* ━━\n{weight_str}{bmi_line}"
        if muscle_str:
            report += f"\n━━ 💪 *本週訓練* ━━\n{muscle_str}"
        if streak_note:
            report += f"\n{streak_note}"
        report += f"\n━━ 💡 *AI 建議* ━━\n{escape_md(advice)}"

        await context.bot.send_message(
            chat_id=USER_CHAT_ID, text=report, parse_mode=ParseMode.MARKDOWN_V2)

        # ── 附上今日進度圓形圖 ────────────────────────────────
        try:
            chart_bytes = await generate_daily_progress_chart(
                total_in, goal_kcal,
                total_prot, protein_goal,
                total_burn, burn_goal_v,
                water_ml, water_goal
            )
            if chart_bytes:
                import io as _io
                await context.bot.send_photo(
                    chat_id=USER_CHAT_ID,
                    photo=_io.BytesIO(chart_bytes),
                    caption="📊 Today's Progress"
                )
        except Exception as chart_err:
            logger.error("每日進度圖推送失敗：%s", chart_err)

        logger.info("每日報告已推送。")
    except Exception as e:
        logger.error("每日報告推送失敗：%s", e)

# ════════════════════════════════════════════════════════════
#  Handlers
# ════════════════════════════════════════════════════════════

async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """撤銷最後一筆記錄"""
    action = await _load_last_action()
    if not action:
        await update.message.reply_text("❌ 沒有可撤銷的記錄")
        return
    sheet_emoji = {"diet": "🍽", "exercise": "🏃", "weight": "⚖️"}
    emoji = sheet_emoji.get(action["sheet"], "📝")
    await update.message.reply_text(
        f"⚠️ *確認撤銷？*\n\n"
        f"{emoji} {escape_md(action['desc'])}\n"
        f"⏰ {escape_md(action['time'])}\n\n"
        f"回覆 `/confirm_undo` 確認，或忽略此訊息取消",
        parse_mode=ParseMode.MARKDOWN_V2)
    context.user_data["pending_undo"] = True


async def cmd_confirm_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """確認執行撤銷"""
    if not context.user_data.get("pending_undo"):
        await update.message.reply_text("❌ 沒有待確認的撤銷操作")
        return
    context.user_data["pending_undo"] = False
    result = await undo_last_action()
    await update.message.reply_text(
        escape_md(result),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=MAIN_KEYBOARD)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = STATE_IDLE
    chat_id = update.effective_chat.id
    logger.info("用戶 Chat ID: %s", chat_id)
    await update.message.reply_text(
        f"💪 *歡迎使用私人健身助理\\!*\n\n"
        f"🆔 您的 Chat ID：`{chat_id}`\n"
        f"_請將此 ID 填入代碼頂部的 USER\\_CHAT\\_ID_\n\n"
        f"請從下方選單選擇功能：",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("請提供體重，例如：`/weight 70\\.5`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    try:
        weight = float(args[0])
    except ValueError:
        await update.message.reply_text("❌ 無效數字\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    await append_weight_row(weight)
    await update.message.reply_text(
        f"✅ *體重已記錄*\n\n⚖️  {escape_md(str(weight))} kg\n⏰ {escape_md(now_str())}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ 撤銷此筆", callback_data="undo_last")
        ]])
    )
    await update.message.reply_text("繼續或選擇其他：", reply_markup=SUB_KEYBOARD_PROFILE)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip()
    state = context.user_data.get("state", STATE_IDLE)

    # ── 返回主選單 ──────────────────────────────────────────
    if text == "🔙 返回主選單":
        context.user_data["state"] = STATE_IDLE
        context.user_data["in_food_quick"] = False
        await update.message.reply_text("請選擇功能分類：", reply_markup=MAIN_KEYBOARD)
        return

    # ── 常用食物快捷鍵（🔁 開頭）────────────────────────────
    if text.startswith("🔁 "):
        food_name = text[2:].strip()
        context.user_data["state"] = STATE_AWAIT_FOOD
        await process_food_text(update, context, text_desc=food_name, image_bytes=None)
        return

    # ── 「➕ 輸入新食物」回到一般輸入模式 ──────────────────
    if text == "📷 上傳照片":
        context.user_data["state"] = STATE_AWAIT_FOOD
        await update.message.reply_text(
            "📷 請直接傳送食物照片，可附加文字說明：",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("🔙 返回主選單")]], resize_keyboard=True))
        return

    if text == "➕ 輸入新食物":
        context.user_data["state"] = STATE_AWAIT_FOOD
        context.user_data["in_food_quick"] = False
        await update.message.reply_text(
            "🍎 請輸入食物名稱或描述，或傳送照片：",
            reply_markup=SUB_KEYBOARD_FOOD)
        return

    # ── 主選單分類 → 顯示子選單 ─────────────────────────────
    if text == "🍎 飲食":
        await update.message.reply_text("🍎 *飲食*", parse_mode=ParseMode.MARKDOWN_V2,
                                        reply_markup=SUB_KEYBOARD_FOOD)
        return
    if text == "🏃 運動":
        await update.message.reply_text("🏃 *運動*", parse_mode=ParseMode.MARKDOWN_V2,
                                        reply_markup=SUB_KEYBOARD_EXERCISE)
        return
    if text == "📱 上傳活動數據":
        context.user_data["state"] = STATE_AWAIT_ACTIVITY
        await update.message.reply_text(
            "📱 *上傳活動數據*\n\n"
            "請輸入今日活動數據，格式：\n"
            "`步數8000 消耗350`\n\n"
            "_或直接上傳手環截圖，AI會自動識別！_",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("🔙 返回主選單")]], resize_keyboard=True))
        return
    if text == "🤖 AI教練":
        await update.message.reply_text("🤖 *AI 健身教練*\n\n_我會根據您的數據給出個人化建議_",
                                        parse_mode=ParseMode.MARKDOWN_V2,
                                        reply_markup=SUB_KEYBOARD_COACH)
        return
    if text == "💬 問教練":
        context.user_data["state"] = STATE_AWAIT_COACH_Q
        await update.message.reply_text(
            "💬 *問我任何健身問題！*\n\n"
            "例如：\n"
            "• _今天吃什麼比較好？_\n"
            "• _我應該練哪個部位？_\n"
            "• _我的減脂效率怎麼樣？_\n"
            "• _幫我安排本週訓練計劃_",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("🔙 返回主選單")]], resize_keyboard=True))
        return
    if text in ("📊 本週分析", "🏋️ 訓練計劃", "📉 減重分析", "💪 部位弱點分析", "🍽 今日飲食建議"):
        # 快捷問題直接回答
        QUICK_Q = {
            "📊 本週分析":   "請幫我分析本週的整體表現，給出評分和改善建議。",
            "🏋️ 訓練計劃":  "根據我的訓練記錄和弱點部位，幫我安排下週的訓練計劃，要具體說明每天練什麼。",
            "📉 減重分析":   "根據我的體重趨勢、飲食和運動數據，分析我的減重進度和效率，告訴我還差什麼。",
            "💪 部位弱點分析":"分析我最近哪些肌肉部位訓練不足，推薦具體的訓練動作和頻率。",
            "🍽 今日飲食建議": "根據我今天的攝取情況，建議我接下來應該吃什麼，要具體說明食物名稱和份量。",
        }
        question = QUICK_Q.get(text, text)
        thinking_msg = await update.message.reply_text("🤖 AI教練分析中\\.\\.\\.⏳", parse_mode=ParseMode.MARKDOWN_V2)
        ctx = await get_coach_context()
        answer = await ask_ai_coach(question, ctx)
        await thinking_msg.delete()
        await update.message.reply_text(
            f"🤖 *AI教練建議*\n\n{escape_md(answer)}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=SUB_KEYBOARD_COACH)
        return
    if text == "👤 個人資料":
        await update.message.reply_text("👤 *個人資料*", parse_mode=ParseMode.MARKDOWN_V2,
                                        reply_markup=SUB_KEYBOARD_PROFILE)
        return
    if text == "📊 查看":
        await update.message.reply_text("📊 *查看數據*", parse_mode=ParseMode.MARKDOWN_V2,
                                        reply_markup=SUB_KEYBOARD_VIEW)
        return
    if text == "💧 喝水":
        water_ml   = await fetch_water_today()
        water_goal = await load_water_goal()
        bar        = progress_bar(water_ml, water_goal)
        await update.message.reply_text(
            f"💧 *喝水記錄*\n\n"
            f"今日：`{escape_md(bar)}`\n"
            f"{escape_md(str(water_ml))} ／ {escape_md(str(water_goal))} ml\n\n"
            f"選擇飲水量快速記錄：",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=SUB_KEYBOARD_WATER)
        return
    if text == "📈 分析":
        await update.message.reply_text("📈 *分析*", parse_mode=ParseMode.MARKDOWN_V2,
                                        reply_markup=SUB_KEYBOARD_ANALYSIS)
        return
    if text == "⚙️ 設定":
        await update.message.reply_text("⚙️ *設定*", parse_mode=ParseMode.MARKDOWN_V2,
                                        reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if text == "🌙 睡眠":
        await update.message.reply_text(
            "🌙 *睡眠追蹤*", parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=SUB_KEYBOARD_SLEEP)
        return

    if text == "📷 上傳睡眠截圖":
        context.user_data["state"] = STATE_AWAIT_SLEEP
        await update.message.reply_text(
            "📷 *上傳睡眠截圖*\n\n"
            "請傳送 *Zepp / Mi Fitness / Xiaomi Health* 睡眠頁面截圖\n\n"
            "_AI 自動識別：入睡 & 起床時間、深睡、淺睡、REM、評分_",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("🔙 返回主選單")]], resize_keyboard=True))
        return

    if text == "🌙 今日睡眠":
        rows = await fetch_sleep_rows(filter_date=today_str())
        if rows:
            display = [""] + list(rows[-1])
            msg = format_sleep_summary(display)
        else:
            msg = "🌙 _今日尚未記錄睡眠，請上傳截圖_"
        await update.message.reply_text(
            msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SLEEP)
        return

    if text in ("📊 睡眠趨勢", "📋 本週睡眠報告"):
        rows = await fetch_sleep_rows(days=7)
        report = format_sleep_week_report(rows)
        await update.message.reply_text(
            report, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SLEEP)
        return

    if text == "🤖 AI睡眠分析":
        thinking = await update.message.reply_text(
            "🤖 *AI 分析中\\.\\.\\.* ⏳", parse_mode=ParseMode.MARKDOWN_V2)
        rows_7 = await fetch_sleep_rows(days=7)
        ctx = await get_coach_context()
        analysis = await get_sleep_ai_analysis(rows_7, ctx)
        await thinking.edit_text("🤖 AI 睡眠分析完成！")
        await update.message.reply_text(
            f"🤖 *AI 睡眠分析報告*\n\n{escape_md(analysis)}",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SLEEP)
        return

    # ── 子選單功能 ──────────────────────────────────────────
    if text == "🍎 上傳食物":
        context.user_data["state"] = STATE_AWAIT_FOOD
        context.user_data["in_food_quick"] = True
        frequent = await load_frequent_foods(top_n=6)
        buttons = []
        if frequent:
            pairs = [frequent[i:i+2] for i in range(0, len(frequent), 2)]
            for pair in pairs:
                buttons.append([KeyboardButton(f"🔁 {f}") for f in pair])
        buttons.append([KeyboardButton("➕ 輸入新食物"), KeyboardButton("📷 上傳照片")])
        buttons.append([KeyboardButton("🔙 返回主選單")])
        quick_kb = ReplyKeyboardMarkup(buttons, resize_keyboard=True)
        header = "🍎 *飲食記錄*\n\n🔁 _點選常用食物快速記錄_\n➕ _或輸入新食物名稱 / 上傳照片_" if frequent \
            else "🍎 *飲食記錄*\n\n_輸入食物名稱、描述，或直接上傳照片_"
        await update.message.reply_text(
            header,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=quick_kb)
        return

    if text == "💡 今日飲食建議":
        td           = today_str()
        diet_rows    = await fetch_diet_rows(filter_date=td)
        activity_rows= await fetch_activity_rows(filter_date=td)
        goal_kcal    = await load_goal()
        protein_goal = await load_protein_goal()
        burn_goal_v  = await load_burn_goal()
        total_in     = sum(float(r[2]) for r in diet_rows    if len(r) > 2 and r[2])
        total_prot   = sum(float(r[3]) for r in diet_rows    if len(r) > 3 and r[3])
        total_burn   = sum(float(r[2]) for r in activity_rows if len(r) > 2 and r[2])
        kcal_gap     = goal_kcal + total_burn - total_in
        protein_gap  = protein_goal - total_prot
        suggestion   = get_food_suggestions(kcal_gap, protein_gap)
        await update.message.reply_text(
            f"💡 *今日飲食建議*\n\n{suggestion}",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_FOOD)
        return

    if text == "📖 食譜庫":
        await update.message.reply_text(
            "📖 *食譜庫*\n\n選擇食譜類別，或根據今日缺口推薦：",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_RECIPES)
        return

    if text == "🔙 飲食選單":
        await update.message.reply_text("🍎 *飲食*", parse_mode=ParseMode.MARKDOWN_V2,
                                        reply_markup=SUB_KEYBOARD_FOOD)
        return

    for recipe_btn, cat_key in [
        ("🏋️ 增肌食譜", "增肌"), ("🔥 減脂食譜", "減脂"),
        ("⚡ 快速備餐", "快速"), ("🥩 高蛋白零食", "零食")
    ]:
        if text == recipe_btn:
            context.user_data["recipe_cat"]  = cat_key
            context.user_data["recipe_page"] = 1
            context.user_data["state"]       = STATE_AWAIT_RECIPE_NUM
            await update.message.reply_text(
                get_recipe_list(cat_key, 1),
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_RECIPES)
            return

    # 食譜分頁：「食譜增肌2」格式
    import re as _re
    _page_m = _re.match(r"食譜(.+?)(\d+)$", text)
    if _page_m:
        cat_map = {"增肌":"增肌","減脂":"減脂","快速":"快速","零食":"零食"}
        cat  = cat_map.get(_page_m.group(1), "增肌")
        page = int(_page_m.group(2))
        context.user_data["recipe_cat"]  = cat
        context.user_data["recipe_page"] = page
        context.user_data["state"]       = STATE_AWAIT_RECIPE_NUM
        await update.message.reply_text(
            get_recipe_list(cat, page),
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_RECIPES)
        return

    if text == "💡 根據缺口推薦食譜":
        td           = today_str()
        diet_rows    = await fetch_diet_rows(filter_date=td)
        activity_rows= await fetch_activity_rows(filter_date=td)
        goal_kcal    = await load_goal()
        protein_goal = await load_protein_goal()
        total_in     = sum(float(r[2]) for r in diet_rows    if len(r) > 2 and r[2])
        total_prot   = sum(float(r[3]) for r in diet_rows    if len(r) > 3 and r[3])
        total_burn   = sum(float(r[2]) for r in activity_rows if len(r) > 2 and r[2])
        kcal_gap     = goal_kcal + total_burn - total_in
        protein_gap  = protein_goal - total_prot
        rec_text     = get_recipe_by_gap(kcal_gap, protein_gap)
        # 找到推薦類別設定狀態
        if "增肌" in rec_text:
            context.user_data["recipe_cat"] = "增肌"
        elif "減脂" in rec_text:
            context.user_data["recipe_cat"] = "減脂"
        elif "零食" in rec_text:
            context.user_data["recipe_cat"] = "零食"
        else:
            context.user_data["recipe_cat"] = "快速"
        context.user_data["state"] = STATE_AWAIT_RECIPE_NUM
        await update.message.reply_text(
            rec_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_RECIPES)
        return

    if text == "🏃 上傳運動記錄":
        context.user_data["state"] = STATE_AWAIT_EXERCISE
        await update.message.reply_text(
            "🏃 *運動記錄模式*\n\n支援以下輸入方式：\n\n"
            "📝 *純文字：*\n`跑步 30 分鐘`\n`游泳 45 分鐘`\n`重訓胸背 1 小時`\n\n"
            "📷 *純照片：*\n拍運動截圖、跑步機螢幕、健身器材等\n\n"
            "📷\\+📝 *照片\\+說明：*\n發照片時附上文字說明，AI 更準確",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "📊 本週運動記錄":
        await show_week_exercise(update, context); return

    if text == "👟 上傳活動數據":
        context.user_data["state"] = STATE_AWAIT_ACTIVITY
        await update.message.reply_text(
            "👟 *活動數據記錄模式*\n\n"
            "請輸入今日活動數據，可只輸入有的部分：\n\n"
            "*格式範例：*\n"
            "`步數8000 消耗350 心率點12 活動點30`\n\n"
            "*欄位說明：*\n"
            "• 步數 \\— 今日步數\n"
            "• 消耗 \\— 卡路里消耗 \\(kcal\\)\n"
            "• 心率點 \\— Google Fit Heart Points\n"
            "• 活動點 \\— Move Minutes / 活躍分鐘\n\n"
            "也可以分行輸入，例如：\n"
            "`步數: 8000`\n`消耗: 350`\n`心率點: 12`",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "📸 上傳身材照":
        context.user_data["profile_nav"] = True
        context.user_data["state"] = STATE_AWAIT_BODY
        await update.message.reply_text(
            "📸 *身材照上傳模式*\n\n請傳送一張身材照片，AI 將進行體態評估\\.",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "⚖️ 記錄體重" or text == "⚖️ 體重上傳":
        context.user_data["state"] = STATE_AWAIT_WEIGHT
        await update.message.reply_text(
            "⚖️ *體重記錄模式*\n\n請輸入體重數字（kg），例如：`70\\.5`",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "🎯 設定熱量目標":
        context.user_data["state"] = STATE_AWAIT_GOAL
        current_goal = await load_goal()
        await update.message.reply_text(
            f"🎯 *設定每日熱量目標*\n\n"
            f"目前目標：{escape_md(str(current_goal))} kcal\n\n"
            f"請輸入新的每日熱量目標（kcal），例如：`1800`",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "🔥 設定消耗目標":
        context.user_data["state"] = STATE_AWAIT_BURN_GOAL
        current = await load_burn_goal()
        await update.message.reply_text(
            f"🔥 *設定每日消耗目標*\n\n"
            f"目前目標：{escape_md(str(current))} kcal\n\n"
            f"請輸入每日目標消耗熱量（kcal），例如：`500`",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "🥩 設定蛋白質目標":
        context.user_data["state"] = STATE_AWAIT_PROTEIN_GOAL
        current = await load_protein_goal()
        await update.message.reply_text(
            f"🥩 *設定每日蛋白質目標*\n\n"
            f"目前目標：{escape_md(str(current))} g\n\n"
            f"請輸入每日蛋白質攝取目標（g），例如：`80`\n"
            f"_建議：體重 ×1\\.2 \\~ 2\\.0 g，例如 70kg → 84\\~140g_",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "👟 設定步數目標":
        context.user_data["state"] = STATE_AWAIT_STEPS_GOAL
        current = await load_steps_goal()
        await update.message.reply_text(
            f"👟 *設定每日步數目標*\n\n"
            f"目前目標：{escape_md(str(current))} 步\n\n"
            f"請輸入每日步數目標，例如：`8000`\n"
            f"_WHO 建議：每日 8000\\~10000 步_",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "⚖️ 設定體重目標":
        context.user_data["state"] = STATE_AWAIT_WEIGHT_GOAL
        current_wg = await load_weight_goal()
        current_str = f"目前目標：{escape_md(str(current_wg))} kg\n\n" if current_wg else ""
        await update.message.reply_text(
            f"⚖️ *設定體重目標*\n\n"
            f"{current_str}"
            f"請輸入目標體重（kg），例如：`70\\.0`\n"
            f"_每日推送和體重趨勢會自動顯示距離目標的差距_",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "💧 設定飲水目標":
        context.user_data["state"] = STATE_AWAIT_WATER_GOAL
        current = await load_water_goal()
        await update.message.reply_text(
            f"💧 *設定每日飲水目標*\n\n"
            f"目前目標：{escape_md(str(current))} ml\n\n"
            f"請輸入每日飲水目標（ml），例如：`2000`\n"
            f"_WHO 建議：每日 2000\\~2500 ml_",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "📏 我的身高/BMI":
        # 顯示目前身高和 BMI，提供設定入口
        height_cm   = await load_height()
        all_weights = await fetch_weight_rows()
        valid_wts   = [float(r[1]) for r in all_weights if len(r) > 1 and r[1] and float(r[1]) > 0]
        latest_wt   = valid_wts[-1] if valid_wts else None

        if height_cm and height_cm > 0 and latest_wt:
            bmi = latest_wt / ((height_cm / 100) ** 2)
            if bmi < 18.5:   bmi_label, bmi_tip = "偏輕 🔵", "建議增加蛋白質攝取，適當增重"
            elif bmi < 23:   bmi_label, bmi_tip = "正常 🟢", "保持現在的健康狀態！"
            elif bmi < 27.5: bmi_label, bmi_tip = "過重 🟡", "建議控制飲食，增加運動量"
            else:            bmi_label, bmi_tip = "肥胖 🔴", "建議諮詢醫生，制定減重計劃"
            # 理想體重範圍（BMI 18.5–23）
            ideal_low  = round(18.5 * (height_cm / 100) ** 2, 1)
            ideal_high = round(23.0 * (height_cm / 100) ** 2, 1)
            bmi_block = (
                f"📊 *BMI：{escape_md(f'{bmi:.1f}')}*  {escape_md(bmi_label)}\n"
                f"_💡 {escape_md(bmi_tip)}_\n\n"
                f"⚖️ 目前體重：{escape_md(str(latest_wt))} kg\n"
                f"🎯 理想體重：{escape_md(str(ideal_low))}–{escape_md(str(ideal_high))} kg\n"
            )
        elif height_cm and height_cm > 0:
            bmi_block = "_尚未記錄體重，無法計算 BMI_\n"
        else:
            bmi_block = "_尚未設定身高，無法計算 BMI_\n"

        height_str = f"{escape_md(str(height_cm))} cm" if height_cm else "未設定"
        await update.message.reply_text(
            f"📏 *身高 \\/ BMI*\n\n"
            f"身高：{height_str}\n\n"
            f"{bmi_block}\n"
            f"輸入數字可更新身高，例如：`175`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=SUB_KEYBOARD_PROFILE
        )
        context.user_data["state"] = STATE_AWAIT_HEIGHT
        context.user_data["profile_nav"] = True
        return

    if text == "🌾 設定碳水目標":
        context.user_data["state"] = STATE_AWAIT_CARB_GOAL
        current = await load_carb_goal()
        await update.message.reply_text(
            f"🌾 *設定每日碳水目標*\n\n"
            f"目前設定：{escape_md(str(current))} g/天\n\n"
            f"建議範圍：\n"
            f"• 減脂期：150–200g\n"
            f"• 維持期：200–300g\n"
            f"• 增肌期：300–400g\n\n"
            f"_你目前體重86kg，建議增肌期 320g_\n\n"
            f"請輸入目標值（g），例如：`250`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("150"), KeyboardButton("200")],
                 [KeyboardButton("250"), KeyboardButton("320")],
                 [KeyboardButton("🔙 返回設定")]],
                resize_keyboard=True))
        return

    if text == "🌿 設定纖維目標":
        context.user_data["state"] = STATE_AWAIT_FIBER_GOAL
        current = await load_fiber_goal()
        await update.message.reply_text(
            f"🌿 *設定每日纖維目標*\n\n"
            f"目前設定：{escape_md(str(current))} g/天\n\n"
            f"WHO 建議：\n"
            f"• 男性：25–38g/天\n"
            f"• 女性：21–25g/天\n\n"
            f"_膳食纖維幫助消化、控血糖、增加飽腹感_\n\n"
            f"請輸入目標值（g），例如：`30`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("25"), KeyboardButton("30")],
                 [KeyboardButton("35"), KeyboardButton("38")],
                 [KeyboardButton("🔙 返回設定")]],
                resize_keyboard=True))
        return

    if text == "📏 設定身高":
        context.user_data["state"] = STATE_AWAIT_HEIGHT
        current_h = await load_height()
        current_str = f"目前身高：{escape_md(str(current_h))} cm\n\n" if current_h else ""
        await update.message.reply_text(
            f"📏 *設定身高*\n\n"
            f"{current_str}"
            f"請輸入您的身高（cm），例如：`175`\n"
            f"_用於自動計算 BMI_",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    if text == "🎂 設定年齡/性別":
        current_age    = await load_age()
        current_gender = await load_gender()
        gender_str     = "男 👨" if current_gender == "male" else "女 👩"
        context.user_data["state"] = STATE_AWAIT_AGE_GENDER
        await update.message.reply_text(
            f"🎂 *設定年齡與性別*\n\n"
            f"目前設定：{escape_md(str(current_age))} 歲，{escape_md(gender_str)}\n\n"
            f"用於自動計算 BMR（基礎代謝率）\n\n"
            f"請輸入格式：`年齡 性別`\n"
            f"例如：`22 男` 或 `25 女`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("22 男"), KeyboardButton("25 男")],
                 [KeyboardButton("22 女"), KeyboardButton("25 女")],
                 [KeyboardButton("🔙 返回設定")]],
                resize_keyboard=True))
        return

    if text == "📋 查看目前設定":
        cal_goal   = await load_goal()
        burn_goal  = await load_burn_goal()
        prot_goal  = await load_protein_goal()
        step_goal  = await load_steps_goal()
        water_goal = await load_water_goal()
        wt_goal    = await load_weight_goal()
        height_cm  = await load_height()
        plan       = await load_muscle_plan()
        wt_str     = f"{escape_md(str(wt_goal))} kg" if wt_goal else "未設定"
        plan_str   = "、".join(f"{k}{v}次" for k, v in plan.items()) if plan else "未設定"

        # BMI 計算
        bmi_str = "未設定身高"
        if height_cm > 0:
            all_weights = await fetch_weight_rows()
            valid_wts   = [float(r[1]) for r in all_weights if len(r) > 1 and r[1] and float(r[1]) > 0]
            if valid_wts:
                latest_wt = valid_wts[-1]
                bmi       = latest_wt / ((height_cm / 100) ** 2)
                if bmi < 18.5:   bmi_label = "偏輕 🔵"
                elif bmi < 23:   bmi_label = "正常 🟢"
                elif bmi < 25:   bmi_label = "過重 🟡"
                elif bmi < 30:   bmi_label = "肥胖 🟠"
                else:            bmi_label = "嚴重肥胖 🔴"
                bmi_str = f"{escape_md(f'{bmi:.1f}')} {escape_md(bmi_label)}"
            else:
                bmi_str = f"身高 {escape_md(str(int(height_cm)))} cm（未有體重記錄）"

        await update.message.reply_text(
            f"📋 *目前所有設定*\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🎯 每日熱量攝取目標：{escape_md(str(cal_goal))} kcal\n"
            f"🔥 每日卡路里消耗目標：{escape_md(str(burn_goal))} kcal\n"
            f"🥩 每日蛋白質目標：{escape_md(str(prot_goal))} g\n"
            f"👟 每日步數目標：{escape_md(str(step_goal))} 步\n"
            f"💧 每日飲水目標：{escape_md(str(water_goal))} ml\n"
            f"⚖️ 目標體重：{wt_str}\n"
            f"📏 BMI：{bmi_str}\n"
            f"💪 訓練計劃：{escape_md(plan_str)}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"_點選上方按鈕可修改各項目標_",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if text == "📅 今日資料":
        await show_today(update, context); return
    if text == "📋 本週資料":
        await show_week(update, context); return
    if text == "📆 週報":
        await show_weekly_report(update, context); return
    if text == "🗓 月報":
        await show_monthly_report(update, context); return
    if text == "🏆 目標達成":
        await show_streak(update, context); return
    if text == "📊 體重趨勢" or text == "📈 體重趨勢":
        await show_weight_trend(update, context); return
    if text == "📊 每週熱量圖":
        await show_weekly_calorie_chart(update, context); return
    if text == "📉 BMI趨勢":
        await show_bmi_trend_chart(update, context); return
    if text == "📂 總資料":
        await show_all(update, context); return
    if text == "💪 本週訓練進度" or text == "💪 訓練部位統計":
        await show_muscle_progress(update, context); return
    if text == "📊 今日飲水":
        await show_water_today(update, context); return

    # ── 喝水快捷按鈕 ────────────────────────────────────────
    water_quick = {"💧 250ml": 250, "💧 500ml": 500, "💧 750ml": 750}
    if text in water_quick:
        ml = water_quick[text]
        total, was_updated = await upsert_water_row(ml)
        water_goal = await load_water_goal()
        bar = progress_bar(total, water_goal)
        status = "✅ 已達標\\!" if total >= water_goal else f"還需 {escape_md(str(water_goal - total))} ml"
        await update.message.reply_text(
            f"💧 *\\+{escape_md(str(ml))} ml 已記錄\\!*\n\n"
            f"今日總量　`{escape_md(bar)}`\n"
            f"{escape_md(str(total))} ／ {escape_md(str(water_goal))} ml  {status}",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_WATER)
        return
    if text == "💧 自訂ml":
        context.user_data["state"] = STATE_AWAIT_WATER
        await update.message.reply_text("💧 請輸入飲水量（ml），例如：`300`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # ── 設定訓練計劃 ─────────────────────────────────────────
    if text == "💪 設定訓練計劃":
        plan = await load_muscle_plan()
        plan_lines = []
        for cat in MUSCLE_CATEGORIES:
            emoji = MUSCLE_EMOJI.get(cat, "🏋️")
            goal  = plan.get(cat, 0)
            plan_lines.append(f"{emoji} {cat}：{goal} 次/週")
        context.user_data["state"] = STATE_AWAIT_MUSCLE_GOAL
        await update.message.reply_text(
            f"💪 *設定每週訓練計劃*\n\n"
            f"目前設定：\n" + "\n".join(plan_lines) + "\n\n"
            f"請輸入格式：`部位 次數` 每行一個，例如：\n"
            f"`腿 2`\n`胸 2`\n`背 2`\n`核心 3`\n`有氧 4`\n\n"
            f"_輸入的部位會更新，未輸入的保持不變。輸入 0 表示取消該部位_",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    # 狀態機
    if state == STATE_AWAIT_WEIGHT:
        try:
            weight = float(text)
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數字，例如：`70\\.5`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await append_weight_row(weight)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ *體重已記錄*\n\n⚖️  {escape_md(str(weight))} kg\n⏰ {escape_md(now_str())}",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_WEIGHT)
        return

    if state == STATE_AWAIT_GOAL:
        try:
            goal = int(float(text))
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數字，例如：`1800`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_goal(goal)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ *熱量目標已設定*\n\n🎯 每日攝取目標：{escape_md(str(goal))} kcal",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_BURN_GOAL:
        try:
            val = int(float(text))
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數字，例如：`500`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_burn_goal(val)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ *消耗目標已設定*\n\n🔥 每日消耗目標：{escape_md(str(val))} kcal",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_PROTEIN_GOAL:
        try:
            val = int(float(text))
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數字，例如：`80`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_protein_goal(val)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ *蛋白質目標已設定*\n\n🥩 每日蛋白質目標：{escape_md(str(val))} g",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_STEPS_GOAL:
        try:
            val = int(float(text))
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數字，例如：`8000`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_steps_goal(val)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ *步數目標已設定*\n\n👟 每日步數目標：{escape_md(str(val))} 步",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_WATER:
        try:
            ml = int(float(text))
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數字，例如：`300`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        total, _ = await upsert_water_row(ml)
        water_goal = await load_water_goal()
        bar = progress_bar(total, water_goal)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"💧 *\\+{escape_md(str(ml))} ml 已記錄\\!*\n\n"
            f"今日總量　`{escape_md(bar)}`\n"
            f"{escape_md(str(total))} ／ {escape_md(str(water_goal))} ml",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_WATER)
        return

    if state == STATE_AWAIT_WATER_GOAL:
        try:
            val = int(float(text))
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數字，例如：`2000`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_water_goal(val)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ *飲水目標已設定*\n\n💧 每日飲水目標：{escape_md(str(val))} ml",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_HEIGHT:
        try:
            h = float(text)
            if not (100 < h < 250):
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效身高（cm），例如：`175`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_height(h)
        context.user_data["state"] = STATE_IDLE
        all_weights = await fetch_weight_rows()
        valid_wts   = [float(r[1]) for r in all_weights if len(r) > 1 and r[1] and float(r[1]) > 0]
        bmi_line = ""
        if valid_wts:
            bmi = valid_wts[-1] / ((h / 100) ** 2)
            if bmi < 18.5:   bmi_label = "偏輕 🔵"
            elif bmi < 23:   bmi_label = "正常 🟢"
            elif bmi < 27.5: bmi_label = "過重 🟡"
            else:            bmi_label = "肥胖 🔴"
            ideal_low  = round(18.5 * (h / 100) ** 2, 1)
            ideal_high = round(23.0 * (h / 100) ** 2, 1)
            bmi_line = (
                f"\n📊 BMI：{escape_md(f'{bmi:.1f}')} {escape_md(bmi_label)}"
                f"\n🎯 理想體重：{escape_md(str(ideal_low))}–{escape_md(str(ideal_high))} kg"
            )
        # 從個人資料進來就返回個人資料，從設定進來就返回設定
        from_profile = context.user_data.get("profile_nav", False)
        back_kb = SUB_KEYBOARD_PROFILE if from_profile else SUB_KEYBOARD_SETTINGS
        await update.message.reply_text(
            f"✅ *身高已設定*\n\n📏 身高：{escape_md(str(int(h)))} cm{bmi_line}",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb)
        return

    if state == STATE_AWAIT_AGE_GENDER:
        import re
        m = re.match(r'(\d+)\s*([男女MmFf])', text.strip())
        if not m:
            await update.message.reply_text(
                "❌ 格式錯誤，請輸入如：`22 男` 或 `25 女`",
                parse_mode=ParseMode.MARKDOWN_V2)
            return
        age    = int(m.group(1))
        gender = "male" if m.group(2) in ["男", "M", "m"] else "female"
        if not (10 < age < 120):
            await update.message.reply_text("❌ 請輸入有效年齡（10–120）", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_age(age)
        await save_gender(gender)
        context.user_data["state"] = STATE_IDLE
        bmr = await calc_bmr()
        gender_str = "男 👨" if gender == "male" else "女 👩"
        bmr_line   = f"\n\n🧬 您的 BMR：*{escape_md(str(int(bmr)))} kcal/天*\n_\\(每天躺著不動也會消耗的熱量\\)_" if bmr > 0 else ""
        await update.message.reply_text(
            f"✅ *年齡/性別已設定*\n\n"
            f"🎂 年齡：{escape_md(str(age))} 歲\n"
            f"👤 性別：{escape_md(gender_str)}"
            f"{bmr_line}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_COACH_Q:
        context.user_data["state"] = STATE_IDLE
        thinking_msg = await update.message.reply_text("🤖 AI教練分析中\\.\\.\\.⏳", parse_mode=ParseMode.MARKDOWN_V2)
        ctx = await get_coach_context()
        answer = await ask_ai_coach(text, ctx)
        await thinking_msg.delete()
        await update.message.reply_text(
            f"🤖 *AI教練建議*\n\n{escape_md(answer)}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=SUB_KEYBOARD_COACH)
        return

    if state == STATE_AWAIT_CARB_GOAL:
        try:
            val = int(float(text.strip()))
            if not (50 <= val <= 600):
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數值（50–600 g）", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_carb_goal(val)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ *碳水目標已設定*\n\n🌾 每日碳水目標：*{escape_md(str(val))} g*",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_FIBER_GOAL:
        try:
            val = int(float(text.strip()))
            if not (10 <= val <= 100):
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數值（10–100 g）", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_fiber_goal(val)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ *纖維目標已設定*\n\n🌿 每日纖維目標：*{escape_md(str(val))} g*",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_MUSCLE_GOAL:
        # 解析多行輸入：每行「部位 次數」
        plan = await load_muscle_plan()
        lines_input = text.strip().split("\n")
        updated = []
        errors  = []
        for line in lines_input:
            parts = line.strip().split()
            if len(parts) >= 2:
                cat   = parts[0]
                try:
                    count = int(parts[1])
                except ValueError:
                    errors.append(line)
                    continue
                if cat in MUSCLE_CATEGORIES:
                    if count == 0:
                        plan.pop(cat, None)
                    else:
                        plan[cat] = count
                    updated.append(f"{MUSCLE_EMOJI.get(cat,'🏋️')} {cat}：{count} 次/週")
                else:
                    errors.append(f"{cat}（不在支援部位清單）")
            else:
                errors.append(line)
        if updated:
            await save_muscle_plan(plan)
        context.user_data["state"] = STATE_IDLE
        reply = "✅ *訓練計劃已更新\\!*\n\n" + "\n".join(escape_md(u) for u in updated)
        if errors:
            reply += f"\n\n⚠️ 無法識別：{escape_md(', '.join(errors))}"
        reply += f"\n\n_支援部位：{escape_md('、'.join(MUSCLE_CATEGORIES))}_"
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_WEIGHT_GOAL:
        try:
            wg = float(text)
        except ValueError:
            await update.message.reply_text("❌ 請輸入有效數字，例如：`70.0`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        await save_weight_goal(wg)
        context.user_data["state"] = STATE_IDLE
        await update.message.reply_text(
            f"✅ *體重目標已設定*\n\n⚖️ 目標體重：{escape_md(str(wg))} kg\n"
            f"_每日推送將顯示您距目標的進度_",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SETTINGS)
        return

    if state == STATE_AWAIT_RECIPE_NUM:
        try:
            num = int(text.strip())
            cat = context.user_data.get("recipe_cat", "增肌")
            detail = get_recipe_detail(cat, num)
            await update.message.reply_text(detail, parse_mode=ParseMode.MARKDOWN_V2,
                                            reply_markup=SUB_KEYBOARD_RECIPES)
        except ValueError:
            pass  # 不是數字就忽略，讓用戶繼續操作
        return

    if state == STATE_AWAIT_EXERCISE:
        await process_exercise(update, context, text_desc=text); return

    if state == STATE_AWAIT_ACTIVITY:
        await process_activity(update, context, text); return

    if state == STATE_AWAIT_SLEEP:
        import re as _rsl
        time_m = _rsl.findall(r'(\d{1,2}:\d{2})', text)
        deep_m = _rsl.search(r'深[睡眠]?\s*(\d+)', text)
        rem_m  = _rsl.search(r'(?i)rem\s*(\d+)', text)
        tot_m  = _rsl.search(r'(\d+)\s*(?:分|min)', text)
        if len(time_m) >= 2 or tot_m:
            bed, wake = (time_m[0], time_m[1]) if len(time_m) >= 2 else ("", "")
            if not tot_m and bed and wake:
                bh, bm = map(int, bed.split(":")); wh, wm = map(int, wake.split(":"))
                total = (wh*60+wm) - (bh*60+bm)
                if total < 0: total += 1440
            else:
                total = int(tot_m.group(1)) if tot_m else 0
            data = {
                "date": today_str(), "bed_time": bed, "wake_time": wake,
                "total_min": total,
                "deep_min":  int(deep_m.group(1)) if deep_m else 0,
                "light_min": 0, "rem_min": int(rem_m.group(1)) if rem_m else 0,
                "awake_min": 0, "score": 0, "notes": "手動輸入",
            }
            await append_sleep_row(data)
            context.user_data["state"] = STATE_IDLE
            display = ["", data["date"], data["bed_time"], data["wake_time"],
                       data["total_min"], data["deep_min"], 0, data["rem_min"], 0, 0, "手動"]
            await update.message.reply_text(
                f"✅ *睡眠記錄成功！*\n\n{format_sleep_summary(display)}",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_SLEEP)
        else:
            await update.message.reply_text(
                "📷 請傳送截圖，或用文字格式輸入：\n"
                "`23:30 7:00 深睡90 REM60`\n"
                "_（入睡時間 起床時間 深睡分鐘 REM分鐘）_",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=ReplyKeyboardMarkup(
                    [[KeyboardButton("🔙 返回主選單")]], resize_keyboard=True))
        return

    if state == STATE_AWAIT_FOOD:
        # ── 份量調整解析：支援「半碗白飯」「2個水煮蛋」「1.5份雞胸肉飯」──
        import re as _re
        portion_match = _re.match(
            r"^(半|[\d\.]+(?:\/\d+)?)\s*(?:碗|份|個|片|塊|條|顆|杯|罐|勺)?\s*(.+)$",
            text.strip()
        )
        if portion_match:
            qty_str  = portion_match.group(1)
            food_str = portion_match.group(2).strip()
            qty = 0.5 if qty_str == "半" else float(eval(qty_str))
            # 把份量和食物名稱一起傳給 AI，讓它知道份量
            desc = f"{qty_str}份 {food_str}" if qty != 1 else food_str
            context.user_data["food_portion"] = qty
            await process_food_text(update, context, text_desc=desc, image_bytes=None)
        else:
            context.user_data.pop("food_portion", None)
            await process_food_text(update, context, text_desc=text, image_bytes=None)
        return

    await update.message.reply_text(
        "請使用下方選單選擇功能，或輸入 /start 重置\\.",
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=MAIN_KEYBOARD)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state   = context.user_data.get("state", STATE_IDLE)
    caption = update.message.caption or ""

    photo_file         = await update.message.photo[-1].get_file()
    photo_bytes_arr    = await photo_file.download_as_bytearray()
    image_bytes: bytes = bytes(photo_bytes_arr)

    if state == STATE_AWAIT_BODY:
        await process_body_photo(update, context, image_bytes)
    elif state == STATE_AWAIT_EXERCISE:
        await process_exercise(update, context, text_desc=caption or None, image_bytes=image_bytes)
    elif state == STATE_AWAIT_ACTIVITY:
        # 活動截圖識別
        await process_activity_image(update, context, image_bytes)
    elif state == STATE_AWAIT_SLEEP:
        await process_sleep_image(update, context, image_bytes)
    else:
        cap_lower = caption.lower()
        sleep_kw    = ["睡眠", "sleep", "深睡", "淺睡", "rem", "入睡", "起床", "zepp", "mi fitness"]
        activity_kw = ["步", "步數", "消耗", "卡路里", "步行", "手環", "garmin", "xiaomi",
                       "samsung", "apple", "fitbit", "google fit", "健康", "activity"]
        if any(kw in cap_lower for kw in sleep_kw):
            await process_sleep_image(update, context, image_bytes)
        elif any(kw in cap_lower for kw in activity_kw):
            await process_activity_image(update, context, image_bytes)
        else:
            context.user_data["state"] = STATE_AWAIT_FOOD
            await process_food_text(update, context, text_desc=caption or None, image_bytes=image_bytes)


async def process_activity_image(update, context, image_bytes: bytes):
    """用 AI 識別手環截圖，自動提取步數和消耗並記錄"""
    thinking = await update.message.reply_text(
        "📱 *AI 正在識別活動數據\\.\\.\\.* ⏳", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        result = await analyze_activity_image(image_bytes)
    except Exception as e:
        logger.error("活動截圖識別失敗：%s", e)
        await thinking.edit_text("❌ 無法識別截圖，請手動輸入數據。", parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data["state"] = STATE_AWAIT_ACTIVITY
        return

    steps   = int(result.get("steps", 0))
    burn    = int(result.get("calories_burned", 0))
    source  = result.get("source", "手環")

    if steps == 0 and burn == 0:
        await thinking.edit_text(
            "❌ 無法從截圖中識別數據，請確認截圖包含步數或卡路里資訊，或改為手動輸入。",
            parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data["state"] = STATE_AWAIT_ACTIVITY
        return

    # 寫入資料庫
    data = {"steps": steps if steps else None, "calories_burned": burn if burn else None,
            "heart_points": None, "active_minutes": None}
    try:
        was_updated = await upsert_activity_row(data)
    except Exception as e:
        logger.error("活動數據寫入失敗：%s", e)
        await thinking.edit_text("❌ 資料庫寫入失敗。", parse_mode=ParseMode.MARKDOWN_V2)
        return

    context.user_data["state"] = STATE_IDLE
    status = "✅ *活動數據已更新\\!*" if was_updated else "✅ *活動數據已記錄\\!*"
    note   = "\n_（今日原有數據已覆蓋）_" if was_updated else ""

    # BMR 顯示
    bmr = await calc_bmr()
    burn_lines = ""
    if burn > 0:
        if bmr > 0:
            total = burn + int(bmr)
            burn_lines = (
                f"🔥 活動消耗：*{escape_md(str(burn))} kcal*\n"
                f"🧬 基礎代謝：{escape_md(str(int(bmr)))} kcal\n"
                f"📊 真實總消耗：*{escape_md(str(total))} kcal*\n"
            )
        else:
            burn_lines = f"🔥 消耗：*{escape_md(str(burn))} kcal*\n"

    lines = [f"{status}{note}\n",
             f"📱 來源：{escape_md(source)}"]
    if steps: lines.append(f"👟 步數：*{escape_md(str(steps))}*")
    if burn_lines: lines.append(burn_lines.rstrip())
    lines.append(f"⏰ {escape_md(now_str())}")

    await thinking.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ 撤銷此筆", callback_data="undo_last")
        ]])
    )
    await update.message.reply_text("繼續或選擇其他：", reply_markup=SUB_KEYBOARD_EXERCISE)


# ════════════════════════════════════════════════════════════
#  核心處理函式
# ════════════════════════════════════════════════════════════

async def process_food_text(update, context, text_desc, image_bytes):
    # ── 份量修飾語解析（例：「半碗飯」「2份雞胸肉」「1.5倍」）──
    portion_multiplier = 1.0
    cleaned_desc = text_desc or ""
    if text_desc and not image_bytes:
        import re
        # 中文份量詞：半/0.5、一/1、兩/二/2、三/3 等 + 碗/份/杯/盒/條/個/片...
        zh_frac = {"半": 0.5, "一": 1.0, "兩": 2.0, "二": 2.0, "三": 3.0, "四": 4.0, "五": 5.0}
        # 例：「半碗飯」「兩份雞胸肉」
        m = re.match(r'^([半一兩二三四五])([碗份杯盒條個片包罐匙顆])(.*)', cleaned_desc)
        if m:
            portion_multiplier = zh_frac.get(m.group(1), 1.0)
            cleaned_desc       = m.group(3).strip() or text_desc
        else:
            # 例：「2份雞胸肉」「1.5碗飯」「0.5份」
            m2 = re.match(r'^(\d+\.?\d*)\s*([碗份杯盒條個片包罐匙顆x×倍])?\s*(.*)', cleaned_desc)
            if m2 and m2.group(3):
                try:
                    portion_multiplier = float(m2.group(1))
                    cleaned_desc       = m2.group(3).strip()
                except ValueError:
                    pass
        if portion_multiplier != 1.0:
            logger.info("份量修飾：%.1fx，分析食物：%s", portion_multiplier, cleaned_desc)

    # ── 純文字且無圖片時，先查快取避免呼叫 AI ──────────────
    data = None
    if cleaned_desc and not image_bytes:
        cached = await get_food_nutrition_cache(cleaned_desc.strip())
        if cached:
            data = dict(cached)  # 複製避免改到快取
            status_prefix = "⚡ *快速記錄（歷史數據）*"
        else:
            status_prefix = "✅ *飲食已記錄\\!*"
    else:
        status_prefix = "✅ *飲食已記錄\\!*"

    if data is None:
        msg = await update.message.reply_text("🤖 AI 正在分析食物\\.\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
        try:
            raw = await analyze_food(image_bytes=image_bytes, text_desc=cleaned_desc or text_desc)
        except Exception as e:
            logger.error("食物分析失敗：%s", e)
            await msg.edit_text("❌ AI 分析失敗，請重試\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        # ── AI 回傳多食物 list → 逐筆寫入 ──────────────────
        if isinstance(raw, list):
            saved, lines_out = 0, []
            for item in raw:
                if not isinstance(item, dict): continue
                try:
                    await append_diet_row(item)
                    saved += 1
                    name = escape_md(str(item.get("food_name", "未知")))
                    kcal = item.get("calories", 0)
                    prot = item.get("protein", 0)
                    raw_name = str(item.get("food_name", "未知"))
                    kcal_raw = item.get("calories", 0)
                    prot_raw = item.get("protein", 0)
                    lines_out.append(f"• {raw_name}：{kcal_raw} kcal | 蛋白 {prot_raw}g")
                except Exception as we:
                    logger.error("多項食物寫入失敗：%s", we)
            today_diet   = await fetch_diet_rows(filter_date=today_str())
            today_in     = sum(float(r[2]) for r in today_diet if len(r) > 2 and r[2])
            today_prot   = sum(float(r[3]) for r in today_diet if len(r) > 3 and r[3])
            goal_kcal    = await load_goal()
            protein_goal = await load_protein_goal()
            bar_kcal     = progress_bar(today_in, goal_kcal)
            bar_prot     = progress_bar(today_prot, protein_goal)
            detail = "\n".join(lines_out)
            # 熱量狀態（純文字，不用 MarkdownV2 避免特殊字符問題）
            if today_in < 2000:
                kcal_status = f"🟡 距目標還差 {round(2000 - today_in)} kcal"
            elif today_in <= 2200:
                kcal_status = "✅ 剛剛好！在目標區間"
            else:
                kcal_status = f"🔴 超出 +{round(today_in - 2200)} kcal"
            recap = (
                f"✅ 已記錄 {saved} 項食物！\n\n"
                f"{detail}\n\n"
                f"────────────────\n"
                f"🔥 熱量：{round(today_in)} / {goal_kcal} kcal\n"
                f"{bar_kcal}\n"
                f"{kcal_status}\n"
                f"🥩 蛋白：{round(today_prot)} / {protein_goal} g\n"
                f"{bar_prot}"
            )
            await msg.edit_text("✅ 記錄完成！")
            await update.message.reply_text(recap, reply_markup=SUB_KEYBOARD_FOOD)
            context.user_data["state"] = STATE_IDLE
            return
        data = raw
    else:
        msg = await update.message.reply_text("⚡ *快速記錄中\\.\\.\\.\\.*", parse_mode=ParseMode.MARKDOWN_V2)

    # ── 套用份量乘數 ─────────────────────────────────────────
    if portion_multiplier != 1.0:
        for field in ("calories", "protein", "fiber", "carbs", "fat"):
            if field in data and data[field]:
                data[field] = round(float(data[field]) * portion_multiplier, 1)
        orig_name = data.get("food_name", "")
        frac_str  = {0.5: "半份", 2.0: "2份", 3.0: "3份"}.get(portion_multiplier, f"{portion_multiplier}份")
        data["food_name"] = f"{orig_name}（{frac_str}）"
        status_prefix = f"✅ *飲食已記錄（{escape_md(frac_str)}）\\!*"

    try:
        await append_diet_row(data)
    except Exception as e:
        logger.error("Sheets 寫入失敗：%s", e)
        await msg.edit_text("❌ 資料庫寫入失敗\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    goal_kcal    = await load_goal()
    protein_goal = await load_protein_goal()
    today_diet   = await fetch_diet_rows(filter_date=today_str())
    today_in     = sum(float(r[2]) for r in today_diet if len(r) > 2 and r[2])
    today_prot   = sum(float(r[3]) for r in today_diet if len(r) > 3 and r[3])
    bar_kcal     = progress_bar(today_in, goal_kcal)
    bar_prot     = progress_bar(today_prot, protein_goal)
    carb_goal    = await load_carb_goal()
    fiber_goal   = await load_fiber_goal()
    today_carb   = sum(float(r[4]) for r in today_diet if len(r) > 4 and r[4])
    today_fiber  = sum(float(r[5]) for r in today_diet if len(r) > 5 and r[5])
    bar_carb     = progress_bar(today_carb, carb_goal)
    bar_fiber    = progress_bar(today_fiber, fiber_goal)
    context.user_data["state"] = STATE_IDLE

    # 單筆食物：顯示碳水和纖維（若為0則不顯示）
    nutrition_line = (
        f"🔥 {escape_md(str(data.get('calories', 0)))} kcal \\| "
        f"🥩 {escape_md(str(data.get('protein', 0)))}g"
    )
    if data.get('carbs', 0):
        nutrition_line += f" \\| 🌾 {escape_md(str(data.get('carbs', 0)))}g"
    if data.get('fiber', 0):
        nutrition_line += f" \\| 🌿 {escape_md(str(data.get('fiber', 0)))}g"

    await msg.edit_text(
        f"{status_prefix}\n\n"
        f"🍽 {escape_md(data.get('food_name', '未知'))}\n"
        f"{nutrition_line}\n"
        f"⏰ {escape_md(now_str())}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🍽 今日攝取　`{escape_md(bar_kcal)}`\n"
        f"   {escape_md(str(int(today_in)))} ／ {escape_md(str(goal_kcal))} kcal\n"
        f"🥩 蛋白質　`{escape_md(bar_prot)}`\n"
        f"   {escape_md(f'{today_prot:.0f}')} ／ {escape_md(str(protein_goal))} g\n"
        f"🌾 碳水　`{escape_md(bar_carb)}`\n"
        f"   {escape_md(f'{today_carb:.0f}')} ／ {escape_md(str(carb_goal))} g\n"
        f"🌿 纖維　`{escape_md(bar_fiber)}`\n"
        f"   {escape_md(f'{today_fiber:.0f}')} ／ {escape_md(str(fiber_goal))} g",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ 撤銷此筆", callback_data="undo_last")
        ]])
    )
    # ── 記錄後顯示快速繼續鍵盤 ───────────────────────────────
    frequent = await load_frequent_foods(top_n=6)

    # 常用食物兩個一排
    buttons = []
    if frequent:
        pairs = [frequent[i:i+2] for i in range(0, len(frequent), 2)]
        for pair in pairs:
            buttons.append([KeyboardButton(f"🔁 {f}") for f in pair])

    # 固定底部按鈕
    buttons.append([KeyboardButton("➕ 輸入新食物"), KeyboardButton("💡 今日飲食建議")])
    buttons.append([KeyboardButton("🔙 返回主選單")])
    quick_kb = ReplyKeyboardMarkup(buttons, resize_keyboard=True)

    header = "🔁 *繼續記錄食物*\n\n" if frequent else "➕ *繼續記錄*\n\n"
    tips   = "_點選常用食物快速記錄，或輸入新食物_" if frequent else "_輸入食物名稱或上傳照片_"
    await update.message.reply_text(
        f"{header}{tips}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=quick_kb
    )
    context.user_data["in_food_quick"] = True


async def process_exercise(update, context, text_desc: str | None, image_bytes: bytes | None = None):
    msg = await update.message.reply_text("🤖 AI 正在分析訓練內容\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        data = await analyze_exercise(text_desc=text_desc, image_bytes=image_bytes)
    except Exception as e:
        logger.error("運動分析失敗：%s", e)
        await msg.edit_text("❌ AI 分析失敗，請重試\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    try:
        await append_exercise_row(data)
    except Exception as e:
        logger.error("Sheets 寫入失敗：%s", e)
        await msg.edit_text("❌ 資料庫寫入失敗\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # 組裝肌肉群顯示
    muscles    = data.get("muscles", [])
    movements  = data.get("movements", [])
    category   = data.get("muscle_category", "")
    categories = data.get("muscle_categories", [category]) if category else data.get("muscle_categories", [])

    CATEGORY_EMOJI = {
        "胸": "🫁", "背": "🔙", "肩": "🔺", "手臂": "💪",
        "核心": "⚡", "腿": "🦵", "全身": "🏋️", "有氧": "🏃", "柔韌": "🧘"
    }

    # 多部位顯示
    if len(categories) > 1:
        cats_str = "、".join(f"{CATEGORY_EMOJI.get(c,'🏋️')}{c}" for c in categories)
    else:
        cat_emoji = CATEGORY_EMOJI.get(category, "🏋️")
        cats_str  = f"{cat_emoji}{category}"

    muscle_line   = escape_md("、".join(muscles))   if muscles   else "\\-"
    movement_line = escape_md("、".join(movements)) if movements else "\\-"

    await msg.edit_text(
        f"✅ *運動已記錄\\!*\n\n"
        f"🏋️ *{escape_md(data.get('exercise_name', '未知'))}*\n"
        f"⏱ 時長：{escape_md(str(data.get('duration_min', 0)))} 分鐘\n"
        f"🎯 部位分類：{escape_md(cats_str)}\n"
        f"💪 訓練肌群：{muscle_line}\n"
        f"📋 動作：{movement_line}\n"
        f"⏰ {escape_md(now_str())}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ 撤銷此筆", callback_data="undo_last")
        ]])
    )
    await update.message.reply_text(
        "✅ 已記錄！可繼續發送更多運動照片或文字，完成後點選其他功能：",
        reply_markup=SUB_KEYBOARD_EXERCISE)


async def process_activity(update, context, text: str):
    """處理手動輸入的活動數據（步數、消耗、心率點、活躍分鐘）。同日自動覆蓋更新。"""
    data = parse_activity_input(text)

    if all(v is None for v in [data["steps"], data["calories_burned"],
                                data["heart_points"], data["active_minutes"]]):
        await update.message.reply_text(
            "❌ 無法識別數據，請按格式輸入，例如：\n`步數8000 消耗350 心率點12 活動點30`",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        was_updated = await upsert_activity_row(data)
    except Exception as e:
        logger.error("活動數據寫入失敗：%s", e)
        await update.message.reply_text("❌ 資料庫寫入失敗\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    context.user_data["state"] = STATE_IDLE

    status = "✅ *今日活動數據已更新\\!*" if was_updated else "✅ *活動數據已記錄\\!*"
    note   = "\n_（今日原有數據已覆蓋）_" if was_updated else ""

    lines = [f"{status}{note}\n"]
    if data["steps"]           is not None: lines.append(f"👟 步數：{escape_md(str(data['steps']))}")
    if data["calories_burned"] is not None:
        act_burn = data["calories_burned"]
        bmr      = await calc_bmr()
        if bmr > 0:
            total_burn = int(act_burn + bmr)
            lines.append(
                f"🔥 活動消耗：{escape_md(str(act_burn))} kcal\n"
                f"🧬 基礎代謝：{escape_md(str(int(bmr)))} kcal\n"
                f"📊 *真實總消耗：{escape_md(str(total_burn))} kcal*"
            )
        else:
            lines.append(f"🔥 消耗：{escape_md(str(act_burn))} kcal\n_\\(設定身高/體重/年齡後可自動加入BMR\\)_")
    if data["heart_points"]   is not None: lines.append(f"❤️ Heart Points：{escape_md(str(data['heart_points']))}")
    if data["active_minutes"] is not None: lines.append(f"⏱ 活躍分鐘：{escape_md(str(data['active_minutes']))}")
    lines.append(f"⏰ {escape_md(now_str())}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ 撤銷此筆", callback_data="undo_last")
        ]])
    )
    await update.message.reply_text("繼續或選擇其他：", reply_markup=SUB_KEYBOARD_ACTIVITY)


async def process_body_photo(update, context, image_bytes: bytes):
    msg = await update.message.reply_text("🤖 AI 正在分析體態\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)

    # 取上一次身材照的評估內容做對比
    prev_assessment = None
    try:
        all_weights = await fetch_weight_rows()
        body_notes  = [r[2] for r in all_weights if len(r) > 2 and r[2].startswith("[身材照]")]
        if body_notes:
            prev_assessment = body_notes[-1].replace("[身材照] ", "")
    except Exception:
        pass

    try:
        data = await analyze_body_photo(image_bytes, prev_assessment=prev_assessment)
    except Exception as e:
        logger.error("身材分析失敗：%s", e)
        await msg.edit_text("❌ AI 分析失敗，請重試\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    try:
        await append_weight_row(0, note=f"[身材照] {data.get('assessment', '')[:80]}")
    except Exception as e:
        logger.warning("身材照記錄寫入失敗：%s", e)

    comparison = data.get("comparison", "")
    comp_line  = f"\n📊 *對比上次：* {escape_md(comparison)}\n" if comparison and comparison != "（首次記錄）" else "\n_（首次記錄，下次上傳將自動對比）_\n"

    context.user_data["state"] = STATE_IDLE
    await msg.edit_text(
        f"📸 *身材評估報告*\n\n"
        f"🏋️ *評估：*\n{escape_md(data.get('assessment', 'N/A'))}\n"
        f"{comp_line}\n"
        f"💡 *建議：*\n{escape_md(data.get('suggestions', 'N/A'))}\n\n"
        f"⏰ {escape_md(now_str())}",
        parse_mode=ParseMode.MARKDOWN_V2)
    await update.message.reply_text("繼續上傳或選擇其他：", reply_markup=SUB_KEYBOARD_PROFILE)


# ════════════════════════════════════════════════════════════
#  數據查詢
# ════════════════════════════════════════════════════════════

async def show_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    td            = today_str()
    diet_rows     = await fetch_diet_rows(filter_date=td)
    exercise_rows = await fetch_exercise_rows(filter_date=td)
    weight_rows   = await fetch_weight_rows(filter_date=td)
    activity_rows = await fetch_activity_rows(filter_date=td)
    goal_kcal     = await load_goal()
    protein_goal  = await load_protein_goal()
    carb_goal     = await load_carb_goal()
    fiber_goal    = await load_fiber_goal()
    steps_goal    = await load_steps_goal()
    burn_goal     = await load_burn_goal()
    water_goal    = await load_water_goal()
    water_ml      = await fetch_water_today()

    total_in    = sum(float(r[2]) for r in diet_rows     if len(r) > 2 and r[2])
    total_burn  = sum(float(r[2]) for r in activity_rows if len(r) > 2 and r[2])
    total_steps = sum(float(r[1]) for r in activity_rows if len(r) > 1 and r[1])
    bmr         = await calc_bmr()

    # 第1條：飲食 + 熱量總覽 + 飲水
    msg1 = build_today_msg1(
        diet_rows, goal_kcal, protein_goal, total_in, total_burn, td,
        water_ml=water_ml, water_goal=water_goal, bmr=bmr,
        carb_goal=carb_goal, fiber_goal=fiber_goal
    )
    await update.message.reply_text(msg1, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_VIEW)

    # 第2條：運動 + 活動 + 體重
    msg2 = build_today_msg2(
        exercise_rows, activity_rows, weight_rows,
        total_burn, burn_goal, total_steps, steps_goal, td
    )
    await update.message.reply_text(msg2, parse_mode=ParseMode.MARKDOWN_V2)

    # 第3條：今日進度圓形圖
    protein_goal = await load_protein_goal()
    water_ml     = await fetch_water_today()
    water_goal_v = await load_water_goal()
    burn_goal_v  = await load_burn_goal()
    total_prot   = sum(float(r[3]) for r in diet_rows if len(r) > 3 and r[3])
    chart_bytes  = await generate_daily_progress_chart(
        total_in, goal_kcal,
        total_prot, protein_goal,
        total_burn, burn_goal_v,
        water_ml, water_goal_v
    )
    if chart_bytes:
        import io as _io
        await update.message.reply_photo(
            photo=_io.BytesIO(chart_bytes),
            caption="📊 Today's Progress"
        )


async def show_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today      = datetime.now().date()
    week_dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    goal_kcal  = await load_goal()

    diet_rows     = await fetch_diet_rows()
    exercise_rows = await fetch_exercise_rows()
    weight_rows   = await fetch_weight_rows()
    activity_rows = await fetch_activity_rows()

    diet_week     = [r for r in diet_rows     if r and any(r[0].startswith(d) for d in week_dates)]
    exercise_week = [r for r in exercise_rows if r and any(r[0].startswith(d) for d in week_dates)]
    weight_week   = [r for r in weight_rows   if r and any(r[0].startswith(d) for d in week_dates)]
    activity_week = [r for r in activity_rows if r and any(r[0].startswith(d) for d in week_dates)]

    await update.message.reply_text(
        format_diet_report(diet_week, "📊 本週飲食", goal_kcal * 7),
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_VIEW)
    if exercise_week:
        await update.message.reply_text(
            format_exercise_report(exercise_week, "🏃 本週運動"),
            parse_mode=ParseMode.MARKDOWN_V2)
    if activity_week:
        await update.message.reply_text(
            format_activity_report(activity_week, "👟 本週活動數據"),
            parse_mode=ParseMode.MARKDOWN_V2)
    if weight_week:
        await update.message.reply_text(
            format_weight_report(weight_week, "⚖️ 本週體重"),
            parse_mode=ParseMode.MARKDOWN_V2)


async def show_week_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """本週運動總覽：按部位分類 + 每日紀錄 + AI 週訓練總結（一次 API）"""
    msg   = await update.message.reply_text("📊 正在整理本週訓練記錄\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    today = datetime.now().date()
    week_dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    week_start = week_dates[-1]
    week_end   = week_dates[0]

    exercise_rows = await fetch_exercise_rows()
    ew = [r for r in exercise_rows if r and any(r[0].startswith(d) for d in week_dates)]

    if not ew:
        await msg.edit_text("📭 本週尚無訓練記錄\\.", parse_mode=ParseMode.MARKDOWN_V2)
        await update.message.reply_text("選擇其他功能：", reply_markup=SUB_KEYBOARD_EXERCISE)
        return

    from collections import defaultdict
    CATEGORY_ORDER = ["胸", "背", "肩", "手臂", "核心", "腿", "全身", "有氧", "柔韌"]
    CATEGORY_EMOJI = {
        "胸": "🫁", "背": "🔙", "肩": "🔺", "手臂": "💪",
        "核心": "⚡", "腿": "🦵", "全身": "🏋️", "有氧": "🏃", "柔韌": "🧘"
    }

    by_category: dict[str, list[dict]] = defaultdict(list)
    daily_sessions: dict[str, list[str]] = defaultdict(list)
    total_duration = 0

    for r in ew:
        ts       = r[0] if len(r) > 0 else ""
        name     = r[1] if len(r) > 1 else "未知"
        dur      = r[2] if len(r) > 2 else "0"
        category = r[3] if len(r) > 3 else "其他"
        muscles  = r[4] if len(r) > 4 else ""
        day      = ts[:10]
        try:
            total_duration += int(float(dur))
        except Exception:
            pass
        by_category[category].append({"name": name, "dur": dur, "muscles": muscles, "day": day})
        daily_sessions[day].append(f"{name} {dur}min")

    total_sessions = len(ew)

    lines = [
        f"🗓 *本週訓練總覽*",
        f"_{escape_md(week_start)} \\~ {escape_md(week_end)}_\n",
        f"📌 共 *{escape_md(str(total_sessions))}* 次　⏱ *{escape_md(str(total_duration))}* 分鐘\n",
        "━━━━━━━━━━━━━━━━",
    ]

    present_cats = [c for c in CATEGORY_ORDER if c in by_category]
    other_cats   = [c for c in by_category if c not in CATEGORY_ORDER]

    for cat in present_cats + other_cats:
        emoji    = CATEGORY_EMOJI.get(cat, "🏋️")
        sessions = by_category[cat]
        count    = len(sessions)
        bar      = "▮" * count + "▯" * max(0, 4 - count)
        lines.append(f"\n{emoji} *{escape_md(cat)}*　{escape_md(bar)} {escape_md(str(count))}次")
        for s in sessions:
            day_short   = s["day"][5:]
            muscles_str = f"（{escape_md(s['muscles'])}）" if s["muscles"] else ""
            lines.append(f"  • {escape_md(s['name'])} {escape_md(s['dur'])}min　{escape_md(day_short)}{muscles_str}")

    lines.append("\n━━━━━━━━━━━━━━━━\n📅 *每日紀錄*")
    for day in sorted(daily_sessions.keys(), reverse=True):
        try:
            day_label = datetime.strptime(day, "%Y-%m-%d").strftime("%m/%d")
        except Exception:
            day_label = day
        items = escape_md(" ／ ".join(daily_sessions[day]))
        lines.append(f"  {escape_md(day_label)}　{items}")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    ai_msg = await update.message.reply_text("🤖 AI 正在生成訓練建議\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        cat_summary = "、".join(f"{c}×{len(by_category[c])}" for c in present_cats + other_cats)
        all_names   = "、".join(r[1] for r in ew if len(r) > 1)
        summary_prompt = (
            f"用戶本週訓練：{all_names}。部位分佈：{cat_summary}。共{total_sessions}次，{total_duration}分鐘。\n"
            "請用繁體中文，以以下結構回覆（純文字，各項不超過30字）：\n"
            "✅ 做得好：\n⚠️ 需要補強：\n🎯 下週建議：\n"
            "不要加任何多餘說明，直接三行回覆。"
        )
        def _call():
            return gemini_client.models.generate_content(model=GEMINI_MODEL_ID, contents=[summary_prompt]).text
        ai_text = await asyncio.to_thread(_call)
        await ai_msg.edit_text(
            f"💡 *本週訓練分析*\n\n{escape_md(ai_text)}",
            parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error("AI 訓練總結失敗：%s", e)
        await ai_msg.edit_text("（AI 建議暫時無法取得）", parse_mode=ParseMode.MARKDOWN_V2)

    await update.message.reply_text("選擇其他功能：", reply_markup=SUB_KEYBOARD_EXERCISE)


async def show_anatomy_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """發送前後視圖肌肉解剖圖"""
    import os
    from telegram import InputMediaPhoto

    anterior  = ANATOMY_ANTERIOR_PATH
    posterior = ANATOMY_POSTERIOR_PATH

    missing = [p for p in [anterior, posterior] if not os.path.exists(p)]
    if missing:
        await update.message.reply_text(
            "❌ 解剖圖檔案未找到，請確認 `anatomy_anterior.png` 和 `anatomy_posterior.png` 在同一目錄。",
            parse_mode=ParseMode.MARKDOWN_V2)
        return

    await update.message.reply_text("🫀 *肌肉解剖圖*", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        with open(anterior, "rb") as f_ant, open(posterior, "rb") as f_post:
            await update.message.reply_media_group([
                InputMediaPhoto(media=f_ant,  caption="Anterior View — Chest / Shoulders / Abs / Quads"),
                InputMediaPhoto(media=f_post, caption="Posterior View — Back / Glutes / Hamstrings / Calves"),
            ])
    except Exception as e:
        logger.error("發送解剖圖失敗：%s", e)
        await update.message.reply_text("❌ 發送圖片失敗，請稍後再試。")
        return
    await update.message.reply_text("選擇其他功能：", reply_markup=SUB_KEYBOARD_EXERCISE)


async def generate_weight_chart(weight_rows: list) -> bytes | None:
    """
    用 matplotlib 生成體重趨勢圖，回傳 PNG bytes。
    需要 pip install matplotlib numpy
    """
    def _build():
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from datetime import datetime as dt
            import numpy as np
            import io

            # Force built-in font to avoid CJK tofu blocks
            plt.rcParams["font.family"]      = "DejaVu Sans"
            plt.rcParams["axes.unicode_minus"]= False

            valid = [(r[0][:10], float(r[1])) for r in weight_rows
                     if len(r) > 1 and r[1] and float(r[1]) > 0]
            if len(valid) < 2:
                return None

            dates  = [dt.strptime(d, "%Y-%m-%d") for d, _ in valid]
            weights= [w for _, w in valid]

            fig, ax = plt.subplots(figsize=(9, 4.5), facecolor="#1e1e2e")
            ax.set_facecolor("#1e1e2e")

            # 趨勢線（線性回歸）
            x_num = np.array([(d - dates[0]).days for d in dates], dtype=float)
            z     = np.polyfit(x_num, weights, 1)
            p     = np.poly1d(z)
            trend_y = p(x_num)
            trend_color = "#ff6b6b" if z[0] > 0 else "#51cf66"

            ax.plot(dates, weights,  color="#74c0fc", linewidth=2, marker="o",
                    markersize=5, label="Weight", zorder=3)
            ax.plot(dates, trend_y,  color=trend_color, linewidth=1.5,
                    linestyle="--", alpha=0.8, label="Trend")
            ax.fill_between(dates, weights, min(weights) - 0.5,
                            alpha=0.15, color="#74c0fc")

            # 最高/最低標記
            max_idx = weights.index(max(weights))
            min_idx = weights.index(min(weights))
            ax.annotate(f"{weights[max_idx]:.1f}", xy=(dates[max_idx], weights[max_idx]),
                        xytext=(0, 10), textcoords="offset points",
                        color="#ff6b6b", fontsize=8, ha="center")
            ax.annotate(f"{weights[min_idx]:.1f}", xy=(dates[min_idx], weights[min_idx]),
                        xytext=(0, -16), textcoords="offset points",
                        color="#51cf66", fontsize=8, ha="center")

            # 軸格式
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
            plt.setp(ax.get_xticklabels(), rotation=30, ha="right",
                     color="#cdd6f4", fontsize=8)
            ax.tick_params(colors="#cdd6f4", labelsize=8)
            for spine in ax.spines.values():
                spine.set_color("#45475a")
            ax.yaxis.set_tick_params(labelcolor="#cdd6f4")

            # 單位 & 標題
            ax.set_ylabel("kg", color="#cdd6f4", fontsize=9)
            change = weights[-1] - weights[0]
            sign   = "+" if change > 0 else ""
            ax.set_title(
                f"Weight Trend  {weights[-1]:.1f} kg  ({sign}{change:.1f} kg)",
                color="#cdd6f4", fontsize=11, pad=10
            )
            ax.legend(facecolor="#313244", edgecolor="#45475a",
                      labelcolor="#cdd6f4", fontsize=8, loc="upper right")
            ax.grid(axis="y", color="#45475a", linewidth=0.5, alpha=0.7)

            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)
            return buf.read()
        except Exception as e:
            logger.error("生成體重圖表失敗：%s", e)
            return None

    return await asyncio.to_thread(_build)


async def show_weight_trend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    weight_rows = await fetch_weight_rows()
    weight_goal = await load_weight_goal()
    analysis    = analyze_weight_trend_local(weight_rows)
    valid_rows  = [r for r in weight_rows if len(r) > 1 and r[1] and float(r[1]) > 0]
    report      = format_weight_report(valid_rows[-10:], "📈 體重記錄（最近10筆）")

    # 體重目標進度區塊
    goal_block = ""
    if weight_goal and valid_rows:
        latest_wt = float(valid_rows[-1][1])
        diff_wt   = latest_wt - weight_goal
        pct       = max(0, min(100, int((1 - abs(diff_wt) / max(abs(diff_wt), 0.1)) * 100))) if diff_wt != 0 else 100
        bar_fill  = round(pct / 10)
        bar       = "█" * bar_fill + "░" * (10 - bar_fill)
        if abs(diff_wt) < 0.1:
            goal_block = f"\n🎉 *體重目標達成\\!* 目前 {escape_md(str(latest_wt))} kg\n"
        elif diff_wt > 0:
            goal_block = (
                f"\n🎯 *體重目標：* {escape_md(str(weight_goal))} kg\n"
                f"📍 目前：{escape_md(str(latest_wt))} kg　還差 {escape_md(f'{diff_wt:.1f}')} kg\n"
                f"`{escape_md(bar)}` {escape_md(str(pct))}\\%\n"
            )
        else:
            goal_block = (
                f"\n🎯 *體重目標：* {escape_md(str(weight_goal))} kg\n"
                f"✅ 目前：{escape_md(str(latest_wt))} kg　已低於目標 {escape_md(f'{abs(diff_wt):.1f}')} kg\n"
            )

    await update.message.reply_text(
        f"{report}{goal_block}\n━━━━━━━━━━━━━━━━\n📊 *趨勢分析*\n{escape_md(analysis)}",
        parse_mode=ParseMode.MARKDOWN_V2)

    # ── 發送趨勢圖（matplotlib）─────────────────────────────
    if len(valid_rows) >= 2:
        chart_bytes = await generate_weight_chart(valid_rows)
        if chart_bytes:
            import io as _io
            await update.message.reply_photo(
                photo=_io.BytesIO(chart_bytes),
                caption="📈 Weight Trend"
            )
        else:
            await update.message.reply_text(
                "_（趨勢圖生成失敗，請確認已安裝 matplotlib 和 numpy：`pip install matplotlib numpy`）_",
                parse_mode=ParseMode.MARKDOWN_V2)

    await update.message.reply_text("選擇其他功能：", reply_markup=SUB_KEYBOARD_ANALYSIS)


# ════════════════════════════════════════════════════════════
#  📊 每週熱量柱狀圖
# ════════════════════════════════════════════════════════════

async def generate_weekly_calorie_chart(diet_rows: list, activity_rows: list,
                                         goal_kcal: int) -> bytes | None:
    """生成近7天熱量攝取 vs 消耗柱狀圖，回傳 PNG bytes。"""
    def _build():
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.patches as mpatches
            from datetime import date, timedelta
            import io

            # Force built-in font to avoid CJK tofu blocks
            plt.rcParams["font.family"]       = "DejaVu Sans"
            plt.rcParams["axes.unicode_minus"] = False

            today = date.today()
            days  = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
            labels= [d.strftime("%m/%d") for d in days]
            d_strs= [d.strftime("%Y-%m-%d") for d in days]

            intake = []
            burn   = []
            for ds in d_strs:
                day_diet = [r for r in diet_rows if r and r[0].startswith(ds)]
                day_act  = [r for r in activity_rows if r and r[0].startswith(ds)]
                intake.append(sum(float(r[2]) for r in day_diet if len(r) > 2 and r[2]))
                burn.append(sum(float(r[2]) for r in day_act  if len(r) > 2 and r[2]))

            fig, ax = plt.subplots(figsize=(9, 4.8), facecolor="#1e1e2e")
            ax.set_facecolor("#1e1e2e")

            x    = range(len(days))
            w    = 0.35
            bars_in  = ax.bar([i - w/2 for i in x], intake, width=w,
                               color="#74c0fc", alpha=0.85, label="Intake", zorder=3)
            bars_out = ax.bar([i + w/2 for i in x], burn,   width=w,
                               color="#ff8787", alpha=0.85, label="Burned", zorder=3)

            # Goal line
            ax.axhline(goal_kcal, color="#ffd43b", linewidth=1.5,
                       linestyle="--", alpha=0.8, label=f"Goal {goal_kcal} kcal", zorder=4)

            # 數字標籤
            for bar in bars_in:
                h = bar.get_height()
                if h > 0:
                    ax.text(bar.get_x() + bar.get_width()/2, h + 20,
                            f"{int(h)}", ha="center", va="bottom",
                            color="#74c0fc", fontsize=7, fontweight="bold")
            for bar in bars_out:
                h = bar.get_height()
                if h > 0:
                    ax.text(bar.get_x() + bar.get_width()/2, h + 20,
                            f"{int(h)}", ha="center", va="bottom",
                            color="#ff8787", fontsize=7, fontweight="bold")

            # 超標日期標記紅色X軸
            for i, (ing, ds) in enumerate(zip(intake, d_strs)):
                if ing > goal_kcal:
                    ax.get_xticklabels()  # ensure exists
                    ax.axvspan(i - 0.5, i + 0.5, alpha=0.08, color="#ff6b6b", zorder=1)

            ax.set_xticks(list(x))
            ax.set_xticklabels(labels, color="#cdd6f4", fontsize=9)
            ax.tick_params(colors="#cdd6f4", labelsize=8)
            for spine in ax.spines.values():
                spine.set_color("#45475a")
            ax.yaxis.set_tick_params(labelcolor="#cdd6f4")
            ax.set_ylabel("kcal", color="#cdd6f4", fontsize=9)
            ax.set_title("Last 7 Days — Intake vs Burned", color="#cdd6f4", fontsize=11, pad=10)
            ax.legend(facecolor="#313244", edgecolor="#45475a",
                      labelcolor="#cdd6f4", fontsize=8, loc="upper right")
            ax.grid(axis="y", color="#45475a", linewidth=0.5, alpha=0.6)

            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)
            return buf.read()
        except Exception as e:
            logger.error("每週熱量圖生成失敗：%s", e)
            return None

    return await asyncio.to_thread(_build)


async def show_weekly_calorie_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示近7天熱量柱狀圖"""
    from datetime import date, timedelta
    today = date.today()
    week_dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    diet_rows     = await fetch_diet_rows()
    activity_rows = await fetch_activity_rows()
    goal_kcal     = await load_goal()

    diet_week = [r for r in diet_rows     if r and any(r[0].startswith(d) for d in week_dates)]
    act_week  = [r for r in activity_rows if r and any(r[0].startswith(d) for d in week_dates)]

    # 文字摘要
    total_in   = sum(float(r[2]) for r in diet_week if len(r) > 2 and r[2])
    total_burn = sum(float(r[2]) for r in act_week  if len(r) > 2 and r[2])
    avg_in     = total_in / 7
    over_days  = sum(1 for d in week_dates
                     if sum(float(r[2]) for r in diet_week
                            if r and r[0].startswith(d) and len(r) > 2 and r[2]) > goal_kcal)

    summary = (
        f"📊 *近7天熱量統計*\n\n"
        f"🍽 總攝取：{escape_md(str(int(total_in)))} kcal\n"
        f"🔥 總消耗：{escape_md(str(int(total_burn)))} kcal\n"
        f"📈 日均攝取：{escape_md(f'{avg_in:.0f}')} kcal\n"
        f"⚠️ 超標天數：{escape_md(str(over_days))} 天"
    )
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN_V2)

    chart = await generate_weekly_calorie_chart(diet_week, act_week, goal_kcal)
    if chart:
        import io as _io
        await update.message.reply_photo(
            photo=_io.BytesIO(chart),
            caption="📊 Last 7 Days: Intake vs Burned (yellow = goal, red bg = over limit)"
        )
    else:
        await update.message.reply_text(
            "_（圖表生成失敗，請確認已安裝 matplotlib：`pip install matplotlib numpy`）_",
            parse_mode=ParseMode.MARKDOWN_V2)

    await update.message.reply_text("選擇其他功能：", reply_markup=SUB_KEYBOARD_ANALYSIS)


# ════════════════════════════════════════════════════════════
#  📉 BMI 趨勢圖（體重 + BMI 雙軸）
# ════════════════════════════════════════════════════════════

async def generate_bmi_trend_chart(weight_rows: list, height_cm: float) -> bytes | None:
    """生成體重與BMI雙軸趨勢圖，回傳 PNG bytes。"""
    def _build():
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from datetime import datetime as dt
            import io

            # Force built-in font to avoid CJK tofu blocks
            plt.rcParams["font.family"]       = "DejaVu Sans"
            plt.rcParams["axes.unicode_minus"] = False

            valid = [(r[0][:10], float(r[1])) for r in weight_rows
                     if len(r) > 1 and r[1] and float(r[1]) > 0]
            if len(valid) < 2:
                return None

            dates   = [dt.strptime(d, "%Y-%m-%d") for d, _ in valid]
            weights = [w for _, w in valid]
            h_m     = height_cm / 100
            bmis    = [round(w / (h_m ** 2), 1) for w in weights]

            fig, ax1 = plt.subplots(figsize=(9, 4.8), facecolor="#1e1e2e")
            ax2 = ax1.twinx()
            ax1.set_facecolor("#1e1e2e")

            # 體重線（左軸）
            ax1.plot(dates, weights, color="#74c0fc", linewidth=2,
                     marker="o", markersize=5, label="Weight (kg)", zorder=3)
            ax1.fill_between(dates, weights, min(weights) - 0.5,
                             alpha=0.12, color="#74c0fc")

            # BMI 線（右軸）
            ax2.plot(dates, bmis, color="#a9e34b", linewidth=2,
                     marker="s", markersize=4, linestyle="--", label="BMI", zorder=3)

            # BMI 標準區間背景
            ax2.axhspan(18.5, 23.0, alpha=0.08, color="#51cf66", label="Normal 18.5-23")
            ax2.axhline(18.5, color="#51cf66", linewidth=0.8, linestyle=":", alpha=0.6)
            ax2.axhline(23.0, color="#ffd43b", linewidth=0.8, linestyle=":", alpha=0.6)
            ax2.axhline(27.5, color="#ff8787", linewidth=0.8, linestyle=":", alpha=0.6)

            # 軸格式
            for ax in [ax1, ax2]:
                ax.tick_params(colors="#cdd6f4", labelsize=8)
                for spine in ax.spines.values():
                    spine.set_color("#45475a")

            ax1.set_ylabel("kg", color="#74c0fc", fontsize=9)
            ax2.set_ylabel("BMI", color="#a9e34b", fontsize=9)
            ax1.yaxis.set_tick_params(labelcolor="#74c0fc")
            ax2.yaxis.set_tick_params(labelcolor="#a9e34b")

            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            ax1.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
            plt.setp(ax1.get_xticklabels(), rotation=30, ha="right",
                     color="#cdd6f4", fontsize=8)

            current_bmi = bmis[-1]
            bmi_label = (
                "Underweight" if current_bmi < 18.5 else
                "Normal"      if current_bmi < 23   else
                "Overweight"  if current_bmi < 27.5 else "Obese"
            )

            ax1.set_title(
                f"Weight & BMI Trend   Now: {weights[-1]:.1f} kg  BMI {current_bmi:.1f} ({bmi_label})",
                color="#cdd6f4", fontsize=10, pad=10
            )

            # 合併圖例
            lines1, labels1 = ax1.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax1.legend(lines1 + lines2, labels1 + labels2,
                       facecolor="#313244", edgecolor="#45475a",
                       labelcolor="#cdd6f4", fontsize=8, loc="upper left")
            ax1.grid(axis="y", color="#45475a", linewidth=0.5, alpha=0.5)

            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)
            return buf.read()
        except Exception as e:
            logger.error("BMI趨勢圖生成失敗：%s", e)
            return None

    return await asyncio.to_thread(_build)


async def show_bmi_trend_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示 BMI 趨勢圖"""
    height_cm   = await load_height()
    weight_rows = await fetch_weight_rows()
    valid_rows  = [r for r in weight_rows if len(r) > 1 and r[1] and float(r[1]) > 0]

    if height_cm <= 0:
        await update.message.reply_text(
            "❌ 請先到 ⚙️ 設定 → 📏 設定身高，才能計算 BMI\\.",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_ANALYSIS)
        return

    if len(valid_rows) < 2:
        await update.message.reply_text(
            "❌ 體重記錄不足（至少需要2筆），請先記錄體重\\.",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_ANALYSIS)
        return

    h_m = height_cm / 100
    latest_wt  = float(valid_rows[-1][1])
    current_bmi= round(latest_wt / (h_m ** 2), 1)
    ideal_low  = round(18.5 * h_m ** 2, 1)
    ideal_high = round(23.0 * h_m ** 2, 1)
    bmi_label  = (
        "偏輕 🔵" if current_bmi < 18.5 else
        "正常 🟢" if current_bmi < 23   else
        "過重 🟡" if current_bmi < 27.5 else "肥胖 🔴"
    )

    summary = (
        f"📉 *BMI 趨勢分析*\n\n"
        f"📏 身高：{escape_md(str(int(height_cm)))} cm\n"
        f"⚖️ 目前體重：{escape_md(str(latest_wt))} kg\n"
        f"📊 當前 BMI：*{escape_md(str(current_bmi))}* {escape_md(bmi_label)}\n"
        f"🎯 理想體重：{escape_md(str(ideal_low))}–{escape_md(str(ideal_high))} kg\n\n"
        f"_BMI 參考：\\<18\\.5 偏輕 \\| 18\\.5–23 正常 \\| 23–27\\.5 過重 \\| \\>27\\.5 肥胖_"
    )
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN_V2)

    chart = await generate_bmi_trend_chart(valid_rows, height_cm)
    if chart:
        import io as _io
        await update.message.reply_photo(
            photo=_io.BytesIO(chart),
            caption="📉 Weight & BMI Trend (green zone = normal BMI 18.5-23)"
        )
    else:
        await update.message.reply_text(
            "_（圖表生成失敗，請確認已安裝 matplotlib：`pip install matplotlib numpy`）_",
            parse_mode=ParseMode.MARKDOWN_V2)

    await update.message.reply_text("選擇其他功能：", reply_markup=SUB_KEYBOARD_ANALYSIS)


# ════════════════════════════════════════════════════════════
#  🌙 每日推送附今日進度圓形圖
# ════════════════════════════════════════════════════════════

async def generate_daily_progress_chart(total_in: float, goal_kcal: int,
                                          total_prot: float, protein_goal: int,
                                          total_burn: float, burn_goal: int,
                                          water_ml: int, water_goal: int) -> bytes | None:
    """生成今日4項指標圓形進度圖（2×2），回傳 PNG bytes。"""
    def _build():
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.patches as mpatches
            import io

            # Force built-in font to avoid CJK tofu blocks
            plt.rcParams["font.family"]       = "DejaVu Sans"
            plt.rcParams["axes.unicode_minus"] = False

            metrics = [
                ("Calorie Intake", total_in,    goal_kcal,    "#74c0fc", "#1e3a5f", "kcal"),
                ("Protein",        total_prot,  protein_goal, "#a9e34b", "#2d3f1e", "g"),
                ("Calories Burned",total_burn,  burn_goal,    "#ff8787", "#3f1e1e", "kcal"),
                ("Water",          water_ml,    water_goal,   "#63e6be", "#1e3f38", "ml"),
            ]

            fig, axes = plt.subplots(2, 2, figsize=(7, 6.5), facecolor="#1e1e2e")
            fig.subplots_adjust(hspace=0.45, wspace=0.35)

            for ax, (label, val, goal, color, bg, unit) in zip(axes.flat, metrics):
                ax.set_facecolor("#1e1e2e")
                ratio = min(val / goal, 1.0) if goal > 0 else 0
                remain = 1.0 - ratio

                wedge_data  = [ratio, remain] if ratio > 0 else [0.001, 0.999]
                wedge_colors= [color, bg]
                ax.pie(wedge_data, colors=wedge_colors, startangle=90,
                       counterclock=False,
                       wedgeprops={"width": 0.38, "edgecolor": "#1e1e2e", "linewidth": 2})

                pct       = int(ratio * 100)
                color_txt = "#51cf66" if pct >= 100 else color
                ax.text(0,  0.12, f"{pct}%", ha="center", va="center",
                        color=color_txt, fontsize=14, fontweight="bold")
                ax.text(0, -0.18, f"{int(val)}/{int(goal)}{unit}",
                        ha="center", va="center", color="#a6adc8", fontsize=7.5)

                if val > goal and goal > 0:
                    over = int(val - goal)
                    ax.text(0, -0.48, f"+{over}{unit} over",
                            ha="center", va="center", color="#ff6b6b", fontsize=7)

                ax.set_title(label, color="#cdd6f4", fontsize=9.5, pad=6)

            fig.suptitle("Today's Progress", color="#cdd6f4", fontsize=12, y=0.98)
            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor(),
                        bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            return buf.read()
        except Exception as e:
            logger.error("Daily progress chart failed: %s", e)
            return None

    return await asyncio.to_thread(_build)


async def build_weekly_report_text() -> str:
    """生成上週週報文字（自動推送和手動點擊共用）。"""
    today     = datetime.now().date()
    last_mon  = today - timedelta(days=today.weekday() + 7)
    week_dates= [(last_mon + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    week_label= f"{week_dates[0]} \\~ {week_dates[6]}"
    goal_kcal = await load_goal()

    diet_rows     = await fetch_diet_rows()
    exercise_rows = await fetch_exercise_rows()
    activity_rows = await fetch_activity_rows()
    weight_rows   = await fetch_weight_rows()

    dw = [r for r in diet_rows     if r and any(r[0].startswith(d) for d in week_dates)]
    ew = [r for r in exercise_rows if r and any(r[0].startswith(d) for d in week_dates)]
    aw = [r for r in activity_rows if r and any(r[0].startswith(d) for d in week_dates)]
    ww = [r for r in weight_rows   if r and any(r[0].startswith(d) for d in week_dates)]

    # 本週訓練部位統計（不需 AI）
    from collections import defaultdict, Counter
    cat_counter: Counter = Counter()
    exercise_min = 0
    for r in ew:
        cat = r[3] if len(r) > 3 else ""
        if cat:
            cat_counter[cat] += 1
        try:
            exercise_min += int(float(r[2])) if len(r) > 2 else 0
        except Exception:
            pass
    CATEGORY_EMOJI = {
        "胸": "🫁", "背": "🔙", "肩": "🔺", "手臂": "💪",
        "核心": "⚡", "腿": "🦵", "全身": "🏋️", "有氧": "🏃", "柔韌": "🧘"
    }
    cat_lines = "  ".join(
        f"{CATEGORY_EMOJI.get(c,'🏋️')}{c}×{n}"
        for c, n in cat_counter.most_common()
    ) or "無記錄"

    total_in   = sum(float(r[2]) for r in dw if len(r) > 2 and r[2])
    total_steps= sum(float(r[1]) for r in aw if len(r) > 1 and r[1])
    total_hp   = sum(float(r[3]) for r in aw if len(r) > 3 and r[3])
    net        = total_in
    days_rec   = len(set(r[0][:10] for r in dw if r))
    wt_list    = [float(r[1]) for r in ww if len(r) > 1 and r[1] and float(r[1]) > 0]
    if len(wt_list) >= 2:
        diff    = wt_list[-1] - wt_list[0]
        sign    = "\\+" if diff > 0 else ""
        wt_change = f"{sign}{escape_md(f'{diff:.1f}')} kg"
    elif wt_list:
        wt_change = escape_md(f"{wt_list[0]} kg")
    else:
        wt_change = "無記錄"

    try:
        advice = await ai_weekly_summary(dw, ew, aw, ww, goal_kcal)
    except Exception:
        advice = "（AI 建議暫時無法取得）"

    return (
        f"📆 *週報 \\— {escape_md(week_label)}*\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📝 *記錄天數：* {escape_md(str(days_rec))} 天\n"
        f"🍽 *總攝取：* {escape_md(str(int(total_in)))} kcal\n"
        f"🎯 *週目標：* {escape_md(str(goal_kcal * 7))} kcal\n"
        f"⚡ *淨熱量：* {escape_md(str(int(net)))} kcal\n"
        f"👟 *總步數：* {escape_md(str(int(total_steps)))}\n"
        f"❤️ *心率點：* {escape_md(str(int(total_hp)))}\n"
        f"🏋️ *訓練時長：* {escape_md(str(exercise_min))} 分鐘\n"
        f"💪 *訓練部位：* {escape_md(cat_lines)}\n"
        f"⚖️ *體重變化：* {wt_change}\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"🤖 *AI 週評*\n{escape_md(advice)}"
    )


async def send_weekly_report_to_chat(bot, chat_id: int):
    text = await build_weekly_report_text()
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2)


async def show_weekly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """生成上週完整週報（用戶手動點擊）。"""
    msg = await update.message.reply_text("📆 正在生成週報\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        text = await build_weekly_report_text()
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error("週報生成失敗：%s", e)
        await msg.edit_text("❌ 週報生成失敗，請稍後重試\\.", parse_mode=ParseMode.MARKDOWN_V2)
    await update.message.reply_text("選擇其他功能：", reply_markup=SUB_KEYBOARD_VIEW)


async def build_monthly_report_text() -> str:
    """生成上月月報文字（自動推送和手動點擊共用）。"""
    today            = datetime.now().date()
    first_this       = today.replace(day=1)
    last_month_end   = first_this - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    month_label      = last_month_start.strftime("%Y年%m月")
    month_prefix     = last_month_start.strftime("%Y-%m")
    goal_kcal        = await load_goal()

    diet_rows     = await fetch_diet_rows()
    exercise_rows = await fetch_exercise_rows()
    activity_rows = await fetch_activity_rows()
    weight_rows   = await fetch_weight_rows()

    dm = [r for r in diet_rows     if r and r[0].startswith(month_prefix)]
    em = [r for r in exercise_rows if r and r[0].startswith(month_prefix)]
    am = [r for r in activity_rows if r and r[0].startswith(month_prefix)]
    wm = [r for r in weight_rows   if r and r[0].startswith(month_prefix)]

    from collections import Counter
    cat_counter: Counter = Counter()
    exercise_min = 0
    for r in em:
        cat = r[3] if len(r) > 3 else ""
        if cat:
            cat_counter[cat] += 1
        try:
            exercise_min += int(float(r[2])) if len(r) > 2 else 0
        except Exception:
            pass
    CATEGORY_EMOJI = {
        "胸": "🫁", "背": "🔙", "肩": "🔺", "手臂": "💪",
        "核心": "⚡", "腿": "🦵", "全身": "🏋️", "有氧": "🏃", "柔韌": "🧘"
    }
    cat_lines = "  ".join(
        f"{CATEGORY_EMOJI.get(c,'🏋️')}{c}×{n}"
        for c, n in cat_counter.most_common()
    ) or "無記錄"

    total_in    = sum(float(r[2]) for r in dm if len(r) > 2 and r[2])
    total_steps = sum(float(r[1]) for r in am if len(r) > 1 and r[1])
    total_hp    = sum(float(r[3]) for r in am if len(r) > 3 and r[3])
    net         = total_in
    days_rec    = len(set(r[0][:10] for r in dm if r))
    days_in_month = last_month_end.day
    avg_in      = total_in / days_in_month if days_in_month else 0
    wt_list     = [float(r[1]) for r in wm if len(r) > 1 and r[1] and float(r[1]) > 0]
    if len(wt_list) >= 2:
        diff = wt_list[-1] - wt_list[0]
        sign = "\\+" if diff > 0 else ""
        wt_change = f"{sign}{escape_md(f'{diff:.1f}')} kg"
    elif wt_list:
        wt_change = escape_md(f"{wt_list[0]} kg")
    else:
        wt_change = "無記錄"

    try:
        advice = await ai_monthly_summary(dm, em, am, wm, goal_kcal)
    except Exception:
        advice = "（AI 建議暫時無法取得）"

    return (
        f"🗓 *月報 \\— {escape_md(month_label)}*\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📝 *記錄天數：* {escape_md(str(days_rec))} ／ {escape_md(str(days_in_month))} 天\n"
        f"🍽 *總攝取：* {escape_md(str(int(total_in)))} kcal\n"
        f"🍽 *日均攝取：* {escape_md(str(int(avg_in)))} kcal\n"
        f"🎯 *月目標：* {escape_md(str(goal_kcal * days_in_month))} kcal\n"
        f"⚡ *月淨熱量：* {escape_md(str(int(net)))} kcal\n"
        f"👟 *總步數：* {escape_md(str(int(total_steps)))}\n"
        f"❤️ *心率點：* {escape_md(str(int(total_hp)))}\n"
        f"🏋️ *訓練時長：* {escape_md(str(exercise_min))} 分鐘\n"
        f"💪 *訓練部位：* {escape_md(cat_lines)}\n"
        f"⚖️ *體重變化：* {wt_change}\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"🤖 *AI 月評*\n{escape_md(advice)}"
    )


async def send_monthly_report_to_chat(bot, chat_id: int):
    text = await build_monthly_report_text()
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2)


async def show_monthly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """生成上個月完整月報（用戶手動點擊）。"""
    msg = await update.message.reply_text("🗓 正在生成月報\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    try:
        text = await build_monthly_report_text()
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error("月報生成失敗：%s", e)
        await msg.edit_text("❌ 月報生成失敗，請稍後重試\\.", parse_mode=ParseMode.MARKDOWN_V2)
    await update.message.reply_text("選擇其他功能：", reply_markup=SUB_KEYBOARD_VIEW)


async def show_streak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """目標達成追蹤 — 今日進度 + 簽到天數 + 激勵語"""
    await update.message.reply_text("🏆 正在計算達成記錄\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)

    td            = today_str()
    diet_rows     = await fetch_diet_rows()
    exercise_rows = await fetch_exercise_rows()
    activity_rows = await fetch_activity_rows()
    goal_kcal     = await load_goal()
    protein_goal  = await load_protein_goal()
    burn_goal_val = await load_burn_goal()
    steps_goal    = await load_steps_goal()
    hp_goal       = await load_hp_goal()
    water_goal    = await load_water_goal()
    water_today   = await fetch_water_today()

    # 今日數據
    td_diet  = [r for r in diet_rows     if r and r[0][:10] == td]
    td_act   = [r for r in activity_rows if r and r[0][:10] == td]
    today_in    = sum(float(r[2]) for r in td_diet if len(r)>2 and r[2])
    today_prot  = sum(float(r[3]) for r in td_diet if len(r)>3 and r[3])
    today_burn  = sum(float(r[2]) for r in td_act  if len(r)>2 and r[2])
    today_steps = sum(float(r[1]) for r in td_act  if len(r)>1 and r[1])
    today_hp    = sum(float(r[3]) for r in td_act  if len(r)>3 and r[3])

    # 簽到統計
    stats       = await asyncio.to_thread(get_streak_and_stats, diet_rows, exercise_rows, activity_rows, goal_kcal)
    streak      = stats["streak"]
    best_streak = stats["best_streak"]
    total_days  = stats["total_days"]

    # 今日整體進度（以熱量為主）
    today_pct = round(today_in / goal_kcal * 100) if goal_kcal else 0

    # 進度條
    bar_kcal  = progress_bar(today_in,    goal_kcal)
    bar_prot  = progress_bar(today_prot,  protein_goal)
    bar_burn  = progress_bar(today_burn,  burn_goal_val)
    bar_steps = progress_bar(today_steps, steps_goal)
    bar_hp    = progress_bar(today_hp,    hp_goal)
    bar_water = progress_bar(water_today, water_goal)

    # 簽到連續 emoji
    if streak >= 30:
        s_emoji = "🔥🔥🔥"
    elif streak >= 14:
        s_emoji = "🔥🔥"
    elif streak >= 7:
        s_emoji = "🔥"
    elif streak >= 1:
        s_emoji = "✅"
    else:
        s_emoji = "💪"

    # 激勵語
    motivation = get_motivation(streak, today_pct)

    report = (
        f"🏆 *目標達成追蹤*\n\n"
        f"━━ 🍽 *飲食攝取* ━━\n"
        f"熱量　`{escape_md(bar_kcal)}`\n"
        f"　{escape_md(str(int(today_in)))} ／ {escape_md(str(goal_kcal))} kcal\n"
        f"蛋白質　`{escape_md(bar_prot)}`\n"
        f"　{escape_md(f'{today_prot:.0f}')} ／ {escape_md(str(protein_goal))} g\n\n"
        f"━━ 🏃 *運動消耗* ━━\n"
        f"卡路里　`{escape_md(bar_burn)}`\n"
        f"　{escape_md(str(int(today_burn)))} ／ {escape_md(str(burn_goal_val))} kcal\n"
        f"步數　`{escape_md(bar_steps)}`\n"
        f"　{escape_md(str(int(today_steps)))} ／ {escape_md(str(steps_goal))} 步\n"
        f"Heart Points　`{escape_md(bar_hp)}`\n"
        f"　{escape_md(str(int(today_hp)))} ／ {escape_md(str(hp_goal))} 點 _\\(WHO建議每週150點\\)_\n\n"
        f"━━ 💧 *飲水* ━━\n"
        f"`{escape_md(bar_water)}`  {escape_md(str(water_today))} ／ {escape_md(str(water_goal))} ml\n\n"
        f"━━ {s_emoji} *簽到記錄* ━━\n"
        f"目前連續：{escape_md(str(streak))} 天\n"
        f"歷史最長：{escape_md(str(best_streak))} 天\n"
        f"累計簽到：{escape_md(str(total_days))} 天\n\n"
        f"_{escape_md(motivation)}_"
    )

    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_ANALYSIS)


async def show_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    diet_rows     = await fetch_diet_rows()
    exercise_rows = await fetch_exercise_rows()
    weight_rows   = await fetch_weight_rows()
    activity_rows = await fetch_activity_rows()
    goal_kcal     = await load_goal()

    await update.message.reply_text(
        format_diet_report(diet_rows, "📂 全部飲食記錄", goal_kcal),
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=SUB_KEYBOARD_VIEW)
    if exercise_rows:
        await update.message.reply_text(
            format_exercise_report(exercise_rows, "🏃 全部運動記錄"),
            parse_mode=ParseMode.MARKDOWN_V2)
    if activity_rows:
        await update.message.reply_text(
            format_activity_report(activity_rows, "👟 全部活動數據"),
            parse_mode=ParseMode.MARKDOWN_V2)
    if weight_rows:
        await update.message.reply_text(
            format_weight_report(weight_rows, "⚖️ 全部體重記錄"),
            parse_mode=ParseMode.MARKDOWN_V2)


# ════════════════════════════════════════════════════════════
#  錯誤處理
# ════════════════════════════════════════════════════════════

async def handle_inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理所有 Inline 按鈕回調"""
    query = update.callback_query
    await query.answer()  # 消除 Telegram 的 loading 狀態

    if query.data == "undo_last":
        result = await undo_last_action()
        # 更新原訊息，移除 Inline 按鈕並顯示結果
        try:
            original_text = query.message.text or ""
            # 截取原文前兩行（避免過長）
            header = "\n".join(original_text.split("\n")[:3])
            await query.edit_message_text(
                f"{escape_md(header)}\n\n{escape_md(result)}",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=escape_md(result),
                parse_mode=ParseMode.MARKDOWN_V2
            )

    elif query.data == "undo_confirm":
        result = await undo_last_action()
        await query.edit_message_text(
            escape_md(result), parse_mode=ParseMode.MARKDOWN_V2
        )


async def cmd_test_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """測試推送功能是否正常運作"""
    chat_id = update.effective_chat.id
    lines = ["🔧 *推送系統診斷*\n"]

    # 1. JobQueue 狀態
    app = context.application
    if app.job_queue is None:
        lines.append("❌ *JobQueue 不可用*\n請執行：\n`pip install 'python-telegram-bot[job-queue]'`")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        return
    else:
        lines.append("✅ JobQueue 正常")

    # 2. 目前排程任務數量
    jobs = app.job_queue.jobs()
    lines.append(f"✅ 排程任務數：{escape_md(str(len(jobs)))} 個")
    for j in jobs:
        lines.append(f"   • {escape_md(str(j.name or '未命名'))}")

    # 3. Chat ID 確認
    lines.append(f"\n📱 您的 Chat ID：`{escape_md(str(chat_id))}`")
    lines.append(f"📱 設定的 Chat ID：`{escape_md(str(USER_CHAT_ID))}`")
    if chat_id == USER_CHAT_ID:
        lines.append("✅ Chat ID 匹配，推送會發到這裡")
    else:
        lines.append("⚠️ *Chat ID 不匹配！* 推送會發到錯誤的地方\n請把 `USER_CHAT_ID` 改成您的 Chat ID")

    # 4. 立即發送一條測試推送
    lines.append("\n🚀 *立即發送測試推送中\\.\\.\\.*")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    # 強制執行一次智能推送（忽略防重複，直接發）
    try:
        td = today_str()
        diet_rows     = await fetch_diet_rows(filter_date=td)
        activity_rows = await fetch_activity_rows(filter_date=td)
        goal_kcal     = await load_goal()
        protein_goal  = await load_protein_goal()
        burn_goal_v   = await load_burn_goal()
        steps_goal    = await load_steps_goal()
        water_goal    = await load_water_goal()
        water_ml      = await fetch_water_today()

        total_in    = sum(float(r[2]) for r in diet_rows     if len(r) > 2 and r[2])
        total_prot  = sum(float(r[3]) for r in diet_rows     if len(r) > 3 and r[3])
        total_burn  = sum(float(r[2]) for r in activity_rows if len(r) > 2 and r[2])
        total_steps = sum(float(r[1]) for r in activity_rows if len(r) > 1 and r[1])
        total_hp    = sum(float(r[3]) for r in activity_rows if len(r) > 3 and r[3])

        import pytz as _pytz
        _tz      = _pytz.timezone(TIMEZONE)
        hour_now = datetime.now(_tz).hour

        test_msg = (
            f"🔧 *推送測試訊息*\n\n"
            f"⏰ 現在時間：{escape_md(str(hour_now))} 點\n\n"
            f"🍽 攝取：{escape_md(str(int(total_in)))} / {escape_md(str(goal_kcal))} kcal\n"
            f"🥩 蛋白質：{escape_md(f'{total_prot:.0f}')} / {escape_md(str(protein_goal))} g\n"
            f"🔥 消耗：{escape_md(str(int(total_burn)))} / {escape_md(str(burn_goal_v))} kcal\n"
            f"👟 步數：{escape_md(str(int(total_steps)))} / {escape_md(str(steps_goal))}\n"
            f"💧 飲水：{escape_md(str(water_ml))} / {escape_md(str(water_goal))} ml\n\n"
            f"✅ 推送功能正常！\n"
            f"_智能推送每小時自動執行，但每種通知每天只發一次_"
        )
        await context.bot.send_message(
            chat_id=chat_id, text=test_msg, parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ 測試推送失敗：{escape_md(str(e))}",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    logger.error("Exception:", exc_info=context.error)


# ════════════════════════════════════════════════════════════
#  主程式入口
# ════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception:", exc_info=context.error)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("weight",       cmd_weight))
    app.add_handler(CommandHandler("undo",         cmd_undo))
    app.add_handler(CommandHandler("confirm_undo", cmd_confirm_undo))
    app.add_handler(CommandHandler("test_push",    cmd_test_push))
    app.add_handler(CallbackQueryHandler(handle_inline_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    # ── 檢查 JobQueue 是否可用 ─────────────────────────────
    if app.job_queue is None:
        logger.error(
            "❌ JobQueue 不可用！所有定時推送均已停用。\n"
            "   請安裝 APScheduler：pip install 'python-telegram-bot[job-queue]'\n"
            "   或：pip install apscheduler"
        )
        logger.info("🤖 Bot 已啟動（無定時推送）！")
        app.run_polling(drop_pending_updates=True)
        return

    # 每日定時推送
    if USER_CHAT_ID:
        tz          = pytz.timezone(TIMEZONE)
        report_time = dtime(DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE, 0, tzinfo=tz)
        app.job_queue.run_daily(daily_report_job, time=report_time)
        logger.info("✅ 每日報告設定於 %02d:%02d %s 推送。", DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE, TIMEZONE)

        # ── 週報：週一早上8點自動推送上週 ───────────────────
        async def weekly_report_job(ctx):
            try:
                await send_weekly_report_to_chat(ctx.bot, USER_CHAT_ID)
                logger.info("週報已自動推送。")
            except Exception as e:
                logger.error("週報自動推送失敗：%s", e)

        # ── AI教練週報：週一早上8:30自動推送 ──────────────
        async def ai_coach_weekly_job(ctx):
            try:
                coach_ctx = await get_coach_context()
                report    = await generate_weekly_coach_report(coach_ctx)
                await ctx.bot.send_message(
                    chat_id=USER_CHAT_ID,
                    text=f"🤖 *AI教練週報*\n\n{escape_md(report)}",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                logger.info("AI教練週報已推送。")
            except Exception as e:
                logger.error("AI教練週報推送失敗：%s", e)

        # ── 月報：每月1號早上8點自動推送上月 ────────────────
        async def monthly_report_job(ctx):
            if datetime.now(tz).day != MONTHLY_REPORT_DAY:
                return
            try:
                await send_monthly_report_to_chat(ctx.bot, USER_CHAT_ID)
                logger.info("月報已自動推送。")
            except Exception as e:
                logger.error("月報自動推送失敗：%s", e)

        weekly_time  = dtime(8, 0, 0, tzinfo=tz)
        coach_time   = dtime(8, 30, 0, tzinfo=tz)
        monthly_time = dtime(8, 0, 0, tzinfo=tz)

        app.job_queue.run_daily(
            weekly_report_job,
            time=weekly_time,
            days=(WEEKLY_REPORT_DAY,),
        )
        app.job_queue.run_daily(
            ai_coach_weekly_job,
            time=coach_time,
            days=(WEEKLY_REPORT_DAY,),
        )
        app.job_queue.run_daily(
            monthly_report_job,
            time=monthly_time,
            days=tuple(range(7)),
        )
        logger.info("✅ 週報/月報自動推送已設定。")

        # ── 智能推送：每小時檢查達標/超標 ───────────────────
        async def smart_alert_job(ctx):
            await check_and_send_smart_alerts(ctx.bot, USER_CHAT_ID)

        app.job_queue.run_repeating(smart_alert_job, interval=3600, first=60)

        # ── 本週訓練提醒：每週日晚上8點提醒未完成部位 ──────
        async def muscle_reminder_job(ctx):
            try:
                plan  = await load_muscle_plan()
                stats = await fetch_weekly_category_stats()
                if not plan:
                    return
                missing = [
                    f"{MUSCLE_EMOJI.get(cat,'🏋️')} {cat}（目標 {plan[cat]} 次，已完成 {stats.get(cat,0)} 次）"
                    for cat in plan if stats.get(cat, 0) < plan[cat]
                ]
                if missing:
                    msg = "💪 *本週訓練提醒*\n\n以下部位本週還未完成目標：\n" + \
                          "\n".join(f"  {escape_md(m)}" for m in missing)
                    await ctx.bot.send_message(chat_id=USER_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as e:
                logger.error("訓練提醒失敗：%s", e)

        muscle_reminder_time = dtime(20, 0, 0, tzinfo=tz)
        app.job_queue.run_daily(muscle_reminder_job, time=muscle_reminder_time, days=(6,))  # 週日

        # 🌙 每天早上 8:00 推送睡眠報告
        sleep_report_time = dtime(8, 0, 0, tzinfo=tz)
        app.job_queue.run_daily(morning_sleep_report_job, time=sleep_report_time)
        logger.info("🌙 早安睡眠報告：每日 08:00 推送")
        logger.info("✅ 智能推送和訓練提醒已設定。")
    else:
        logger.warning("⚠️  USER_CHAT_ID 未設定，每日推送已停用。發送 /start 查看您的 Chat ID。")

    logger.info("🤖 Bot 已啟動！")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()