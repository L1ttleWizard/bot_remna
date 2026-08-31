import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Union, Tuple

from contextlib import asynccontextmanager
import socket
import ssl

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
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE
        self._session_obj: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session_obj is None or self._session_obj.closed:
            conn = aiohttp.TCPConnector(ssl=self._ssl_ctx, happy_eyeballs_delay=None)
            self._session_obj = aiohttp.ClientSession(headers=self.headers, connector=conn)
        return self._session_obj

    @asynccontextmanager
    async def _session(self, timeout: Optional[aiohttp.ClientTimeout] = None):
        session = await self._get_session()
        yield session

    async def close(self):
        if self._session_obj and not self._session_obj.closed:
            await self._session_obj.close()

    async def get_internal_squads(self) -> Optional[dict]:
        """GET /api/internal-squads — список internal squads."""
        async with self._session() as session:
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
        async with self._session() as session:
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
        p = payload.copy()
        if "uuid" in p and "id" not in p:
            resolved = await self.resolve_user(p["uuid"])
            if resolved and "id" in resolved:
                p["id"] = resolved["id"]
        async with self._session() as session:
            url = f"{self.base_url}/api/users"
            try:
                async with session.patch(url, json=p) as resp:
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

    async def update_active_internal_squads(
        self, user_uuid: str, squad_uuids: List[str]
    ) -> bool:
        """Заменяет список активных internal squads (профилей) пользователя.

        Передаваемый список — это полный набор UUID, который должен быть назначен.
        Пустой список = снять все профили.
        """
        # Remnawave PATCH /api/users принимает activeInternalSquads как массив UUID.
        return await self.patch_user(
            {"uuid": user_uuid, "activeInternalSquads": list(squad_uuids)}
        )

    async def get_user_usage_range(
        self,
        user_id_or_uuid: Union[str, int],
        start_date: str,
        end_date: str,
        *,
        timeout_s: float = 4.0,
    ) -> Optional[int]:
        """Сумма трафика юзера за окно [start_date..end_date] (YYYY-MM-DD).

        Использует Remnawave `/api/bandwidth-stats/users/{userId}` (sparklineData),
        с fallback на legacy `/api/bandwidth-stats/users/{userId}/legacy`.
        Возвращает суммарные байты или None если эндпоинт недоступен/упал/таймаут.
        """
        str_id = str(user_id_or_uuid).strip()
        numeric_id = str_id if str_id.isdigit() else None
        if not numeric_id:
            resolved = await self.resolve_user(str_id)
            if resolved and "id" in resolved:
                numeric_id = str(resolved["id"])

        if not numeric_id:
            logger.error(f"get_user_usage_range: не удалось определить numeric ID для {user_id_or_uuid}")
            return None

        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with self._session(timeout=timeout) as session:
            base = f"{self.base_url}/api/bandwidth-stats/users/{numeric_id}"
            params = {"start": start_date, "end": end_date, "topNodesLimit": 1}
            try:
                async with session.get(base, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        resp_obj = data.get("response") if isinstance(data, dict) else data
                        if isinstance(resp_obj, dict):
                            spark = resp_obj.get("sparklineData")
                            if isinstance(spark, list) and spark:
                                return int(sum(int(v or 0) for v in spark))
                            if "total" in resp_obj:
                                return int(resp_obj["total"] or 0)
                            if "usedTrafficBytes" in resp_obj:
                                return int(resp_obj["usedTrafficBytes"] or 0)
                        elif isinstance(resp_obj, list):
                            return int(sum(int((r.get("total") if isinstance(r, dict) else r) or 0) for r in resp_obj))
                        return 0
                    if resp.status not in (404, 405):
                        logger.error(
                            "get_user_usage_range %s: статус %s",
                            user_id_or_uuid, resp.status,
                        )
                        return None
            except asyncio.TimeoutError:
                logger.warning("get_user_usage_range %s: timeout", user_id_or_uuid)
                return None
            except Exception as e:
                logger.error("get_user_usage_range %s: %s", user_id_or_uuid, e)

            # Fallback: legacy endpoint (массив записей с total).
            try:
                async with session.get(
                    f"{base}/legacy",
                    params={"start": start_date, "end": end_date},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rows = data.get("response") if isinstance(data, dict) else data
                        if isinstance(rows, list):
                            return int(sum(int((r.get("total") if isinstance(r, dict) else r) or 0) for r in rows))
                    else:
                        logger.error(
                            "get_user_usage_range legacy %s: статус %s",
                            user_id_or_uuid, resp.status,
                        )
            except asyncio.TimeoutError:
                logger.warning("get_user_usage_range legacy %s: timeout", user_id_or_uuid)
            except Exception as e:
                logger.error("get_user_usage_range legacy %s: %s", user_id_or_uuid, e)
        return None

    async def get_user_sparkline_traffic(
        self,
        user_id_or_uuid: Union[str, int],
        start_date: str,
        end_date: str,
        *,
        timeout_s: float = 4.0,
    ) -> Optional[list[int]]:
        """Получает ежедневный трафик пользователя (список байт) за указанное окно.
        
        Использует Remnawave `/api/bandwidth-stats/users/{userId}` (sparklineData),
        с fallback на legacy `/api/bandwidth-stats/users/{userId}/legacy`.
        Возвращает список интов или None при ошибке.
        """
        str_id = str(user_id_or_uuid).strip()
        numeric_id = str_id if str_id.isdigit() else None
        if not numeric_id:
            resolved = await self.resolve_user(str_id)
            if resolved and "id" in resolved:
                numeric_id = str(resolved["id"])

        if not numeric_id:
            logger.error(f"get_user_sparkline_traffic: не удалось определить numeric ID для {user_id_or_uuid}")
            return None

        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with self._session(timeout=timeout) as session:
            base = f"{self.base_url}/api/bandwidth-stats/users/{numeric_id}"
            params = {"start": start_date, "end": end_date, "topNodesLimit": 1}
            try:
                async with session.get(base, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        resp_obj = data.get("response") if isinstance(data, dict) else data
                        if isinstance(resp_obj, dict):
                            spark = resp_obj.get("sparklineData")
                            if isinstance(spark, list):
                                return [int(v or 0) for v in spark]
                            if "series" in resp_obj and isinstance(resp_obj["series"], list):
                                return [int(v or 0) for v in resp_obj["series"]]
                        elif isinstance(resp_obj, list):
                            return [int((r.get("total") if isinstance(r, dict) else r) or 0) for r in resp_obj]
                        return []
                    if resp.status not in (404, 405):
                        logger.error(
                            "get_user_sparkline_traffic %s: статус %s",
                            user_id_or_uuid, resp.status,
                        )
                        return None
            except asyncio.TimeoutError:
                logger.warning("get_user_sparkline_traffic %s: timeout", user_id_or_uuid)
                return None
            except Exception as e:
                logger.error("get_user_sparkline_traffic %s: %s", user_id_or_uuid, e)

            # Fallback: legacy endpoint
            try:
                async with session.get(
                    f"{base}/legacy",
                    params={"start": start_date, "end": end_date},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rows = data.get("response") if isinstance(data, dict) else data
                        if isinstance(rows, list):
                            return [int((r.get("total") if isinstance(r, dict) else r) or 0) for r in rows]
                    else:
                        logger.error(
                            "get_user_sparkline_traffic legacy %s: статус %s",
                            user_id_or_uuid, resp.status,
                        )
            except asyncio.TimeoutError:
                logger.warning("get_user_sparkline_traffic legacy %s: timeout", user_id_or_uuid)
            except Exception as e:
                logger.error("get_user_sparkline_traffic legacy %s: %s", user_id_or_uuid, e)
        return None

    async def get_nodes_bandwidth_stats(
        self,
        start_date: str,
        end_date: str,
        *,
        timeout_s: float = 5.0,
    ) -> Optional[dict]:
        """Получает посуточную статистику трафика по нодам за указанное окно.
        
        Использует эндпоинт `/api/bandwidth-stats/nodes?start=YYYY-MM-DD&end=YYYY-MM-DD`.
        Возвращает dict с ответом от панели или None при ошибке.
        """
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with self._session(timeout=timeout) as session:
            url = f"{self.base_url}/api/bandwidth-stats/nodes"
            params = {"start": start_date, "end": end_date}
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("response") or data
                    logger.error(
                        "get_nodes_bandwidth_stats: статус %s",
                        resp.status,
                    )
            except asyncio.TimeoutError:
                logger.warning("get_nodes_bandwidth_stats: timeout")
            except Exception as e:
                logger.error("get_nodes_bandwidth_stats: %s", e)
        return None

    async def set_user_expire_unlimited(self, user_uuid: str) -> Tuple[bool, Optional[str]]:
        """Снимает лимит времени подписки. Remnawave требует ISO-дату в expireAt
        (null не принимается), поэтому ставим заведомо далёкое будущее — 2099-12-31.
        Возвращает (успех, новый expireAt ISO или None).
        """
        far_future = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        new_iso = _format_expire_iso_utc(far_future)
        ok = await self.patch_user({"uuid": user_uuid, "expireAt": new_iso})
        return ok, new_iso if ok else None

    async def resolve_user(self, identifier: Union[str, int]) -> Optional[dict]:
        """POST /api/users/resolve — резолвит UUID, shortUuid или username в словарь с {id, uuid, shortUuid, username}."""
        str_id = str(identifier).strip()
        if not str_id:
            return None
        if str_id.isdigit():
            payload = {"id": int(str_id)}
        elif "-" in str_id and len(str_id) >= 32:
            payload = {"uuid": str_id}
        else:
            payload = {"shortUuid": str_id}

        async with self._session() as session:
            url = f"{self.base_url}/api/users/resolve"
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("response")
                    err = await resp.text()
                    logger.warning(f"resolve_user не удалось для {identifier}: {resp.status} {err}")
                    return None
            except Exception as e:
                logger.error(f"resolve_user ошибка при {identifier}: {e}")
                return None

    async def delete_user(self, user_uuid: str) -> Tuple[bool, Optional[str]]:
        """Удаляет пользователя по UUID или numeric ID. Возвращает (успех: bool, error_msg: str | None)."""
        str_id = str(user_uuid).strip()
        numeric_id = str_id if str_id.isdigit() else None
        if not numeric_id:
            resolved = await self.resolve_user(str_id)
            if resolved and "id" in resolved:
                numeric_id = str(resolved["id"])

        if not numeric_id:
            logger.error(f"delete_user: не удалось определить numeric ID для {user_uuid}")
            return False, "user_not_found"

        async with self._session() as session:
            url = f"{self.base_url}/api/users/{numeric_id}"
            try:
                async with session.delete(url) as resp:
                    if resp.status in (200, 204, 404):
                        return True, None
                    err = await resp.text()
                    logger.error(f"delete_user: статус {resp.status}, ответ: {err}")
                    return False, f"http_{resp.status}"
            except Exception as e:
                logger.error(f"delete_user: {e}")
                return False, "exception"

    async def list_users(self, size: int = 100, start: int = 0) -> Optional[dict]:
        """GET /api/users?size=&start= — постраничный список юзеров.
        Возвращает dict вида {'response': {'total': int, 'users': [...]}} или None при ошибке.
        """
        async with self._session() as session:
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

    async def get_user_info(self, user_id: Union[str, int]) -> Optional[dict]:
        """Получает информацию о пользователе по numeric ID, UUID или shortUuid."""
        str_id = str(user_id).strip()
        numeric_id = str_id if str_id.isdigit() else None
        if not numeric_id:
            resolved = await self.resolve_user(str_id)
            if resolved and "id" in resolved:
                numeric_id = str(resolved["id"])

        if not numeric_id:
            logger.error(f"get_user_info: не удалось определить numeric ID для {user_id}")
            return None

        async with self._session() as session:
            url = f"{self.base_url}/api/users/{numeric_id}"
            logger.info(f"Запрос инфо о пользователе: {url} (origin: {user_id})")

            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.info(f"Данные получены успешно для {user_id} (numeric id: {numeric_id})")
                        return data
                    else:
                        err = await resp.text()
                        logger.error(f"❌ Ошибка получения инфо! Статус: {resp.status}. Ответ: {err}")
                        return None
            except Exception as e:
                logger.error(f"❌ Ошибка соединения при get_user_info: {e}")
                return None

    async def get_user_hwid_devices(self, user_id_or_uuid: Union[str, int]) -> Optional[dict]:
        """GET /api/hwid/devices/{userId} — список устройств пользователя.
        {userId} должен быть числовым ID в обновлённой панели.
        """
        str_id = str(user_id_or_uuid).strip()
        numeric_id = str_id if str_id.isdigit() else None
        if not numeric_id:
            resolved = await self.resolve_user(str_id)
            if resolved and "id" in resolved:
                numeric_id = str(resolved["id"])

        if not numeric_id:
            logger.error(f"get_user_hwid_devices: не удалось определить numeric ID для {user_id_or_uuid}")
            return None

        async with self._session() as session:
            url = f"{self.base_url}/api/hwid/devices/{numeric_id}"
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

    async def delete_user_hwid_device(self, user_id_or_uuid: Union[str, int], hwid: str) -> bool:
        """POST /api/hwid/devices/delete — удалить устройство по HWID.
        Тело: {"userId": int, "hwid": str}.
        """
        str_id = str(user_id_or_uuid).strip()
        numeric_id = int(str_id) if str_id.isdigit() else None
        if numeric_id is None:
            resolved = await self.resolve_user(str_id)
            if resolved and "id" in resolved:
                numeric_id = int(resolved["id"])

        if numeric_id is None:
            logger.error(f"delete_user_hwid_device: не удалось определить numeric ID для {user_id_or_uuid}")
            return False

        async with self._session() as session:
            url = f"{self.base_url}/api/hwid/devices/delete"
            payload = {"userId": numeric_id, "hwid": hwid}
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        logger.error(f"delete_user_hwid_device: статус {resp.status}, ответ: {err}")
                    return resp.status == 200
            except Exception as e:
                logger.error(f"delete_user_hwid_device: {e}")
                return False

    # =========================================================================
    # Nodes management
    # =========================================================================

    async def list_nodes(self) -> Optional[list]:
        """GET /api/nodes — все ноды панели. Возвращает список dict'ов или None."""
        async with self._session() as session:
            url = f"{self.base_url}/api/nodes"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and "response" in data:
                            return data["response"]
                        return data if isinstance(data, list) else None
                    err = await resp.text()
                    logger.error(f"list_nodes: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"list_nodes: {e}")
                return None

    async def get_node(self, uuid: str) -> Optional[dict]:
        """GET /api/nodes/{uuid} — карточка одной ноды."""
        async with self._session() as session:
            url = f"{self.base_url}/api/nodes/{uuid}"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    err = await resp.text()
                    logger.error(f"get_node: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"get_node: {e}")
                return None

    async def create_node(self, payload: dict) -> Optional[dict]:
        """POST /api/nodes — создать ноду. payload: name/address/port/configProfileUuid и т.д."""
        async with self._session() as session:
            url = f"{self.base_url}/api/nodes"
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status in (200, 201):
                        try:
                            return await resp.json()
                        except Exception:
                            return None
                    err = await resp.text()
                    logger.error(f"create_node: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"create_node: {e}")
                return None

    async def update_node(self, payload: dict) -> Optional[dict]:
        """PATCH /api/nodes — обновить ноду (uuid передаётся в payload)."""
        async with self._session() as session:
            url = f"{self.base_url}/api/nodes"
            try:
                async with session.patch(url, json=payload) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    err = await resp.text()
                    logger.error(f"update_node: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"update_node: {e}")
                return None

    async def delete_node(self, uuid: str) -> bool:
        """DELETE /api/nodes/{uuid}."""
        async with self._session() as session:
            url = f"{self.base_url}/api/nodes/{uuid}"
            try:
                async with session.delete(url) as resp:
                    if resp.status not in (200, 204):
                        err = await resp.text()
                        logger.error(f"delete_node: статус {resp.status}, ответ: {err}")
                    return resp.status in (200, 204)
            except Exception as e:
                logger.error(f"delete_node: {e}")
                return False

    async def _node_action(self, uuid: str, action: str) -> bool:
        """POST /api/nodes/{uuid}/actions/{action} — общий метод для enable/disable/restart/reset-traffic."""
        async with self._session() as session:
            url = f"{self.base_url}/api/nodes/{uuid}/actions/{action}"
            try:
                async with session.post(url) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        logger.error(f"_node_action({action}): статус {resp.status}, ответ: {err}")
                    return resp.status == 200
            except Exception as e:
                logger.error(f"_node_action({action}): {e}")
                return False

    async def enable_node(self, uuid: str) -> bool:
        return await self._node_action(uuid, "enable")

    async def disable_node(self, uuid: str) -> bool:
        return await self._node_action(uuid, "disable")

    async def restart_node(self, uuid: str) -> bool:
        return await self._node_action(uuid, "restart")

    async def reset_node_traffic(self, uuid: str) -> bool:
        return await self._node_action(uuid, "reset-traffic")

    async def restart_all_nodes(self) -> bool:
        """POST /api/nodes/actions/restart-all."""
        async with self._session() as session:
            url = f"{self.base_url}/api/nodes/actions/restart-all"
            try:
                async with session.post(url, json={}) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        logger.error(f"restart_all_nodes: статус {resp.status}, ответ: {err}")
                    return resp.status == 200
            except Exception as e:
                logger.error(f"restart_all_nodes: {e}")
                return False

    # =========================================================================
    # Infra Billing
    # =========================================================================

    async def list_billing_providers(self) -> Optional[list]:
        """GET /api/infra-billing/providers."""
        async with self._session() as session:
            url = f"{self.base_url}/api/infra-billing/providers"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and "response" in data:
                            r = data["response"]
                            return r.get("providers") if isinstance(r, dict) else r
                        return data.get("providers") if isinstance(data, dict) else None
                    err = await resp.text()
                    logger.error(f"list_billing_providers: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"list_billing_providers: {e}")
                return None

    async def get_billing_provider(self, uuid: str) -> Optional[dict]:
        """GET /api/infra-billing/providers/{uuid}."""
        async with self._session() as session:
            url = f"{self.base_url}/api/infra-billing/providers/{uuid}"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and "response" in data:
                            return data["response"]
                        return data
                    err = await resp.text()
                    logger.error(f"get_billing_provider: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"get_billing_provider: {e}")
                return None

    async def create_billing_provider(self, payload: dict) -> Optional[dict]:
        """POST /api/infra-billing/providers."""
        async with self._session() as session:
            url = f"{self.base_url}/api/infra-billing/providers"
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        if isinstance(data, dict) and "response" in data:
                            return data["response"]
                        return data
                    err = await resp.text()
                    logger.error(f"create_billing_provider: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"create_billing_provider: {e}")
                return None

    async def delete_billing_provider(self, uuid: str) -> bool:
        """DELETE /api/infra-billing/providers/{uuid}."""
        async with self._session() as session:
            url = f"{self.base_url}/api/infra-billing/providers/{uuid}"
            try:
                async with session.delete(url) as resp:
                    if resp.status not in (200, 204):
                        err = await resp.text()
                        logger.error(f"delete_billing_provider: статус {resp.status}, ответ: {err}")
                    return resp.status in (200, 204)
            except Exception as e:
                logger.error(f"delete_billing_provider: {e}")
                return False

    async def list_billing_nodes(self) -> Optional[list]:
        """GET /api/infra-billing/nodes."""
        async with self._session() as session:
            url = f"{self.base_url}/api/infra-billing/nodes"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and "response" in data:
                            r = data["response"]
                            return r.get("billingNodes") if isinstance(r, dict) else r
                        return data.get("billingNodes") if isinstance(data, dict) else None
                    err = await resp.text()
                    logger.error(f"list_billing_nodes: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"list_billing_nodes: {e}")
                return None

    async def create_billing_node(self, payload: dict) -> Optional[dict]:
        """POST /api/infra-billing/nodes."""
        async with self._session() as session:
            url = f"{self.base_url}/api/infra-billing/nodes"
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        if isinstance(data, dict) and "response" in data:
                            return data["response"]
                        return data
                    err = await resp.text()
                    logger.error(f"create_billing_node: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"create_billing_node: {e}")
                return None

    async def update_billing_nodes(self, payload: dict) -> Optional[dict]:
        """PATCH /api/infra-billing/nodes."""
        async with self._session() as session:
            url = f"{self.base_url}/api/infra-billing/nodes"
            try:
                async with session.patch(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and "response" in data:
                            return data["response"]
                        return data
                    err = await resp.text()
                    logger.error(f"update_billing_nodes: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"update_billing_nodes: {e}")
                return None

    async def delete_billing_node(self, uuid: str) -> bool:
        """DELETE /api/infra-billing/nodes/{uuid}."""
        async with self._session() as session:
            url = f"{self.base_url}/api/infra-billing/nodes/{uuid}"
            try:
                async with session.delete(url) as resp:
                    if resp.status not in (200, 204):
                        err = await resp.text()
                        logger.error(f"delete_billing_node: статус {resp.status}, ответ: {err}")
                    return resp.status in (200, 204)
            except Exception as e:
                logger.error(f"delete_billing_node: {e}")
                return False

    async def list_billing_history(self, params: dict) -> Optional[list]:
        """GET /api/infra-billing/history."""
        async with self._session() as session:
            url = f"{self.base_url}/api/infra-billing/history"
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and "response" in data:
                            r = data["response"]
                            return r.get("records") if isinstance(r, dict) else r
                        return data.get("records") if isinstance(data, dict) else None
                    err = await resp.text()
                    logger.error(f"list_billing_history: статус {resp.status}, ответ: {err}")
                    return None
            except Exception as e:
                logger.error(f"list_billing_history: {e}")
                return None