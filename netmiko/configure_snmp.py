import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

import yaml
from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)


BASE_DIR = Path(__file__).resolve().parent

LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"

LOG_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)

STAMP = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

LOG_FILE = LOG_DIR / f"snmp_{STAMP}.log"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)

log = logging.getLogger("snmp")


def parse_args():
    """Parse CLI arguments for SNMP automation."""
    parser = argparse.ArgumentParser(
        description="Automate SNMPv2c configuration across campus network devices."
    )
    parser.add_argument(
        "--device",
        help="Limit SNMP configuration to one exact device (e.g. SW-A-DCEE)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview configuration changes without applying them",
    )
    return parser.parse_args()


def load_inventory():
    """Load the common Netmiko YAML inventory."""

    inventory_file = BASE_DIR / "inventory.yaml"

    with inventory_file.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def env_value(name):
    """Read a required credential from an environment variable."""

    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Missing environment variable: {name}"
        )

    return value


def connection_params(device):
    """Create Netmiko connection parameters for one device."""

    return {
        "device_type": "cisco_ios",
        "host": device["host"],
        "username": "admin",
        "password": env_value(device["password_env"]),
        "secret": env_value(device["secret_env"]),
        "conn_timeout": 15,
        "auth_timeout": 30,
        "banner_timeout": 30,
        "fast_cli": False,
    }


def backup_running_config(conn, hostname):
    """Save a local pre-change configuration backup."""

    running_config = conn.send_command(
        "show running-config"
    )

    backup_file = (
        BACKUP_DIR /
        f"{hostname}_before_snmp_{STAMP}.cfg"
    )

    backup_file.write_text(
        running_config,
        encoding="utf-8",
    )

    log.info(
        "%s: backup saved to %s",
        hostname,
        backup_file,
    )


def configure_snmp(
    conn,
    hostname,
    community,
    zabbix_ip,
    dry_run=False,
):
    """
    Add only missing SNMP configuration lines.

    Returning False means the device was already correct.
    """

    desired = [
        f"snmp-server community {community} RO",
        (
            f"snmp-server host {zabbix_ip} "
            f"version 2c {community}"
        ),
        "snmp-server enable traps",
    ]

    running_config = conn.send_command(
        "show running-config"
    )

    missing = [
        command
        for command in desired
        if command not in running_config
    ]

    if not missing:
        log.info(
            "%s: SNMP configuration already correct",
            hostname,
        )
        return False

    if dry_run:
        log.info(
            "%s: DRY RUN - would apply %s missing SNMP command(s): %s",
            hostname,
            len(missing),
            missing,
        )
        return True

    log.info(
        "%s: applying %s missing SNMP command(s)",
        hostname,
        len(missing),
    )

    conn.send_config_set(missing)
    conn.save_config()

    return True


def main():

    args = parse_args()

    inventory = load_inventory()

    global_vars = inventory["global_vars"]

    community = global_vars["snmp_community"]
    zabbix_ip = global_vars["zabbix_server_ip"]

    devices = inventory.get("snmp_targets", [])

    if args.device:
        devices = [d for d in devices if d["hostname"] == args.device]
        if not devices:
            log.error("Unknown SNMP target: %s", args.device)
            raise SystemExit(f"Unknown SNMP target: {args.device}")

    success_count = 0

    for device in devices:

        hostname = device["hostname"]

        log.info(
            "Connecting to %s (%s)",
            hostname,
            device["host"],
        )

        try:
            conn = ConnectHandler(
                **connection_params(device)
            )

            try:
                conn.enable()

                if not args.dry_run:
                    backup_running_config(
                        conn,
                        hostname,
                    )

                changed = configure_snmp(
                    conn,
                    hostname,
                    community,
                    zabbix_ip,
                    dry_run=args.dry_run,
                )

                output = conn.send_command(
                    "show running-config | "
                    "include ^snmp-server"
                )

                log.info(
                    "%s: changed=%s (dry_run=%s)\n%s",
                    hostname,
                    changed,
                    args.dry_run,
                    output,
                )

                success_count += 1

            finally:
                conn.disconnect()

        except NetmikoAuthenticationException:
            log.error(
                "%s: authentication failed",
                hostname,
            )

        except NetmikoTimeoutException:
            log.error(
                "%s: SSH connection timed out",
                hostname,
            )

        except Exception as error:
            log.exception(
                "%s: SNMP automation failed: %s",
                hostname,
                error,
            )

    log.info(
        "SNMP automation complete: %s/%s successful",
        success_count,
        len(devices),
    )

    log.info(
        "Log saved to %s",
        LOG_FILE,
    )

    if success_count != len(devices):
        raise SystemExit(1)


if __name__ == "__main__":
    main()