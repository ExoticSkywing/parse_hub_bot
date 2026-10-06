import asyncio
import os
import sys

from pyrogram import Client
from pyrogram.session import Session

from core.config import bs, ws
from log import logger

logger = logger.bind(name="Watchdog")

_heartbeat_task: asyncio.Task | None = None


async def reset_count_task() -> None:
    """重置重启次数任务"""
    if ws.restart_count:
        logger.info(f"第 {ws.restart_count} 次重启成功, 稳定运行 10 分钟后重置重启次数")
    elif ws.disconnect_count:
        logger.info("Bot 重连成功, 稳定运行 10 分钟后重置断开连接次数")

    await asyncio.sleep(600)
    ws.reset_bot_disconnect_count()
    ws.reset_bot_restart_count()
    logger.info("已稳定运行 10 分钟, 次数已重置")


async def remove_session_file(cli: Client) -> None:
    """删除会话文件"""
    logger.warning("尝试删除会话文件...")
    try:
        if cli.session is not None:
            try:
                await cli.session.stop()
            except Exception:
                pass
        try:
            await cli.storage.close()
        except Exception:
            pass

        for suffix in ("", "-journal", "-wal", "-shm"):
            session_file = cli.workdir / f"{cli.name}.session{suffix}"
            if session_file.exists():
                os.remove(session_file)
                logger.warning(f"会话文件已移除: {session_file}")
    except Exception as e:
        logger.error(f"移除会话文件失败: {e}")


async def trigger_restart(cli: Client, reason: str = "Bot 已断开连接") -> None:
    """执行重启或终止进程"""
    if ws.exit_flag:
        return

    if ws.restart_count >= ws.max_restart_count:
        logger.critical(f"重启次数已达上限 ({ws.max_restart_count} 次), 结束进程")
        os._exit(1)

    if bs.debug:
        logger.warning("Bot 已断开连接, 目前处于调试模式, 已跳过重启")
        os._exit(1)

    try:
        ws.update_bot_restart_count()
        logger.warning(f"{reason}, 尝试重启... | {ws.restart_count}/{ws.max_restart_count}")

        if ws.restart_count == ws.remove_session_after_restart and not cli.in_memory:
            await remove_session_file(cli)

        python = sys.executable
        os.execv(python, [python] + sys.argv)
    except Exception as e:
        logger.exception(f"重启失败, 结束进程: {e}")
        os._exit(1)


async def heartbeat_worker(cli: Client, interval: int = 30) -> None:
    """巡检任务: 检测 Bot 连接与会话状态，防止静默假死"""
    abnormal_count = 0
    max_abnormal = 3

    while not ws.exit_flag:
        await asyncio.sleep(interval)
        if ws.exit_flag:
            break
        if not ws.is_running:
            continue

        session = getattr(cli, "session", None)
        session_started = (
            session is not None
            and getattr(session, "is_started", None) is not None
            and session.is_started.is_set()
        )
        is_connected = getattr(cli, "is_connected", False)

        if is_connected and session_started:
            abnormal_count = 0
        else:
            abnormal_count += 1
            logger.warning(
                f"Bot 会话状态异常: is_connected={is_connected}, session_started={session_started} ({abnormal_count}/{max_abnormal})"
            )
            if abnormal_count >= max_abnormal:
                logger.error("Bot 会话长时间未恢复，触发看门狗重启...")
                await trigger_restart(cli, reason="会话假死超时")
                break


def start_heartbeat_task(cli: Client) -> None:
    """启动心跳监控任务"""
    global _heartbeat_task
    if _heartbeat_task is None or _heartbeat_task.done():
        _heartbeat_task = asyncio.create_task(heartbeat_worker(cli))


async def on_connect(cli: Client, session: Session) -> None:
    """Bot 连接成功回调函数"""

    if session.is_media:
        return

    ws.is_running = True
    logger.success("Bot 开始运行...")

    start_heartbeat_task(cli)

    if ws.restart_count or ws.disconnect_count:
        asyncio.create_task(reset_count_task())


async def on_disconnect(cli: Client, session: Session) -> None:
    """Bot 断开连接回调函数"""

    if session.is_media:
        return

    if ws.exit_flag:
        ws.is_running = False

    # 正常退出
    if ws.exit_flag and not ws.is_running:
        logger.info("Bot 已结束运行")
        return

    # 启动失败
    if not ws.is_running and not ws.restart_count:
        logger.error("Bot 连接失败, 请检查设备网络和代理配置")
        os._exit(1)

    if ws.disconnect_count < ws.max_disconnect_count:
        ws.update_bot_disconnect_count()
        logger.warning(f"Bot 已断开连接... | {ws.disconnect_count}/{ws.max_disconnect_count}")
        return

    await trigger_restart(cli, reason="Bot 断开连接超限")
