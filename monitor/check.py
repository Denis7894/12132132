#!/usr/bin/env python3
"""Проверка доступности сайта кузница-контента.рф из России с оповещениями в Telegram.

Запускается по расписанию (GitHub Actions, раз в 5 минут), работает с 22.11.2026. Только стандартная библиотека.

Что делает за один запуск:
  1. Открывает главную страницу напрямую и через узлы check-host.net в России.
  2. Если что-то не так, перепроверяет через минуту, чтобы не поднимать шум из-за мелкого сбоя.
  3. Шлёт сообщение в бот заявок только при смене состояния (упал, восстановился),
     без повторов, пока состояние не меняется.
  4. Следит за сроком SSL-сертификата (предупреждения за 14, 5 и 1 день).
  5. Раз в неделю (понедельник утром по Москве) присылает короткую сводку,
     чтобы было видно, что проверка живая.
  6. Подписывает на оповещения тех, кто написал боту кодовое слово; отвечает на «статус».

Переменные окружения:
  TG_TOKEN     токен бота заявок (секрет репозитория, обязательный)
  DRY_RUN=1    ничего не отправлять в Telegram, печатать сообщения в лог
  FORCE_TEST=1 отправить пробное сообщение всем подписчикам
"""
import datetime as dt
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")

SITE_HOST = "xn----7sbbthgmexebj7ccd5c.xn--p1ai"
SITE_NAME = "кузница-контента.рф"
SITE_URL = f"https://{SITE_HOST}/"
# Текст, который точно есть на главной: если его нет, значит открылась заглушка или ошибка.
MARKERS = ["Кузница", "кузница", "Kuznica", "kuznica"]

# Код для подписки на оповещения: в репозитории хранится только его отпечаток.
SUBSCRIBE_CODE_SHA256 = "233f2e78d92adc0bf6b3227e93d3b76cd8c6f0f63a4d60ff04e5a9b75559b8ab"
STATUS_WORDS = {"статус", "статус сайта", "/status", "/start статус"}

CHECK_HOST = "https://check-host.net"
UA = "Mozilla/5.0 (compatible; KuznicaUptime/1.0)"
MSK = dt.timezone(dt.timedelta(hours=3))

SSL_STAGES = [14, 5, 1]  # за сколько дней предупреждать
RECHECK_DELAY = 60       # секунд до перепроверки при сбое

TOKEN = os.environ.get("TG_TOKEN", "").strip()
DRY = os.environ.get("DRY_RUN") == "1"


# ---------------------------------------------------------------- утилиты

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def fmt_msk(ts):
    return dt.datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M")


def fmt_minutes(sec):
    m = max(1, round(sec / 60))
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} ч {m} мин" if m else f"{h} ч"
    d, h = divmod(h, 24)
    return f"{d} д {h} ч"


def http_get(url, timeout=25, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- состояние

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _openssl(args, data):
    env = dict(os.environ, KK_PASS=TOKEN)
    p = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-a", "-A",
                        "-pass", "env:KK_PASS", *args], input=data, capture_output=True, env=env)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode(errors="replace"))
    return p.stdout


def get_chats(st):
    """Список чатов хранится зашифрованным ключом из токена бота: в открытом репозитории их не видно."""
    blob = st.get("chats_enc")
    if not blob or not TOKEN:
        return []
    try:
        return json.loads(_openssl(["-d"], blob.encode() + b"\n").decode())
    except Exception as e:  # noqa: BLE001
        log("не удалось расшифровать список чатов:", e)
        return []


def set_chats(st, chats):
    st["chats_enc"] = _openssl([], json.dumps(sorted(set(chats))).encode()).decode().strip()


# ---------------------------------------------------------------- Telegram

def tg(method, **params):
    if not TOKEN:
        raise RuntimeError("нет TG_TOKEN")
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data,
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": str(e)}


def send(chats, text):
    if DRY or not TOKEN:
        log("[сообщение]", chats and len(chats), "получателей:\n" + text)
        return True
    ok_any = False
    for cid in chats:
        for attempt in range(3):
            r = tg("sendMessage", chat_id=cid, text=text, disable_web_page_preview="true")
            if r.get("ok"):
                ok_any = True
                break
            log("Telegram ответил ошибкой:", r.get("description"))
            time.sleep(3)
    return ok_any


def normalize(text):
    return " ".join(text.strip().lower().replace("ё", "е").split())


