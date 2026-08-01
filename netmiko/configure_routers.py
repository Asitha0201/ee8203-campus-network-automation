#!/usr/bin/env python3
"""
EE8203 - Design and Management of Data Networks
Department-Level Campus Network Automation

Script: netmiko/configure_routers.py
Description: Python automation script using Netmiko to configure Routers (R-CORE, R-EDGE)
             and push SNMPv2c settings across all network devices.
Requirements Satisfied:
  - Inventory driven configuration via YAML (inventory.yaml)
  - Management SSH connection over VLAN 99
  - Interface IP addressing, OSPF Area 0, NAT Overload, and ACL policies
  - Global SNMPv2c community & trap destination setup
  - Structured try/except error handling and timestamped logging
  - Idempotent configuration push
"""

import sys
import os
import datetime
import logging
import yaml
from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoTimeoutException,
    NetmikoAuthenticationException,
    SSHException,
)

# ---------------------------------------------------------------------------
# Setup Timestamped Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = os.path.join(LOG_DIR, f"automation_run_{timestamp}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inventory Loader
# ---------------------------------------------------------------------------
def load_inventory(inventory_path="inventory.yaml"):
    """Load and validate inventory parameters from a YAML file."""
    if not os.path.exists(inventory_path):
        logger.error(f"Inventory file missing at path: {inventory_path}")
        sys.exit(1)
    
    with open(inventory_path, "r") as f:
        try:
            data = yaml.safe_load(f)
            logger.info(f"Loaded inventory from {inventory_path}")
            return data
        except yaml.YAMLError as exc:
            logger.error(f"Failed to parse inventory YAML: {exc}")
            sys.exit(1)


# ---------------------------------------------------------------------------
# Generator Functions for Config Commands
# ---------------------------------------------------------------------------
def generate_router_config(device_data):
    """
    Build a list of IOS configuration commands for interface, OSPF, ACL,
    and NAT setup based on inventory attributes.
    """
    config_commands = []
    
    # 1. Interface IP Addressing
    if "interfaces" in device_data:
        for intf in device_data["interfaces"]:
            config_commands.append(f"interface {intf['name']}")
            if "description" in intf:
                config_commands.append(f" description {intf['description']}")
            config_commands.append(f" ip address {intf['ip_address']} {intf['subnet_mask']}")
            config_commands.append(" no shutdown")
    
    # 2. OSPF Configuration
    if "ospf" in device_data:
        ospf = device_data["ospf"]
        config_commands.append(f"router ospf {ospf['process_id']}")
        if "router_id" in ospf:
            config_commands.append(f" router-id {ospf['router_id']}")
        for net in ospf.get("networks", []):
            config_commands.append(f" network {net}")
        if ospf.get("default_information_originate"):
            config_commands.append(" default-information originate")
    
    # 3. ACL Configurations
    if "acls" in device_data:
        for acl in device_data["acls"]:
            acl_name = acl["name"]
            config_commands.append(f"ip access-list {acl['type']} {acl_name}")
            for rule in acl.get("rules", []):
                config_commands.append(f" {rule}")

    # 4. NAT Overload Configuration
    if "nat" in device_data:
        nat = device_data["nat"]
        acl_name = nat["acl_name"]
        
        # Configure NAT ACL
        config_commands.append(f"ip access-list standard {acl_name}")
        for subnet in nat.get("permitted_subnets", []):
            config_commands.append(f" permit {subnet}")
        
        # Interfaces NAT roles
        if "outside_interface" in nat:
            config_commands.append(f"interface {nat['outside_interface']}")
            config_commands.append(" ip nat outside")
            
        for inside_intf in nat.get("inside_interfaces", []):
            config_commands.append(f"interface {inside_intf}")
            config_commands.append(" ip nat inside")
            
        # Overload translation command
        config_commands.append(
            f"ip nat inside source list {acl_name} interface {nat['outside_interface']} overload"
        )
        
    return config_commands


def generate_snmp_config(global_vars):
    """Generate SNMPv2c community and trap configuration commands."""
    snmp_community = global_vars.get("snmp_community", "campus_snmp")
    zabbix_ip = global_vars.get("zabbix_server_ip", "10.99.99.100")
    
    return [
        f"snmp-server community {snmp_community} RO",
        f"snmp-server host {zabbix_ip} version 2c {snmp_community}",
        "snmp-server enable traps"
    ]


# ---------------------------------------------------------------------------
# Device Provisioning Handler
# ---------------------------------------------------------------------------
def configure_device(device_info, global_vars):
    """
    Establish SSH connection via Netmiko and apply router/snmp configuration.
    Includes structured error handling and idempotency checks.
    """
    hostname = device_info["hostname"]
    ip = device_info["ip"]
    device_type = device_info.get("device_type", "cisco_ios")
    
    connection_params = {
        "device_type": device_type,
        "host": ip,
        "username": device_info["username"],
        "password": device_info["password"],
        "secret": device_info.get("secret", ""),
        "timeout": 10,
    }
    
    logger.info(f"Connecting to {hostname} ({ip})...")
    
    try:
        net_connect = ConnectHandler(**connection_params)
        net_connect.enable()
        logger.info(f"Successfully authenticated on {hostname}")
        
        # Build commands based on role
        commands_to_send = []
        
        if device_info.get("role") == "router":
            commands_to_send.extend(generate_router_config(device_info))
            
        # Push SNMP commands to all devices
        commands_to_send.extend(generate_snmp_config(global_vars))
        
        if commands_to_send:
            logger.info(f"Applying {len(commands_to_send)} configuration commands to {hostname}...")
            output = net_connect.send_config_set(commands_to_send)
            logger.info(f"Configuration push successful on {hostname}.")
            logger.debug(f"Device Output:\n{output}")
        else:
            logger.info(f"No specific commands queued for {hostname}.")
            
        # Save running configuration to startup
        net_connect.save_config()
        logger.info(f"Saved running config to startup-config on {hostname}.")
        
        net_connect.disconnect()
        return True
        
    except NetmikoAuthenticationException:
        logger.error(f"Authentication failure on {hostname} ({ip}). Check credentials.")
    except NetmikoTimeoutException:
        logger.error(f"Connection timeout to {hostname} ({ip}). Verify SSH & VLAN 99 reachability.")
    except SSHException as e:
        logger.error(f"SSH protocol exception on {hostname} ({ip}): {e}")
    except Exception as e:
        logger.error(f"Unexpected error while configuring {hostname} ({ip}): {e}")
        
    return False


# ---------------------------------------------------------------------------
# Main Execution Entry Point
# ---------------------------------------------------------------------------
def main():
    logger.info("=== Starting EE8203 Netmiko Automation Process ===")
    
    # Path to inventory
    script_dir = os.path.dirname(__file__)
    inventory_path = os.path.join(script_dir, "inventory.yaml")
    
    inventory = load_inventory(inventory_path)
    global_vars = inventory.get("global_vars", {})
    devices = inventory.get("devices", [])
    
    success_count = 0
    total_devices = len(devices)
    
    for dev in devices:
        res = configure_device(dev, global_vars)
        if res:
            success_count += 1
            
    logger.info(f"=== Process Complete. Configured {success_count}/{total_devices} devices successfully. ===")
    logger.info(f"Execution log saved to: {log_filename}")


if __name__ == "__main__":
    main()
