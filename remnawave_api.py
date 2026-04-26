import aiohttp
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Union, Tuple

logger = logging.getLogger(__name__)

DEFAULT_INTERNAL_SQUAD_NAME = "Default-Squad"


def _parse_api_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_expire_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def _internal_squad_uuid_by_name(squads_payload: Optional[dict], name: str) -> Optional[str]:
    """Ищет UUID internal squad по имени (сравнение без учёта регистра)."""
    if not squads_payload or "response" not in squads_payload:
        return None
    squads = squads_payload["response"].get("internalSquads") or []
    target = name.casefold()
    for s in squads:
        n = s.get("name")
        if n and str(n).casefold() == target:
            return s.get("uuid")
    return None


class RemnawaveAPI:
    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url.rstrip('/')
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }

    async def get_internal_squads(self) -> Optional[dict]:
        """GET /api/internal-squads — список internal squads."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            url = f"{self.base_url}/api/internal-squads"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    err = await resp.text()
                    logger.error(f"get_internal_squads: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"get_internal_squads: {e}")
                return None

    async def create_user(
        self,
        username: str,
        expire_days: int,
        hwid_device_limit: int = 3,
        internal_squad_name: Optional[str] = DEFAULT_INTERNAL_SQUAD_NAME,
    ) -> dict:
        async with aiohttp.ClientSession(headers=self.headers) as session:
            # Рассчитываем дату окончания: текущее время (UTC) + количество дней
            expire_date = datetime.utcnow() + timedelta(days=expire_days)
            # Форматируем в строку стандарта ISO 8601, которую обычно ждут API
            expire_at_str = expire_date.strftime("%Y-%m-%dT%H:%M:%SZ")

            payload = {
                "username": username,
                "status": "ACTIVE",         # ИСПРАВЛЕНО: заглавные буквы
                "expireAt": expire_at_str,  # ИСПРАВЛЕНО: правильное название и формат даты
                "data_limit": 0,
                "hwidDeviceLimit": hwid_device_limit,
            }

            if internal_squad_name:
                try:
                    async with session.get(f"{self.base_url}/api/internal-squads") as sq_resp:
                        if sq_resp.status == 200:
                            squads_data = await sq_resp.json()
                            squad_uuid = _internal_squad_uuid_by_name(squads_data, internal_squad_name)
                            if squad_uuid:
                                payload["activeInternalSquads"] = [squad_uuid]
                                logger.info(
                                    "К пользователю будет привязан сквад \"%s\" (%s)",
                                    internal_squad_name,
                                    squad_uuid,
                                )
                            else:
                                logger.warning(
                                    'Internal squad "%s" не найден в панели — пользователь создаётся без сквада',
                                    internal_squad_name,
                                )
                        else:
                            err = await sq_resp.text()
                            logger.warning(
                                "Не удалось получить список сквадов (%s): %s — создаём пользователя без сквада",
                                sq_resp.status,
                                err,
                            )
                except Exception as e:
                    logger.warning("Ошибка при запросе internal squads: %s — создаём без сквада", e)
            
            url = f"{self.base_url}/api/users"
            logger.info(f"Отправляем запрос на создание: {url}")
            logger.info(f"Данные: {payload}")
            
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status in (200, 201):
                        response_data = await resp.json()
                        # ДЕБАГ: Выводим полный ответ панели
                        logger.info("=" * 40)
                        logger.info(f"СЫРОЙ ОТВЕТ ОТ REMNAWAVE: {response_data}")
                        logger.info("=" * 40)
                        return response_data
                    else:
                        error_text = await resp.text()
                        logger.error(f"❌ Ошибка API Remnawave! Статус: {resp.status}. Ответ: {error_text}")
                        return None
            except Exception as e:
                logger.error(f"❌ Критическая ошибка при подключении: {e}")
                return None

    async def patch_user(self, payload: dict) -> bool:
        """PATCH /api/users — частичное обновление пользователя."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            url = f"{self.base_url}/api/users"
            try:
                async with session.patch(url, json=payload) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        logger.error(f"patch_user: статус {resp.status}, ответ: {err}")
                    return resp.status == 200
            except Exception as e:
                logger.error(f"patch_user: {e}")
                return False

    async def extend_user_subscription_days(self, user_uuid: str, days: int) -> Tuple[bool, Optional[str]]:
        """
        Продлевает подписку на days дней от текущего expireAt (если истекла — от текущего момента).
        Возвращает (успех, новый expireAt ISO или None).
        """
        info = await self.get_user_info(user_uuid)
        if not info or "response" not in info:
            return False, None
        ad = info["response"]
        exp = _parse_api_datetime(ad.get("expireAt"))
        now = datetime.now(timezone.utc)
        if exp is None or exp < now:
            base = now
        else:
            base = exp
        new_exp = base + timedelta(days=days)
        new_iso = _format_expire_iso_utc(new_exp)
        ok = await self.patch_user({"uuid": user_uuid, "expireAt": new_iso})
        return ok, new_iso if ok else None

    async def update_hwid_device_limit(self, user_uuid: str, new_limit: Union[int, None]) -> bool:
        """Обновляет лимит устройств по HWID (PATCH /api/users). None = снять лимит (null в JSON)."""
        return await self.patch_user({"uuid": user_uuid, "hwidDeviceLimit": new_limit})

    async def list_users(self, size: int = 100, start: int = 0) -> Optional[dict]:
        """GET /api/users?size=&start= — постраничный список юзеров.
        Возвращает dict вида {'response': {'total': int, 'users': [...]}} или None при ошибке.
        """
        async with aiohttp.ClientSession(headers=self.headers) as session:
            url = f"{self.base_url}/api/users"
            params = {"size": size, "start": start}
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    err = await resp.text()
                    logger.error(f"list_users: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"list_users: {e}")
                return None

    async def get_user_info(self, user_id: str) -> dict:
        """Получает информацию о пользователе. В user_id можно пробовать передавать UUID или username."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            url = f"{self.base_url}/api/users/{user_id}"
            logger.info(f"Запрос инфо о пользователе: {url}")
            
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.info(f"Данные получены успешно для {user_id}")
                        return data
                    else:
                        err = await resp.text()
                        logger.error(f"❌ Ошибка получения инфо! Статус: {resp.status}. Ответ: {err}")
                        return None
            except Exception as e:
                logger.error(f"❌ Ошибка соединения при get_user_info: {e}")
                return None

    async def get_user_hwid_devices(self, user_uuid: str) -> Optional[dict]:
        """GET /api/hwid/devices/{userUuid} — список устройств пользователя."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            url = f"{self.base_url}/api/hwid/devices/{user_uuid}"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    err = await resp.text()
                    logger.error(f"get_user_hwid_devices: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"get_user_hwid_devices: {e}")
                return None

    async def delete_user_hwid_device(self, user_uuid: str, hwid: str) -> bool:
        """POST /api/hwid/devices/delete — удалить устройство по HWID."""
        async with aiohttp.ClientSession(headers=self.headers) as session:
            url = f"{self.base_url}/api/hwid/devices/delete"
            payload = {"userUuid": user_uuid, "hwid": hwid}
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        logger.error(f"delete_user_hwid_device: статус {resp.status}, ответ: {err}")
                    return resp.status == 200
            except Exception as e:
                logger.error(f"delete_user_hwid_device: {e}")
                return False