def process_updates(st, chats, status_text):
    """Подписка по кодовому слову и ответ на «статус»."""
    if not TOKEN or DRY:
        return chats
    offset = int(st.get("tg_offset", 0))
    r = tg("getUpdates", offset=offset + 1 if offset else 0, timeout=0, allowed_updates='["message"]')
    if not r.get("ok"):
        log("getUpdates:", r.get("description"))  # например, у бота включён вебхук
        return chats
    changed = False
    for upd in r.get("result", []):
        st["tg_offset"] = max(int(st.get("tg_offset", 0)), upd["update_id"])
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        text = normalize(msg.get("text") or "")
        cid = chat.get("id")
        if not cid or not text:
            continue
        if text.startswith("/start "):
            text = text[7:]
        if hashlib.sha256(text.encode()).hexdigest() == SUBSCRIBE_CODE_SHA256:
            if cid not in chats:
                chats.append(cid)
                changed = True
            tg("sendMessage", chat_id=cid, text=(
                f"Готово. Сюда будут приходить оповещения, если сайт {SITE_NAME} перестанет открываться "
                "или станет недоступен из России, и сообщение, когда он снова заработает. "
                "Раз в неделю, в понедельник утром, пришлю короткую сводку. "
                "Напишите «статус», чтобы узнать состояние сайта (ответ в течение 5–10 минут)."))
        elif text in STATUS_WORDS and cid in chats:
            tg("sendMessage", chat_id=cid, text=status_text)
    if changed:
        set_chats(st, chats)
    return chats


# ---------------------------------------------------------------- проверки

def check_direct():
    """Прямая проверка с сервера проверки: код ответа, текст страницы, сертификат."""
    try:
        code, body = http_get(SITE_URL, timeout=25)
        text = body.decode("utf-8", errors="replace")
        if code != 200:
            return False, f"сайт ответил кодом {code}"
        if not any(m in text for m in MARKERS):
            return False, "вместо сайта открывается пустая страница или заглушка"
        return True, "ok"
    except urllib.error.HTTPError as e:
        return False, f"сайт ответил кодом {e.code}"
    except ssl.SSLError as e:
        return False, f"ошибка сертификата безопасности ({e.reason or e})"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return False, "сертификат безопасности недействителен"
        if isinstance(reason, socket.gaierror):
            return False, "адрес сайта не находится (проблема с доменом или DNS)"
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return False, "сайт не отвечает дольше 25 секунд"
        return False, f"не удалось подключиться ({reason})"
    except (socket.timeout, TimeoutError):
        return False, "сайт не отвечает дольше 25 секунд"
    except Exception as e:  # noqa: BLE001
        return False, f"ошибка проверки ({e})"


def ru_nodes():
    try:
        _, body = http_get(f"{CHECK_HOST}/nodes/hosts", headers={"Accept": "application/json"})
        nodes = json.loads(body)["nodes"]
        ru = sorted(k for k, v in nodes.items() if v.get("location", [""])[0] == "ru")
        return ru or ["ru1.node.check-host.net", "ru2.node.check-host.net", "ru3.node.check-host.net"]
    except Exception as e:  # noqa: BLE001
        log("список узлов недоступен:", e)
        return ["ru1.node.check-host.net", "ru2.node.check-host.net", "ru3.node.check-host.net"]


def _run_check_http(url, nodes):
    """Запускает проверку адреса с узлов и ждёт результат. Возвращает (ответы узлов, города)."""
    q = urllib.parse.urlencode([("host", url), ("max_nodes", str(len(nodes)))] + [("node", n) for n in nodes])
    req = {}
    for attempt in range(4):
        _, body = http_get(f"{CHECK_HOST}/check-http?{q}", headers={"Accept": "application/json"})
        req = json.loads(body)
        if "request_id" in req:
            break
        log("check-host:", req.get("error"), "- жду 25 с")
        time.sleep(25)
    if "request_id" not in req:
        raise RuntimeError(req.get("error") or "нет ответа")
    rid = req["request_id"]
    names = {k: v[2] for k, v in req.get("nodes", {}).items()}
    res = {}
    for _ in range(12):
        time.sleep(5)
        try:
            _, body = http_get(f"{CHECK_HOST}/check-result/{rid}", headers={"Accept": "application/json"})
            res = json.loads(body)
        except Exception as e:  # noqa: BLE001
            log("результат пока недоступен:", e)
            continue
        if res and all(v is not None for v in res.values()):
            break
    out = {}
    for node, v in res.items():
        if v is None:
            continue
        row = v[0] if v and isinstance(v[0], list) else None
        ok = bool(row and row[0] == 1)
        why = None if ok else ((row[2] if row and len(row) > 2 else None) or "нет ответа")
        out[node] = (ok, why)
    return out, names


