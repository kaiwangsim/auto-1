"""
Network device connector module using netmiko.
This module handles all Cisco device connections and configuration retrieval.
"""

from netmiko import ConnectHandler


def connect_to_device(host, username, password):
    """
    Connect to a Cisco device and retrieve running configuration.
    
    Args:
        host (str): Device IP address
        username (str): Username for authentication
        password (str): Password for authentication
        
    Returns:
        str: Running configuration from the device
        
    Raises:
        Exception: If connection or command execution fails
    """
    device = {
        "device_type": "cisco_ios",
        "ip": host,
        "username": username,
        "password": password,
    }
    
    net_connect = ConnectHandler(**device)
    try:
        output = net_connect.send_command("show running-config")
    finally:
        net_connect.disconnect()
    
    return output
