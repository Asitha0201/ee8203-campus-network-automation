import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

import yaml
from netmiko import configure_routers
from netmiko.exceptions import (NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

BASE_DIR = Path(__file__).resolve().parent

LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"

LOG_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)

STAMP = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = LOG_DIR / f"routers_{STAMP}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)

log = logging.getLogger("ee8203")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--device")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def load_inventory():
    with (BASE_DIR / "inventory.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env_value(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value