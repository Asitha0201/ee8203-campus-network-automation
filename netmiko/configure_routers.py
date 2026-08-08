import argparse
import datetime as dt
import logging
import os
import re
import sys
from pathlib import Path

import yaml
from netmiko import (
    ConnectHandler,
    NetmikoAuthenticationException,
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
    """Return a required credential from an environment variable."""
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set"
        )

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


def normalize_ios_line(line: str) -> str:
    """Normalize IOS whitespace for reliable line comparisons."""
    if not line:
        return ""

    return re.sub(
        r"\s+",
        " ",
        line.strip(),
    )


def normalize_acl_line(line: str) -> str:
    """
    Normalize an ACL rule and remove a leading IOS ACL
    sequence number when one is present.
    """
    line = normalize_ios_line(line)

    return re.sub(
        r"^\d+\s+",
        "",
        line,
    )


def is_line_in_config(
    marker: str,
    running_config: str,
    is_acl: bool = False,
) -> bool:
    """Check whether one normalized IOS line exists."""

    if is_acl:
        expected = normalize_acl_line(marker)
    else:
        expected = normalize_ios_line(marker)

    if not expected:
        return True

    for line in running_config.splitlines():

        if is_acl:
            actual = normalize_acl_line(line)
        else:
            actual = normalize_ios_line(line)

        if actual == expected:
            return True

    return False


def backup(conn, hostname):
    running = conn.send_command("show running-config")
    path = BACKUP_DIR / f"{hostname}_{STAMP}.cfg"
    path.write_text(running, encoding="utf-8")
    log.info("%s: backup saved to %s", hostname, path)


def apply_if_missing(conn, hostname, label, commands, markers, dry_run, is_acl=False):
    running = conn.send_command("show running-config")
    missing = [m for m in markers if not is_line_in_config(m, running, is_acl=is_acl)]

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


def configure_ospf(conn, device, dry_run):
    ospf = device["ospf"]

    commands = [
        f"router ospf {ospf['process_id']}",
        f"router-id {ospf['router_id']}",
    ]

    markers = commands.copy()

    for network in ospf.get("networks", []):
        line = f"network {network}"
        commands.append(line)
        markers.append(line)

    if ospf.get("default_information_originate"):
        commands.append("default-information originate")
        markers.append("default-information originate")

    apply_if_missing(
        conn,
        device["hostname"],
        "OSPF",
        commands,
        markers,
        dry_run,
    )


def configure_nat(conn, device, dry_run):
    nat = device.get("nat")

    if not nat:
        return

    acl_cmds = [
        f"ip access-list standard {nat['acl_name']}"
    ]

    acl_markers = acl_cmds.copy()

    for subnet in nat["permitted_subnets"]:
        line = f"permit {subnet}"
        acl_cmds.append(line)
        acl_markers.append(line)

    apply_if_missing(
        conn,
        device["hostname"],
        "NAT ACL",
        acl_cmds,
        acl_markers,
        dry_run,
        is_acl=True,
    )

    overload = (
        f"ip nat inside source list {nat['acl_name']} "
        f"interface {nat['outside_interface']} overload"
    )

    apply_if_missing(
        conn,
        device["hostname"],
        "NAT overload",
        [overload],
        [overload],
        dry_run,
    )


def configure_ssh_acl(conn, device, dry_run):
    acl = device.get("ssh_acl")

    if not acl:
        return

    commands = [
        f"ip access-list extended {acl['name']}"
    ] + acl["rules"]

    markers = commands.copy()

    apply_if_missing(
        conn,
        device["hostname"],
        "SSH ACL",
        commands,
        markers,
        dry_run,
        is_acl=True,
    )

    vty = [
        "line vty 0 15",
        f"access-class {acl['name']} in",
        "login local",
        "transport input ssh",
    ]

    apply_if_missing(
        conn,
        device["hostname"],
        "VTY policy",
        vty,
        [
            f"access-class {acl['name']} in",
            "login local",
            "transport input ssh",
        ],
        dry_run,
    )


def verify(conn, hostname):
    checks = [
        "show ip interface brief",
        "show ip ospf neighbor",
        "show ip route",
        "show access-lists ACL_SSH_MGMT",
    ]

    if hostname == "R-EDGE":
        checks += [
            "show ip nat statistics",
            "show access-lists NAT_INTERNET_EGRESS",
            "show ip route 0.0.0.0",
        ]

    for command in checks:
        log.info(
            "%s: %s\n%s",
            hostname,
            command,
            conn.send_command(command),
        )


def configure_router(device, dry_run):
    conn = ConnectHandler(**params(device))

    try:
        conn.enable()

        backup(conn, device["hostname"])

        configure_interfaces(conn, device, dry_run)
        configure_ospf(conn, device, dry_run)
        configure_nat(conn, device, dry_run)
        configure_ssh_acl(conn, device, dry_run)

        if not dry_run:
            conn.save_config()

        verify(conn, device["hostname"])

        return True

    finally:
        conn.disconnect()


def main():
    args = parse_args()

    routers = load_inventory().get("routers", [])

    if args.device:
        routers = [
            r for r in routers
            if r["hostname"] == args.device
        ]

    if not routers:
        raise SystemExit(f"Unknown router: {args.device}")

    success = 0

    for device in routers:
        try:
            if configure_router(device, args.dry_run):
                success += 1

        except NetmikoAuthenticationException:
            log.error(
                "%s: authentication failed",
                device["hostname"],
            )

        except NetmikoTimeoutException:
            log.error(
                "%s: SSH timeout",
                device["hostname"],
            )

        except Exception as exc:
            log.exception(
                "%s: failed: %s",
                device["hostname"],
                exc,
            )

    log.info(
        "Complete: %s/%s routers successful",
        success,
        len(routers),
    )

    if success != len(routers):
        raise SystemExit(1)


if __name__ == "__main__":
    main()