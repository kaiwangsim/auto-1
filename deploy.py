import os
import re
from datetime import datetime
from netmiko import ConnectHandler

# 路径相对脚本自身解析, host/password 从上层 input/ 拿
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOSTLIST_FILE = os.path.join(SCRIPT_DIR, "..", "input", "hostlist.txt")
PASSWORD_FILE = os.path.join(SCRIPT_DIR, "..", "input", "password.txt")


# 要下发的配置 (先写死, 后面再考虑做成参数)
CONFIG_COMMANDS = [
    "snmp-server host 10.47.12.69 version 3 priv snmpv3user",
    "ip access-list standard ALLOW-SNMPv3",
    "remark datadog SNMP Agent Collector NEW",
    "permit 10.47.12.69",
]

def read_hosts(path):
    with open(path, encoding="utf-8-sig") as f:
        return [line.strip() for line in f if line.strip()]

def read_credentials(path):
    """约定: 第1行=用户名, 第2行=密码。"""
    with open(path, encoding="utf-8-sig") as f:
        lines = [line.strip() for line in f if line.strip()]
    return lines[0], lines[1]

def deploy_config(host, username, password, commands):
    device = {
        "device_type": "cisco_ios",
        "host": host,
        "username": username,
        "password": password,
    }
    try:
        with ConnectHandler(**device) as net_connect:
            output = net_connect.send_config_set(commands)
            output += net_connect.save_config()
            result = f"Configuration deployed to {host}:\n{output}"
    except Exception as e:
        result = f"Failed to deploy configuration to {host}: {e}"
    print(result)
    return result


if __name__ == "__main__":
    hosts = read_hosts(HOSTLIST_FILE)
    username, password = read_credentials(PASSWORD_FILE)

    log_file = os.path.join(
        SCRIPT_DIR, datetime.now().strftime("deploy_%Y%m%d_%H%M%S.txt")
    )
    with open(log_file, "w", encoding="utf-8") as f:
        for host in hosts:
            result = deploy_config(host, username, password, CONFIG_COMMANDS)
            f.write(result + "\n" + "=" * 60 + "\n")
    print(f"输出已保存到: {log_file}")


