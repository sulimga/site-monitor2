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
        3. Інакше - ставка піднімається до representability_rate + 1.
        4. Якщо навіть representability_rate конкурента вже >= MAX_RATE_CEILING
           (за замовчуванням 60) - боротись занадто дорого, тому ставка
           теж скидається до 0, а НЕ виставляється на стелю (платити
           максимум і все одно програвати немає сенсу).

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

CHECK_INTERVAL_MINUTES = 5

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


def set_pickup_rate(offer_id: int, rate: int) -> dict:
    """Виставляє нову ставку для оголошення. Повертає розібраний JSON
    відповіді (навіть у разі помилки status=ERROR) — виклик сам вирішує,
    чи це критична помилка, чи очікуваний відомий сценарій (напр. заборона
    опускати ставку нижче базової)."""
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
    return response.json()


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
    """
    Платне оголошення (тобто таке, де користувач сам вирішив брати участь
    у боротьбі за представництво) визначаємо по rate > 0 — це саме той
    показник, який видно і в кабінеті, і його ж ми самі змінюємо.
    (dailyCost НЕ підходить для цього — трапляються оголошення з rate > 0,
    де dailyCost чомусь показує 0, тож орієнтуємось саме на rate.)
    """
    return (offer.get("rate") or 0) > 0


def get_max_ceiling(item_type_id: int, oper_type_id: int) -> int:
    """
    Стеля ставки — вище якої здаємось незалежно від того, що тримає
    конкурент. Різні категорії мають кардинально різні масштаби цін:
    - квартири (будь-яка угода) - дуже конкурентний сегмент, стеля 1380
    - будинки на ПРОДАЖ - теж високі ставки, стеля 1060
    - усе інше (комерція, оренда будинків, земля, і все не уточнене) - 60
    """
    if item_type_id == 1:  # квартира
        return 1380
    if item_type_id == 3 and oper_type_id == 1:  # будинок, продаж
        return 1060
    return MAX_RATE_CEILING  # комерція / оренда будинків / земля / дефолт


def is_zero_rate_allowed(item_type_id: int, oper_type_id: int) -> bool:
    """
    Чи можна для цієї категорії взагалі поставити ставку 0.
    Ні для: квартир (будь-яка угода) і будинків на ПРОДАЖ.
    Так для всього іншого (комерція, оренда будинків, земля тощо).
    """
    if item_type_id == 1:  # квартира
        return False
    if item_type_id == 3 and oper_type_id == 1:  # будинок, продаж
        return False
    return True


def decide_new_rate(current_rate: int, representability_rate: int, min_allowed_rate: int,
                     max_ceiling: int, zero_allowed: bool, lun_top_blocked: bool):
    """
    Рахує, яку ставку виставити. Повертає число, АБО None, якщо ставку
    взагалі не чіпаємо (нема причини ані піднімати, ані здаватись).

    Раз користувач сам поставив ставку > 0 — він увійшов у гонку за
    представництво і залишається в ній, ПОКИ САМ не вирішить інакше.
    "Здатись" (опустити ставку) можна лише з двох конкретних причин:
        - конкурент підключив ЛУН ТОП (представництво взагалі недосяжне)
        - конкурент тримає ставку на/понад стелею категорії (задорого)
    "Немає конкурентів зараз" — це НЕ причина знижувати ставку: завтра
    конкурент може з'явитись знову, і ми не хочемо втратити представництво
    в проміжку між перевірками. (Окрема логіка "здешевлення раз на добу,
    коли конкурентів нема" винесена в окремий скрипт lun_monitor_night.py.)
    """
    too_expensive = representability_rate >= max_ceiling
    give_up = lun_top_blocked or too_expensive

    if give_up:
        return min_allowed_rate if zero_allowed else None

    if representability_rate <= 0:
        return None  # нема конкурентів -> тримаємо поточну ставку, нічого не міняємо

    return max(representability_rate + 1, min_allowed_rate)