def check_russia():
    """Проверка с узлов в России. Возвращает (ok|None, сводка, детали). None — узлы не дали надёжного ответа."""
    nodes = ru_nodes()
    try:
        site, names = _run_check_http(SITE_URL, nodes)
    except Exception as e:  # noqa: BLE001
        return None, f"узлы в России недоступны ({e})", {}
    if not site:
        return None, "узлы в России не ответили", {}
    bad = [n for n, (ok, _) in site.items() if not ok]
    ok_n = len(site) - len(bad)
    if bad:
        # Контроль: если с этих же узлов не открывается и Яндекс, сбой у самих узлов, а не у сайта.
        try:
            ctrl, _ = _run_check_http("https://ya.ru/", bad)
            bad = [n for n in bad if ctrl.get(n, (True, None))[0]]
        except Exception as e:  # noqa: BLE001
            log("контрольная проверка не удалась, оставляю результат как есть:", e)
    done = ok_n + len(bad)
    if done == 0:
        return None, "узлы в России сейчас неисправны", {}
    det = {"bad": [f"{names.get(n, n)}: {site[n][1]}" for n in bad]}
    # Недоступен из России, если не открылось минимум с двух исправных узлов и с большинства из них.
    is_ok = not (len(bad) >= 2 and len(bad) > ok_n)
    return is_ok, f"открылось с {ok_n} из {done} узлов в России", det


def ssl_days_left():
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((SITE_HOST, 443), timeout=20) as s:
            with ctx.wrap_socket(s, server_hostname=SITE_HOST) as ss:
                cert = ss.getpeercert()
        exp = dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), dt.timezone.utc)
        return (exp - now_utc()).total_seconds() / 86400, exp
    except Exception as e:  # noqa: BLE001
        log("срок сертификата не получен:", e)
        return None, None


def evaluate():
    d_ok, d_why = check_direct()
    r_ok, r_sum, r_det = check_russia()
    log(f"напрямую: {d_ok} ({d_why}); Россия: {r_ok} ({r_sum}) {r_det}")
    if r_ok is False and d_ok:
        return "ru_down", d_why, r_sum, r_det
    if r_ok is False and not d_ok:
        return "down", d_why, r_sum, r_det
    if r_ok is None and not d_ok:
        return "down", d_why, r_sum, r_det
    # r_ok True: из России открывается, даже если у сервера проверки сбой сети
    return "up", d_why, r_sum, r_det


# ---------------------------------------------------------------- тексты

def text_down(kind, why, r_sum, det, ts):
    lines = []
    if kind == "down":
        lines.append(f"🔴 Сайт {SITE_NAME} не открывается")
        lines.append(f"Причина: {why}.")
    else:
        lines.append(f"🟠 Сайт {SITE_NAME} не открывается из России")
        lines.append("С других стран сайт работает, значит проблема в сети или блокировке на пути к российским пользователям.")
    if r_sum and r_sum.startswith("открылось"):
        lines.append(f"Проверка из России: {r_sum}.")
    lines.append(f"Замечено: {fmt_msk(ts)} по Москве, перепроверено через минуту.")
    lines.append("Что сделать: открыть сайт с телефона без VPN; если не открывается, написать в поддержку Timeweb (чат в панели hosting.timeweb.ru).")
    lines.append("Когда сайт заработает, придёт сообщение.")
    return "\n".join(lines)


def text_up(since, ts):
    return (f"🟢 Сайт {SITE_NAME} снова работает\n"
            f"Был недоступен {fmt_minutes(ts - since)} (с {fmt_msk(since)} до {fmt_msk(ts)} по Москве).")


