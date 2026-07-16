"""
日志系统 - 使用loguru
"""
import sys
from loguru import logger
from config.settings import LOG_DIR, RUN_MODE


def setup_logger():
    """初始化全局日志"""
    logger.remove()

    log_level = "DEBUG" if RUN_MODE == "backtest" else "INFO"

    # 控制台输出
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
               "<level>{message}</level>",
        colorize=True,
    )

    # 文件输出 - 按天轮转
    logger.add(
        LOG_DIR / "quant_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="00:00",           # 每天轮转
        retention="30 days",        # 保留30天
        compression="zip",
        encoding="utf-8",
    )

    # 错误日志单独输出
    logger.add(
        LOG_DIR / "error_{time:YYYY-MM-DD}.log",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="00:00",
        retention="90 days",
        compression="zip",
        encoding="utf-8",
    )

    return logger


# 全局logger实例
log = setup_logger()
