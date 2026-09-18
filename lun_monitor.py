"""
Paid Listing Rate Auto-Bidder + Representative-Status Monitor
================================================================

Продовжує ідею попередньої версії (яка тільки СПОВІЩАЛА про втрату
представництва), але тепер сам ВИПРАВЛЯЄ ситуацію:

    Для кожного ПЛАТНОГО оголошення (dailyCost > 0), яке НЕ є
    представником:
        1. Питає в API поточну ставку і "representabilityRate" -
           скільки монет потрібно, щоб стати представником прямо
           зараз (сайт сам рахує це число, орієнтуючись на ставку
           конкурента).
        2. Якщо representabilityRate == 0 - боротись марно (найчастіше
           причина: конкурент підключив фіксоване представництво
           "ЛУН ТОП", і жодна ставка це не перебʼє) - ставка
           скидається до 0, не витрачаючи монети даремно.
        3. Інакше - ставка піднімається до representabilityRate + 1,
           але НЕ ВИЩЕ MAX_RATE_CEILING (захист від нескінченної
           цінової гонки з конкурентом, який задирає ставку занадто
           високо).
        4. Якщо навіть на стелі representabilityRate все одно вищий -
           ставка виставляється рівно на стелю (це максимум, який ми
           готові платити), і в сповіщенні окремо позначається, що
           представництва так і не досягнуто.

    Оголошення, де представництво вже утримується, НЕ чіпаються
    (ставка не знижується автоматично, навіть якщо є запас).

    Оголошення з dailyCost == 0 (сам не поставив ставку) теж НЕ
    чіпаються - користувач свідомо не бере участі в гонці за
    представництво для них.

СПОВІЩЕННЯ В TELEGRAM надсилається на кожному запуску, де відбулась
хоч одна зміна ставки - зі списком "було X -> стало Y" і причиною
(конкурент тримає N / конкурент підключив ЛУН ТОП / досягнуто стелі
й представництва все одно нема).

НАЛАШТУВАННЯ (обов'язково заповни перед запуском - через GitHub
Secrets, а НЕ прямо в цьому файлі, якщо репозиторій публічний):
    1. SITE_COOKIE     - рядок cookies з твого браузера
    2. TELEGRAM_BOT_TOKEN - токен твого Telegram-бота
    3. TELEGRAM_CHAT_ID   - твій chat_id (кому надсилати повідомлення)
    4. SITE_BASE_URL      - базовий домен API, напр. https://example.com
    5. SITE_CABINET_URL   - домен кабінету, напр. https://my.example.com

ЯК ОТРИМАТИ SITE_COOKIE:
    1. Зайди у свій кабінет під власним акаунтом.
    2. Відкрий DevTools (F12) -> вкладка Network -> Fetch/XHR.
    3. Онови сторінку, клікни на будь-який запит до API
       (наприклад "list/" або "users/info/").
    4. У вкладці Headers знайди розділ "Request Headers" -> "cookie".
    5. Скопіюй ВЕСЬ рядок cookie (він довгий, це нормально) і встав
       його як значення секрету SITE_COOKIE.

    !! Це чутливі дані, що дають доступ до твого акаунту.
       Нікому їх не показуй і не публікуй в репозиторіях.

ДВА РЕЖИМИ ЗАПУСКУ:
    1) Локально, для тесту (постійний цикл, працює поки відкритий термінал):
        pip install -r requirements.txt --break-system-packages
        python3 lun_monitor.py

    2) Одноразово (для GitHub Actions чи Windows Task Scheduler):
        python3 lun_monitor.py --once

У режимі --once усі секретні дані беруться зі змінних середовища.
"""

import json
import os
import sys
import time
import logging

import requests

# ============================================================
# НАЛАШТУВАННЯ
# ============================================================

SITE_COOKIE = os.environ.get("SITE_COOKIE", "PASTE_YOUR_COOKIE_STRING_HERE").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE").strip()

SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "PASTE_YOUR_SITE_BASE_URL_HERE").strip()
SITE_CABINET_URL = os.environ.get("SITE_CABINET_URL", "PASTE_YOUR_SITE_CABINET_URL_HERE").strip()

CHECK_INTERVAL_MINUTES = 15

RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10

CONSECUTIVE_FAILURES_BEFORE_NOTIFY = 2

# Максимальна ставка, вище якої скрипт НЕ намагається перебити
# конкурента - захист від цінової гонки, що виходить з-під контролю.
MAX_RATE_CEILING = 60

# Причина блокування ЛУН ТОП, яка означає "конкурент підключив
# фіксоване представництво - жодна ставка це не перебʼє".
LUN_TOP_BLOCKED_REASON = "group_has_a_promoted_realty"

ERROR_STATE_FILE = "monitor_error_state.json"

OFFERS_LIST_PARAMS = {
    "page": 1,
    "limit": 100,
    "status": 10,
    "mode": 10,
}