def check_once() -> list[dict]:
    """
    Один прохід перевірки й автоматичного виправлення ставок.

    Повертає список змін (або заблокованих спроб) - кожен елемент:
        {"offer": ..., "old_rate": ..., "new_rate": ...,
         "representability_rate": ..., "reason": ..., "blocked": bool}
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
        item_type_id = offer.get("itemTypeId")
        oper_type_id = offer.get("operTypeId")
        max_ceiling = get_max_ceiling(item_type_id, oper_type_id)
        zero_allowed = is_zero_rate_allowed(item_type_id, oper_type_id)

        info = fetch_pickup_info(offer_id)

        current_rate = info["form"]["rate"]

        # Авторитетна перевірка: поле "rate" у списку (яким ми фільтрували
        # вище) інколи буває застарілим/іншим за реальну ставку з
        # pickup-item. Якщо тут виявляється, що насправді 0 — користувач
        # НЕ бере участі в аукціоні, і ми НЕ ЧІПАЄМО це оголошення, хай
        # там що показав список.
        if current_rate <= 0:
            log.info(
                "%s -> насправді ставка 0 (список показав інше) — не чіпаємо",
                format_offer_line(offer),
            )
            continue

        representability_rate = info.get("representabilityRate", 0)
        disabled_reason = info.get("isLunTopPublicationDisabledReason")
        min_allowed_rate = info.get("params", {}).get("minRentaRate", 0) or 0

        lun_top_blocked = disabled_reason == LUN_TOP_BLOCKED_REASON
        new_rate = decide_new_rate(current_rate, representability_rate, min_allowed_rate,
                                    max_ceiling, zero_allowed, lun_top_blocked)

        give_up = lun_top_blocked or representability_rate >= max_ceiling

        if lun_top_blocked:
            reason = "конкурент підключив ЛУН ТОП — представництво недосяжне ставкою"
        elif representability_rate >= max_ceiling:
            reason = f"конкурент тримає {representability_rate} (≥{max_ceiling}) — здаємось, це занадто дорого"
        elif representability_rate <= 0:
            reason = "немає конкуренції за представництво"
        else:
            reason = f"конкурент тримає ставку {representability_rate}"

        if new_rate is None:
            if not give_up:
                # немає конкурентів, і здаватись не треба -> тримаємо поточну
                # ставку мовчки, це нормальний, непомітний стан, не сповіщення
                log.info(
                    "%s -> ставка лишається %s (%s)",
                    format_offer_line(offer), current_rate, reason,
                )
                continue
            # здаємось, але категорія забороняє ставку 0 -> нічого не міняємо,
            # тільки повідомляємо (і будемо повідомляти знову щоразу, поки
            # ситуація не зміниться)
            blocked_reason = reason + " — для цієї категорії ставка 0 заборонена, тому лишаю поточну і нічого не змінюю"
            log.info("%s -> %s", format_offer_line(offer), blocked_reason)
            changes.append({
                "offer": offer,
                "old_rate": current_rate,
                "new_rate": current_rate,
                "representability_rate": representability_rate,
                "reason": blocked_reason,
                "blocked": True,
            })
            continue

        if new_rate == current_rate:
            log.info(
                "%s -> ставка вже %s, зміна не потрібна (%s)",
                format_offer_line(offer), current_rate, reason,
            )
            continue

        result = set_pickup_rate(offer_id, new_rate)

        if result.get("status") != "OK":
            # Категорія не дозволяє опустити ставку нижче за (щойно розрахований)
            # мінімум — теоретично не мало б статись, раз ми й так орієнтуємось
            # на minRentaRate, але лишаємо як запобіжник: не падаємо, а
            # повідомляємо і лишаємо ставку без змін. Наступний прогін
            # спробує знову і знову сповістить, поки ситуація не зміниться.
            blocked_reason = reason + f" — сервер відхилив ставку {new_rate}, лишаю поточну ({result.get('error')})"
            log.info("%s -> %s", format_offer_line(offer), blocked_reason)
            changes.append({
                "offer": offer,
                "old_rate": current_rate,
                "new_rate": current_rate,
                "representability_rate": representability_rate,
                "reason": blocked_reason,
                "blocked": True,
            })
            continue

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
            "blocked": False,
        })

    return changes


def send_rate_changes_notification(changes: list[dict]) -> None:
    """Формує і надсилає повідомлення про зміни ставок (і про заблоковані спроби)."""
    lines = ["📊 <b>Автоматична зміна ставок:</b>", ""]

    for change in changes:
        offer = change["offer"]
        aggregator_url = offer.get("lunUrl", "")
        if change.get("blocked"):
            icon = "⛔"
            rate_line = f"Ставка залишається {change['old_rate']} монет (не вдалось знизити)"
        else:
            icon = "🔁"
            rate_line = f"Ставка: {change['old_rate']} → {change['new_rate']}"
        lines.append(
            f"{icon} {format_offer_line(offer)}\n"
            f"{rate_line} ({change['reason']})\n"
            f"<a href=\"{aggregator_url}\">Відкрити оголошення</a>"
        )

    message = "\n\n".join(lines)
    send_telegram_message(message)
    log.warning("Надіслано сповіщення про %s змінених/заблокованих ставок.", len(changes))


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
