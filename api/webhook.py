"""Telegram-бот поиска музыки для Vercel (webhook, без постоянного сервера).

Пишешь название или исполнителя -> бот ищет в Audius и (если задан токен) в Яндекс Музыке,
показывает кнопки -> по нажатию отправляет аудио.
Файлы бот не скачивает: Telegram сам забирает аудио по ссылке.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
YM_TOKEN = os.environ.get("YANDEX_MUSIC_TOKEN", "")

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
AUDIUS = "https://api.audius.co/v1"
APP = "tg-music-bot"
PER_SOURCE = 5
MAX_UPLOAD = 48 * 1024 * 1024  # лимит Telegram для ботов ~50 МБ

_ym_client = None


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def tg(method, **params):
    try:
        r = requests.post(f"{TG}/{method}", json=params, timeout=30)
        return r.json()
    except Exception as e:  # noqa: BLE001
        log("tg error", method, e)
        return {"ok": False, "description": str(e)}


# ---------- Audius ----------

def search_audius(query):
    try:
        r = requests.get(
            f"{AUDIUS}/tracks/search",
            params={"query": query, "limit": PER_SOURCE, "app_name": APP},
            timeout=15,
        )
        data = r.json().get("data", [])
    except Exception as e:  # noqa: BLE001
        log("audius search error", e)
        return []
    return [
        {
            "src": "a",
            "id": t["id"],
            "title": t.get("title") or "Без названия",
            "artist": (t.get("user") or {}).get("name", ""),
        }
        for t in data
    ]


def audius_track(track_id):
    r = requests.get(f"{AUDIUS}/tracks/{track_id}", params={"app_name": APP}, timeout=15)
    t = r.json()["data"]
    stream = f"{AUDIUS}/tracks/{track_id}/stream?app_name={APP}"
    try:
        # Узнаём конечную ссылку на аудио (у Audius это редирект на ноду)
        resp = requests.get(stream, allow_redirects=False, timeout=15)
        url = resp.headers.get("Location") or stream
    except Exception:  # noqa: BLE001
        url = stream
    return {
        "url": url,
        "title": t.get("title") or "Track",
        "artist": (t.get("user") or {}).get("name", ""),
        "duration": t.get("duration"),
    }


# ---------- Яндекс Музыка (необязательно) ----------

def ym():
    global _ym_client
    if _ym_client is None and YM_TOKEN:
        from yandex_music import Client

        _ym_client = Client(YM_TOKEN).init()
    return _ym_client


def search_yandex(query):
    if not YM_TOKEN:
        return []
    try:
        res = ym().search(query, type_="track")
        tracks = res.tracks.results[:PER_SOURCE] if res and res.tracks else []
    except Exception as e:  # noqa: BLE001
        log("yandex search error", e)
        return []
    out = []
    for t in tracks:
        album = t.albums[0].id if t.albums else None
        out.append(
            {
                "src": "y",
                "id": f"{t.id}:{album}" if album else str(t.id),
                "title": t.title,
                "artist": ", ".join(t.artists_name()),
            }
        )
    return out


def yandex_track(track_id):
    t = ym().tracks([track_id])[0]
    infos = t.get_download_info(get_direct_links=True)
    mp3 = [i for i in infos if i.codec == "mp3"] or infos
    best = max(mp3, key=lambda i: i.bitrate_in_kbps)
    return {
        "url": best.direct_link,
        "title": t.title,
        "artist": ", ".join(t.artists_name()),
        "duration": (t.duration_ms or 0) // 1000,
    }


# ---------- Отправка аудио ----------

def deliver(chat_id, info):
    params = {
        "chat_id": chat_id,
        "audio": info["url"],
        "title": info["title"],
        "performer": info["artist"],
    }
    if info.get("duration"):
        params["duration"] = info["duration"]
    res = tg("sendAudio", **params)
    if res.get("ok"):
        return True

    # Запасной путь: скачиваем сами и загружаем в Telegram
    log("sendAudio by URL failed:", res.get("description"))
    try:
        with requests.get(info["url"], stream=True, timeout=40) as r:
            r.raise_for_status()
            buf = b""
            for chunk in r.iter_content(256 * 1024):
                buf += chunk
                if len(buf) > MAX_UPLOAD:
                    raise ValueError("файл больше 48 МБ")
        up = requests.post(
            f"{TG}/sendAudio",
            data={"chat_id": chat_id, "title": info["title"], "performer": info["artist"]},
            files={"audio": (f"{info['artist']} - {info['title']}.mp3", buf)},
            timeout=60,
        ).json()
        if up.get("ok"):
            return True
        log("upload failed:", up.get("description"))
    except Exception as e:  # noqa: BLE001
        log("fallback download failed:", e)
    return False


# ---------- Логика бота ----------

def keyboard(results):
    rows = []
    for r in results:
        mark = "🟣" if r["src"] == "a" else "🟡"
        label = f"{mark} {r['artist']} — {r['title']}"[:60]
        rows.append([{"text": label, "callback_data": f"{r['src']}:{r['id']}"}])
    return {"inline_keyboard": rows}


def on_text(chat_id, text):
    text = (text or "").strip()
    if text.startswith("/start") or text.startswith("/help"):
        tg(
            "sendMessage",
            chat_id=chat_id,
            text="Привет! Напиши название песни или исполнителя, и я найду её.",
        )
        return
    if not text or text.startswith("/"):
        return
    results = search_audius(text) + search_yandex(text)
    if not results:
        tg("sendMessage", chat_id=chat_id, text="Ничего не найдено 😔 Попробуй другой запрос.")
        return
    tg(
        "sendMessage",
        chat_id=chat_id,
        text=f"Результаты по запросу «{text}»:\n🟣 Audius   🟡 Яндекс Музыка",
        reply_markup=keyboard(results),
    )


def on_callback(cb):
    tg("answerCallbackQuery", callback_query_id=cb["id"], text="⏳ Загружаю...")
    chat_id = cb["message"]["chat"]["id"]
    src, _, track_id = (cb.get("data") or "").partition(":")
    try:
        info = audius_track(track_id) if src == "a" else yandex_track(track_id)
    except Exception as e:  # noqa: BLE001
        log("track info error", e)
        tg("sendMessage", chat_id=chat_id, text="Не удалось получить трек 😔")
        return
    if not deliver(chat_id, info):
        tg("sendMessage", chat_id=chat_id, text="Не удалось отправить этот трек, попробуй другой.")


def process_update(update):
    if "callback_query" in update:
        on_callback(update["callback_query"])
    elif "message" in update:
        m = update["message"]
        on_text(m["chat"]["id"], m.get("text"))


class handler(BaseHTTPRequestHandler):
    def _reply(self, code=200, body=b"ok"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, b"Bot is running")

    def do_POST(self):
        if WEBHOOK_SECRET and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            self._reply(403, b"forbidden")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            update = json.loads(self.rfile.read(length) or b"{}")
            process_update(update)
        except Exception as e:  # noqa: BLE001
            log("update error", e)
        # Всегда отвечаем 200, чтобы Telegram не повторял апдейт
        self._reply(200)