# ============================================================
# Технічна частина
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("lun_monitor")

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "uk-UA",
    "origin": SITE_CABINET_URL,
    "referer": f"{SITE_CABINET_URL}/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
}

REPRESENTABILITY_OK = 1


def fetch_offers() -> list[dict]:
    """Повертає ПОВНИЙ список оголошень, проходячи всі сторінки пагінації."""
    headers = dict(HEADERS)
    headers["cookie"] = SITE_COOKIE

    all_items: list[dict] = []
    page = 1

    while True:
        params = dict(OFFERS_LIST_PARAMS)
        params["page"] = page

        response = requests.get(
            f"{SITE_BASE_URL}/api/offers/list/",
            params=params,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()
        if payload.get("status") != "OK":
            raise RuntimeError(f"Неочікувана відповідь API: {payload}")

        data = payload["data"]
        all_items.extend(data["items"])

        pagination = data.get("pagination", {})
        page_count = pagination.get("pageCount", 1)

        log.info(
            "Завантажено сторінку %s з %s (%s оголошень на ній).",
            page,
            page_count,
            len(data["items"]),
        )

        if page >= page_count:
            break
        page += 1

    return all_items


def fetch_pickup_info(offer_id: int) -> dict:
    """
    Дістає деталі ставки/представництва для одного оголошення:
    поточну ставку (data['form']['rate']) і потрібну для
    представництва (data['representabilityRate']), плюс причину,
    якщо ЛУН ТОП недоступний (data['isLunTopPublicationDisabledReason']).
    """
    headers = dict(HEADERS)
    headers["cookie"] = SITE_COOKIE

    response = requests.get(
        f"{SITE_BASE_URL}/api/offers/pickup-item/{offer_id}/",
        params={"id": offer_id, "place": 0},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    if payload.get("status") != "OK":
        raise RuntimeError(f"Неочікувана відповідь pickup-item API: {payload}")

    return payload["data"]


def set_pickup_rate(offer_id: int, rate: int) -> None:
    """Виставляє нову ставку для оголошення."""
    headers = dict(HEADERS)
    headers["cookie"] = SITE_COOKIE
    headers["content-type"] = "application/json"

    body = {
        "rate": rate,
        "autoupdate": 0,
        "autoupdateTime": "09:00:00",
        "birdPremium": 0,
        "lunTop": 0,
        "quickly": 0,
        "itemType": None,
        "operType": None,
        "cityId": None,
    }
    response = requests.put(
        f"{SITE_BASE_URL}/api/offers/pickup-item/{offer_id}/",
        json=body,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()
    if result.get("status") != "OK":
        raise RuntimeError(f"Не вдалось встановити ставку {rate} для {offer_id}: {result}")


def send_telegram_message(text: str) -> None:
    """Надсилає повідомлення в Telegram через Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Не вдалось надіслати повідомлення в Telegram: %s", exc)


def format_offer_line(offer: dict) -> str:
    return f"{offer['id']} {offer['address']} ({offer['price']})"


def is_paid_offer(offer: dict) -> bool:
    """Платне оголошення визначаємо по dailyCost > 0."""
    return (offer.get("dailyCost") or 0) > 0


def decide_new_rate(current_rate: int, representability_rate: int) -> int:
    """
    Рахує, яку ставку виставити:
    - representability_rate == 0 -> 0 (боротись марно/нема сенсу)
    - інакше -> representability_rate + 1, але не вище MAX_RATE_CEILING
    """
    if representability_rate <= 0:
        return 0
    return min(representability_rate + 1, MAX_RATE_CEILING)


def check_once() -> list[dict]:
    """
    Один прохід перевірки й автоматичного виправлення ставок.

    Повертає список змін, які відбулись (або мали б відбутись, але
    вперлись у стелю) - кожен елемент:
        {"offer": ..., "old_rate": ..., "new_rate": ...,
         "representability_rate": ..., "reason": ..., "hit_ceiling": bool}
    """
    offers = fetch_offers()
    changes = []

    for offer in offers:
        if not is_paid_offer(offer):
            log.info("%s -> (безкоштовне, пропускаємо)", format_offer_line(offer))
            continue

        if offer.get("representability") == REPRESENTABILITY_OK:
            log.info("%s -> ✅ представник, не чіпаємо", format_offer_line(offer))
            continue

        offer_id = offer["id"]
        info = fetch_pickup_info(offer_id)

        current_rate = info["form"]["rate"]
        representability_rate = info.get("representabilityRate", 0)
        disabled_reason = info.get("isLunTopPublicationDisabledReason")

        new_rate = decide_new_rate(current_rate, representability_rate)

        if disabled_reason == LUN_TOP_BLOCKED_REASON:
            reason = "конкурент підключив ЛУН ТОП — представництво недосяжне ставкою"
        elif representability_rate <= 0:
            reason = "немає конкуренції за представництво"
        else:
            reason = f"конкурент тримає ставку {representability_rate}"

        hit_ceiling = representability_rate > MAX_RATE_CEILING

        if new_rate != current_rate:
            set_pickup_rate(offer_id, new_rate)
            log.info(
                "%s -> ставку змінено: %s -> %s (%s)",
                format_offer_line(offer), current_rate, new_rate, reason,
            )
            changes.append({
                "offer": offer,
                "old_rate": current_rate,
                "new_rate": new_rate,
                "representability_rate": representability_rate,
                "reason": reason,
                "hit_ceiling": hit_ceiling,
            })
        else:
            log.info(
                "%s -> ставка вже %s, зміна не потрібна (%s)",
                format_offer_line(offer), current_rate, reason,
            )

    return changes


def send_rate_changes_notification(changes: list[dict]) -> None:
    """Формує і надсилає повідомлення про зміни ставок."""
    lines = ["📊 <b>Автоматична зміна ставок:</b>", ""]

    for change in changes:
        offer = change["offer"]
        aggregator_url = offer.get("lunUrl", "")
        ceiling_note = " ⚠️ стеля 60, представництва все одно нема" if change["hit_ceiling"] else ""
        lines.append(
            f"🔁 {format_offer_line(offer)}\n"
            f"Ставка: {change['old_rate']} → {change['new_rate']} "
            f"({change['reason']}){ceiling_note}\n"
            f"<a href=\"{aggregator_url}\">Відкрити оголошення</a>"
        )

    message = "\n\n".join(lines)
    send_telegram_message(message)
    log.warning("Надіслано сповіщення про %s змінених ставок.", len(changes))


def load_state() -> dict:
    try:
        with open(ERROR_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    try:
        with open(ERROR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as exc:
        log.warning("Не вдалось зберегти файл стану: %s", exc)


def load_consecutive_failures() -> int:
    return load_state().get("consecutive_failures", 0)


def save_consecutive_failures(count: int) -> None:
    save_state({"consecutive_failures": count})


def check_once_with_retry() -> list[dict]:
    last_exception: Exception | None = None

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return check_once()
        except Exception as exc:  # noqa: BLE001
            last_exception = exc
            if attempt < RETRY_ATTEMPTS:
                log.warning(
                    "Спроба %s з %s невдала (%s). Повторюю через %s сек...",
                    attempt, RETRY_ATTEMPTS, exc, RETRY_DELAY_SECONDS,
                )
                time.sleep(RETRY_DELAY_SECONDS)

    raise last_exception


def notify_error(message: str) -> None:
    log.error(message)
    send_telegram_message(f"🔴 <b>Помилка моніторингу оголошень</b>\n{message}")


def _check_config() -> bool:
    placeholders_present = any(
        "PASTE_YOUR" in value
        for value in (SITE_COOKIE, TELEGRAM_BOT_TOKEN, SITE_BASE_URL, SITE_CABINET_URL)
    )
    if placeholders_present:
        log.error(
            "Секрети не задані! Заповни SITE_COOKIE, TELEGRAM_BOT_TOKEN, "
            "TELEGRAM_CHAT_ID, SITE_BASE_URL, SITE_CABINET_URL."
        )
        return False
    return True


def _describe_exception(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status in (401, 403):
            return (
                f"Сайт-джерело відповів помилкою авторизації (HTTP {status}).\n"
                "Найімовірніша причина - протух SITE_COOKIE. Онови секрет SITE_COOKIE."
            )
        return f"Сайт-джерело відповів помилкою HTTP {status}."

    if isinstance(exc, requests.RequestException):
        return f"Не вдалось з'єднатися з сайтом-джерелом: {exc}"

    return f"Неочікувана помилка в скрипті: {exc}"


def run_once() -> None:
    if not _check_config():
        sys.exit(1)

    try:
        changes = check_once_with_retry()
        log.info("Перевірка завершена успішно.")

        if changes:
            send_rate_changes_notification(changes)

        if load_consecutive_failures() > 0:
            log.info("Проблема з попередніх запусків зникла, скидаю лічильник помилок.")
        save_consecutive_failures(0)

    except Exception as exc:  # noqa: BLE001
        failures = load_consecutive_failures() + 1
        save_consecutive_failures(failures)

        if failures >= CONSECUTIVE_FAILURES_BEFORE_NOTIFY:
            notify_error(f"{_describe_exception(exc)}\n\n(Це вже {failures}-й запуск поспіль з помилкою.)")
        else:
            log.warning(
                "Запуск невдалий (%s з %s поспіль перед сповіщенням): %s",
                failures, CONSECUTIVE_FAILURES_BEFORE_NOTIFY, exc,
            )

        sys.exit(1)


def run_forever() -> None:
    if not _check_config():
        return

    log.info("Старт моніторингу. Перевірка кожні %s хв. Ctrl+C для зупинки.", CHECK_INTERVAL_MINUTES)

    while True:
        try:
            changes = check_once_with_retry()
            if changes:
                send_rate_changes_notification(changes)
        except Exception as exc:  # noqa: BLE001
            notify_error(_describe_exception(exc))

        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
