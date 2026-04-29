"""Клиент SSH к master-ноде (Болгария) для запуска ansible.

Используется только в `handlers/admin_add_node.py`. Импорт `asyncssh`
сделан ленивым — если зависимость не установлена, модуль просто не
работает, не валит весь бот.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import config

logger = logging.getLogger(__name__)


class MasterSSHError(RuntimeError):
    """Любая ошибка при работе с master-нодой по SSH."""


@dataclass
class MasterSSHConfig:
    host: str
    port: int
    user: str
    key_path: str
    ansible_repo_path: str

    @classmethod
    def from_env(cls) -> "MasterSSHConfig":
        if not config.master_ssh_configured():
            raise MasterSSHError(
                "MASTER_SSH_HOST/MASTER_SSH_KEY_PATH не заданы — добавь их в .env"
            )
        return cls(
            host=config.MASTER_SSH_HOST or "",
            port=config.MASTER_SSH_PORT,
            user=config.MASTER_SSH_USER,
            key_path=config.MASTER_SSH_KEY_PATH or "",
            ansible_repo_path=config.MASTER_ANSIBLE_REPO_PATH,
        )


def _import_asyncssh():
    try:
        import asyncssh  # type: ignore
        return asyncssh
    except ImportError as e:
        raise MasterSSHError(
            "Зависимость asyncssh не установлена — `pip install asyncssh>=2.14`"
        ) from e


async def run_command_streaming(
    cfg: MasterSSHConfig,
    command: str,
    timeout: float = 1800.0,
) -> AsyncIterator[str]:
    """Подключается по SSH к master, запускает команду и стримит stdout/stderr построчно.

    Yields строки с уже отрезанным `\\n`. По окончании генератор завершается; если
    команда вернула не 0, бросает MasterSSHError с кодом возврата.
    """
    asyncssh = _import_asyncssh()
    logger.info(
        "Master SSH connect %s@%s:%d (key=%s)",
        cfg.user, cfg.host, cfg.port, cfg.key_path,
    )
    async with asyncssh.connect(
        host=cfg.host,
        port=cfg.port,
        username=cfg.user,
        client_keys=[cfg.key_path],
        known_hosts=None,  # bot работает в контейнере, известных хостов нет; см. README
    ) as conn:
        process = await conn.create_process(
            command,
            stderr=asyncssh.STDOUT,
            term_type="xterm",
            term_size=(120, 40),
        )
        try:
            async with asyncio.timeout(timeout):
                async for line in process.stdout:
                    if line is None:
                        break
                    yield line.rstrip("\n").rstrip("\r")
        except asyncio.TimeoutError:
            process.terminate()
            raise MasterSSHError(f"Команда не уложилась в {int(timeout)}с")
        rc = await process.wait()
        if rc.exit_status not in (0, None):
            raise MasterSSHError(
                f"Команда завершилась с кодом {rc.exit_status}"
            )


def build_add_node_command(
    cfg: MasterSSHConfig,
    *,
    name: str,
    address: str,
    ssh_port: int,
    node_port: int,
    bridge_sni: str,
    country_code: str,
) -> str:
    """Собрать shell-команду, которая на master вызывает helper-скрипт."""
    script = f"{cfg.ansible_repo_path}/scripts/add_node.sh"
    args = [
        script,
        "--name", name,
        "--address", address,
        "--ssh-port", str(ssh_port),
        "--node-port", str(node_port),
        "--bridge-sni", bridge_sni,
        "--country", country_code,
    ]
    return " ".join(shlex.quote(a) for a in args)


async def collect_command_output(
    cfg: MasterSSHConfig,
    command: str,
    timeout: float = 1800.0,
    max_lines: int = 4000,
) -> tuple[bool, list[str]]:
    """Запустить команду, собрать вывод в список (с лимитом) и вернуть (ok, lines)."""
    lines: list[str] = []
    try:
        async for line in run_command_streaming(cfg, command, timeout=timeout):
            lines.append(line)
            if len(lines) > max_lines:
                lines = lines[-max_lines:]
        return True, lines
    except MasterSSHError as e:
        lines.append(f"[ERROR] {e}")
        return False, lines
    except Exception as e:  # noqa: BLE001
        logger.exception("master_ssh: неожиданная ошибка")
        lines.append(f"[ERROR] {e}")
        return False, lines


# Sanity-проверка: должен быть валидный приватный ключ и хост резолвится.
async def selfcheck() -> tuple[bool, Optional[str]]:
    """Возвращает (ok, error). Запускает `whoami` на master."""
    try:
        cfg = MasterSSHConfig.from_env()
    except MasterSSHError as e:
        return False, str(e)
    ok, lines = await collect_command_output(cfg, "whoami && hostname && pwd", timeout=15)
    if not ok:
        return False, "\n".join(lines[-10:])
    return True, "\n".join(lines)
