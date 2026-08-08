import datetime as dt
import logging
import os
import sys
from pathlib import Path

import yaml
from netmiko import ConnectHandler

BASE_DIR = Path(__file__).resolve().parent

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

STAMP = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"snmp_{STAMP}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

log = logging.getLogger("snmp")


def load_inventory():
    with (BASE_DIR / "inventory.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env_value(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def main():
    inv = load_inventory()

    community = inv["global_vars"]["snmp_community"]
    zabbix = inv["global_vars"]["zabbix_server_ip"]

    for device in inv.get("snmp_targets", []):
        conn = ConnectHandler(
            device_type="cisco_ios",
            host=device["host"],
            username="admin",
            password=env_value(device["admin"]),
            secret=env_value(device["admin"]),
            fast_cli=False,
        )

        try:
            conn.enable()

            running = conn.send_command("show running-config")

            desired = [
                f"snmp-server community {community} RO",
                f"snmp-server host {zabbix} version 2c {community}",
                "snmp-server enable traps",
            ]

            missing = [line for line in desired if line not in running]

            if missing:
                conn.send_config_set(missing)
                conn.save_config()

                log.info(
                    "%s: added %s SNMP lines",
                    device["hostname"],
                    len(missing),
                )
            else:
                log.info(
                    "%s: SNMP already correct",
                    device["hostname"],
                )

        finally:
            conn.disconnect()


if __name__ == "__main__":
    main()