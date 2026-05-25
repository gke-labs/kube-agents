import logging
import sys


def get_logger(name: str) -> logging.Logger:
  logger = logging.getLogger(name)
  logger.setLevel(logging.INFO)

  # Prevent propagation to the root logger to avoid double-logging or leakage
  logger.propagate = False

  if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(handler)

  return logger
