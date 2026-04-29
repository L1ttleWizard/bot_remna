"""Чистые форматтеры/утилиты, не зависящие от aiogram/БД/панели.

Сюда вынесены отображательные функции и парсеры, которые раньше жили в bot.py
и многократно использовались разными хендлерами.
"""
import html
import re
from datetime import datetime, timezone
from typing import Optional


# --- HWID limits ---

# Лимит устройств по HWID: значение по умолчанию при создании и если панель вернула null
DEFAULT_HWID_DEVICE_LIMIT = 3
# Верхняя граница при +1 / +3 (числовой лимит)
MAX_HWID_INCREMENT_CAP = 9999
# Значение «практически без лимита» (отображается как ♾)
HWID_UNLIMITED_SENTINEL = 9_999_999


def effective_hwid_limit(api_data: dict) -> int:
    v = api_data.get("hwidDeviceLimit")
    if v is None:
        return DEFAULT_HWID_DEVICE_LIMIT
    return int(v)


def is_hwid_unlimited(api_data: dict) -> bool:
    v = api_data.get("hwidDeviceLimit")
    if v is None:
        return False
    return int(v) >= HWID_UNLIMITED_SENTINEL


def hwid_limit_caption(api_data: dict) -> str:
    v = api_data.get("hwidDeviceLimit")
    if v is None:
        return str(DEFAULT_HWID_DEVICE_LIMIT)
    vi = int(v)
    if vi >= HWID_UNLIMITED_SENTINEL:
        return "♾ без лимита"
    return str(vi)


# --- Bytes / traffic ---

def human_bytes(n: int) -> str:
    n = max(0, int(n))
    for div, name in ((1 << 30, "ГБ"), (1 << 20, "МБ"), (1 << 10, "КБ")):
        if n >= div:
            x = n / div
            s = f"{x:.2f}".rstrip("0").rstrip(".")
            return f"{s} {name}"
    return f"{n} Б"


def traffic_summary_markdown(api_data: dict) -> str:
    ut = api_data.get("userTraffic") or {}
    used = int(ut.get("usedTrafficBytes") or 0)
    life = int(ut.get("lifetimeUsedTrafficBytes") or 0)
    tlim = api_data.get("trafficLimitBytes")
    lim_txt = "без лимита"
    if tlim is not None and int(tlim) > 0:
        lim_txt = human_bytes(int(tlim))
    return (
        f"**Использовано (период):** {human_bytes(used)}\n"
        f"**За всё время:** {human_bytes(life)}\n"
        f"**Лимит трафика:** {lim_txt}"
    )


# --- Dates ---

def format_expire_display(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "—"
    s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def parse_expire_to_ts(value: Optional[str]) -> int:
    """ISO-строка expireAt → unix timestamp (UTC). 0 если пусто/некорректно."""
    if not value:
        return 0
    try:
        s = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


# --- Devices ---

def sort_hwid_devices(devices: list) -> list:
    return sorted(devices or [], key=lambda x: (x.get("createdAt") or ""))


def format_devices_html(devices: list, limit_label: str) -> str:
    header = f"📱 <b>Устройства</b> (лимит HWID: {html.escape(limit_label)})"
    if not devices:
        return (
            header
            + "\n\nСписок пуст. Устройства появятся после подключения клиента "
              "с поддержкой HWID (Happ, v2RayTun и др.)."
        )
    blocks = [header]
    for i, d in enumerate(devices):
        pl = html.escape(str(d.get("platform") or "—"))
        model = html.escape(str(d.get("deviceModel") or "—"))
        os_ver = d.get("osVersion")
        os_part = f"\n   ОС: {html.escape(str(os_ver))}" if os_ver else ""
        blocks.append(f"\n\n<b>{i + 1}.</b> {model}{os_part}\n   Платформа: {pl}")
    return "".join(blocks)


# --- Subscription captions ---

def format_sub_caption(sub: tuple) -> str:
    """Читаемое название подписки из (id, uuid, short_uuid, username, expire_date, label, created_at)."""
    sid, _uuid, _short, username, expire_date, label, _created = sub
    if label:
        head = label
    elif username:
        head = username
    else:
        head = f"#{sid}"
    if expire_date:
        ts = datetime.fromtimestamp(int(expire_date)).strftime("%d.%m.%Y")
        return f"#{sid} · {head} · до {ts}"
    return f"#{sid} · {head}"


# --- TG profile ---

def format_tg_name(
    tg_username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
) -> str:
    """Человекочитаемое имя из TG-полей. '—' если ничего не известно."""
    parts: list[str] = []
    full = " ".join(p for p in (first_name, last_name) if p)
    if full:
        parts.append(full)
    if tg_username:
        parts.append(f"@{tg_username}")
    return " · ".join(parts) if parts else "—"


# --- Remnawave panel username generator ---

USERNAME_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_]")
REMNAWAVE_USERNAME_MAX_LEN = 32

# Regex для парсинга tg_<id>[_<suffix>] из Remnawave username (используется в /import_users).
TG_USERNAME_RE = re.compile(r"^tg_(\d+)(?:_[A-Za-z0-9_]*)?$")


def build_panel_username(
    tg_id: int,
    tg_username: Optional[str],
    tg_first_name: Optional[str],
) -> str:
    """Собирает username для Remnawave из TG-профиля.

    Шаблоны (по приоритету):
      `tg_<id>_<sanitized(@username)>` → если есть @username;
      `tg_<id>_<sanitized(first_name)>` → если есть first_name;
      `tg_<id>` → fallback.
    Sanitize: оставляем только [A-Za-z0-9_], обрезаем до общего лимита 32 символа.
    """
    base = f"tg_{tg_id}"
    raw = tg_username or tg_first_name or ""
    if not raw:
        return base[:REMNAWAVE_USERNAME_MAX_LEN]
    suffix = USERNAME_SAFE_CHARS_RE.sub("", raw).strip("_")
    if not suffix:
        return base[:REMNAWAVE_USERNAME_MAX_LEN]
    full = f"{base}_{suffix}"
    if len(full) <= REMNAWAVE_USERNAME_MAX_LEN:
        return full
    avail = REMNAWAVE_USERNAME_MAX_LEN - len(base) - 1
    if avail <= 0:
        return base[:REMNAWAVE_USERNAME_MAX_LEN]
    return f"{base}_{suffix[:avail]}"