def text_status(st):
    s = st.get("status", "up")
    days = st.get("ssl_days")
    ssl_line = f"\nСертификат безопасности действует ещё {int(days)} дн." if days is not None else ""
    last = fmt_msk(st["last_run"]) if st.get("last_run") else "—"
    if s == "up":
        return f"🟢 Сайт {SITE_NAME} работает, из России открывается. Последняя проверка: {last} МСК.{ssl_line}"
    since = fmt_msk(st.get("since", time.time()))
    head = "🔴 Сайт не открывается" if s == "down" else "🟠 Сайт не открывается из России"
    return f"{head} с {since} МСК. Последняя проверка: {last} МСК.{ssl_line}"


# ---------------------------------------------------------------- основной ход

def main():
    st = load_state()
    before = json.dumps(st, sort_keys=True)
    chats = get_chats(st)
    ts = time.time()

    kind, why, r_sum, det = evaluate()
    if kind != "up":
        log(f"сбой ({kind}), перепроверка через {RECHECK_DELAY} с")
        time.sleep(RECHECK_DELAY)
        kind, why, r_sum, det = evaluate()

    prev = st.get("status", "up")
    if kind != prev:
        if kind == "up":
            since = st.get("since", ts)
            send(chats, text_up(since, ts))
            inc = st.setdefault("incidents", [])
            inc.append({"from": round(since), "to": round(ts), "kind": prev})
            st["incidents"] = [i for i in inc if i["to"] > ts - 35 * 86400]
            st.pop("since", None)
        else:
            if prev == "up":
                st["since"] = round(ts)
            send(chats, text_down(kind, why, r_sum, det, ts))
        st["status"] = kind

    # срок SSL (раз в 6 часов достаточно)
    if ts - st.get("ssl_checked", 0) > 6 * 3600:
        days, exp = ssl_days_left()
        if days is not None:
            st["ssl_checked"] = round(ts)
            st["ssl_days"] = round(days, 1)
            stage = st.get("ssl_stage")
            if days > SSL_STAGES[0] + 5:
                st.pop("ssl_stage", None)
            else:
                for s in SSL_STAGES:
                    if days <= s and (stage is None or s < stage):
                        send(chats, f"⚠️ Сертификат безопасности сайта {SITE_NAME} заканчивается через "
                                    f"{max(0, int(days))} дн. ({exp.astimezone(MSK):%d.%m.%Y}). Обычно Timeweb продлевает его сам; "
                                    "если через пару дней сообщение повторится, нажмите «Продлить» в разделе SSL "
                                    "панели hosting.timeweb.ru или напишите в поддержку Timeweb.")
                        st["ssl_stage"] = s
                        stage = s

    # недельная сводка: понедельник, после 10:00 МСК
    msk = dt.datetime.fromtimestamp(ts, MSK)
    if msk.weekday() == 0 and msk.hour >= 10 and ts - st.get("weekly_sent", 0) > 5 * 86400:
        week = [i for i in st.get("incidents", []) if i["to"] > ts - 7 * 86400]
        down_sec = sum(i["to"] - max(i["from"], ts - 7 * 86400) for i in week)
        if st.get("status", "up") != "up":
            down_sec += ts - max(st.get("since", ts), ts - 7 * 86400)
        if not week and st.get("status", "up") == "up":
            body = "за неделю сбоев не было"
        else:
            body = f"сбоев за неделю: {len(week)}, сайт был недоступен в сумме {fmt_minutes(down_sec)}"
        ssl_line = f" Сертификат безопасности действует ещё {int(st['ssl_days'])} дн." if st.get("ssl_days") else ""
        send(chats, f"📋 Проверка сайта {SITE_NAME} работает: {body}.{ssl_line}")
        st["weekly_sent"] = round(ts)

    st["last_run"] = round(ts)
    chats = process_updates(st, chats, text_status(st))

    if os.environ.get("FORCE_TEST") == "1":
        send(chats, f"✅ Пробное сообщение. Проверка сайта {SITE_NAME} настроена: раз в 5 минут, "
                    "с узлов в Москве и Петербурге. Если сайт упадёт или перестанет открываться из России, "
                    "сообщение придёт сюда, а когда заработает — придёт ещё одно.")


    # last_run не сохраняем отдельно, чтобы не делать запись в репозиторий каждые 5 минут
    cmp = dict(st)
    cmp.pop("last_run", None)
    old = json.loads(before)
    old.pop("last_run", None)
    if json.dumps(cmp, sort_keys=True) != json.dumps(old, sort_keys=True):
        save_state(st)
        log("состояние изменилось, сохранено")
    else:
        log("без изменений")


if __name__ == "__main__":
    sys.exit(main())
