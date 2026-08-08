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

def params(device):
    return {
        "device_type": device.get("device_type", "cisco_ios"),
        "host": device["host"],
        "username": device.get("username", "admin"),
        "password": env_value(device["password_env"]),
        "secret": env_value(device["secret_env"]),
        "conn_timeout": 15,
        "auth_timeout": 30,
        "banner_timeout": 30,
        "fast_cli": False,
    }


def backup(conn, hostname):
    running = conn.send_command("show running-config")
    path = BACKUP_DIR / f"{hostname}_{STAMP}.cfg"
    path.write_text(running, encoding="utf-8")
    log.info("%s: backup saved to %s", hostname, path)


def apply_if_missing(conn, hostname, label, commands, markers, dry_run):
    running = conn.send_command("show running-config")
    missing = [m for m in markers if m not in running]

    if not missing:
        log.info("%s: %s already correct", hostname, label)
        return False

    log.info("%s: %s missing: %s", hostname, label, missing)

    if dry_run:
        log.info("%s: DRY RUN - would apply %s", hostname, commands)
        return True

    conn.send_config_set(commands)
    return True


def configure_interfaces(conn, device, dry_run):
    for intf in device.get("interfaces", []):
        commands = [
            f"interface {intf['name']}",
            f"description {intf['description']}",
        ]

        markers = [
            f"interface {intf['name']}",
            f"description {intf['description']}",
        ]

        if intf["mode"] == "dhcp":
            commands.append("ip address dhcp")
            markers.append("ip address dhcp")
        else:
            line = f"ip address {intf['ip_address']} {intf['subnet_mask']}"
            commands.append(line)
            markers.append(line)

        if intf.get("nat_role") == "inside":
            commands.append("ip nat inside")
            markers.append("ip nat inside")
        elif intf.get("nat_role") == "outside":
            commands.append("ip nat outside")
            markers.append("ip nat outside")

        commands.append("no shutdown")

        apply_if_missing(
            conn,
            device["hostname"],
            f"interface {intf['name']}",
            commands,
            markers,
            dry_run,
        )

