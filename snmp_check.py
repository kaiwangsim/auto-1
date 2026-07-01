#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snmp_check.py —— SNMPv3 变更前【预检查】脚本 (按 plan.md 实现)

目的
------
在对交换机做 SNMPv3 变更之前, 批量登录设备检查基线配置是否已就位:

  检查项:
    1) SNMPv3 用户存在: show snmp user 中有 snmpv3user
       (snmp-server user 默认不在 running-config 显示, 故用 show snmp user 判断)
    2) snmp-server group snmpv3-priv-group v3 priv read snmpv3-view access ALLOW-SNMPV3
    3) snmp-server view snmpv3-view iso included
    4) snmp-server host 10.224.188.218 version 3 priv snmpv3user
    5) 存在 access-list ALLOW-SNMPv3
    6) 该 ACL 中存在 permit 10.224.188.218

判定
------
  6 项全部满足          -> 写入 output/1_ready_to_change.txt
  任意一项缺失/连不上   -> 写入 output/2_not_ready.txt
  1、2 文件以 hostname/IP 为每台设备的头。
  output/check_summary.csv / .txt 记录每台设备每一项的具体检查情况, 方便检索。

输入
------
  input/hostlist.txt : 每行一个设备 IP 或主机名 (# 开头或空行忽略)
  input/password.txt : 登录凭据。约定:
                         第 1 行 = 用户名
                         第 2 行 = 密码
                         第 3 行 = enable 密码 (可选)
                       若只有 1 行, 则该行视为密码, 用户名取 --username/默认值。
                       可用 --username / --password 命令行参数覆盖。

用法
------
  python snmp_check.py
  python snmp_check.py --server-ip 10.224.188.218 --username cisco
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime

try:
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
    )
    NETMIKO_AVAILABLE = True
except ImportError:                                   # pragma: no cover
    NETMIKO_AVAILABLE = False


# --------------------------------------------------------------------------- #
# 现场可调常量 (按实际环境修改即可)
# --------------------------------------------------------------------------- #
SERVER_IP = "10.224.188.218"     # SNMP 网管/服务器 IP (出现在 snmp host 行 与 ACL permit)
SNMP_USER = "snmpv3user"
VIEW_NAME = "snmpv3-view"
ACL_NAME = "ALLOW-SNMPv3"         # 注意: ACL 名大小写 (plan 里 group 引用写成 ALLOW-SNMPV3,
                                 # 匹配时已做大小写不敏感处理, 不会误判)
DEVICE_TYPE = "cisco_ios"
DEFAULT_USERNAME = "cisco"        # password.txt 只有密码时使用

# 通过 show run | include snmp-server 检查的命令 (这些行 running-config 里会稳定显示)
# 注意: SNMPv3 用户 (snmp-server user ...) 默认不在 running-config 显示, 因此单独用
#       show snmp user 检查, 不放这里。
EXPECTED_SNMP_CMDS = {
    "group_priv": "snmp-server group snmpv3-priv-group v3 priv read snmpv3-view access ALLOW-SNMPV3",
    "view":       f"snmp-server view {VIEW_NAME} iso included",
    "host_cmd":   f"snmp-server host {SERVER_IP} version 3 priv {SNMP_USER}",
}

# 检查项的中文标签 (用于输出, 顺序即报告列顺序)
CHECK_LABELS = {
    "snmp_user":  f"SNMP用户 {SNMP_USER} (show snmp user)",
    "group_priv": "snmp group snmpv3-priv-group(+ACL)",
    "view":       f"snmp view {VIEW_NAME}",
    "host_cmd":   f"snmp host {SERVER_IP}",
    "acl_exists": f"ACL {ACL_NAME} 存在",
    "acl_permit": f"ACL permit {SERVER_IP}",
}

SHOW_SNMP_CMD = "show running-config | include snmp-server"
SHOW_USER_CMD = "show snmp user"
SHOW_ACL_CMD = f"show access-lists {ACL_NAME}"

# 路径相对【脚本自身所在目录】解析, 这样无论从哪个工作目录运行都能找到 input/ 并写 output/
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(SCRIPT_DIR, "input")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
HOSTLIST_FILE = os.path.join(INPUT_DIR, "hostlist.txt")
PASSWORD_FILE = os.path.join(INPUT_DIR, "password.txt")


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def normalize(s):
    """折叠空白 + 转小写, 用于宽松匹配 (容忍多余空格/大小写差异)。"""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def ip_in_line(line, ip):
    """行内是否精确包含某 IP (用数字/点边界, 避免 ...218 误匹配 ...2180)。"""
    return re.search(r"(?<![\d.])" + re.escape(ip) + r"(?![\d.])", line) is not None


def token_in_text(text, token):
    """文本中是否含某个独立词 (大小写不敏感, 词/连字符边界, 避免部分匹配)。"""
    return re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])",
                     text or "", re.IGNORECASE) is not None


def read_hosts(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"找不到主机清单: {path}")
    hosts = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            h = line.strip()
            if h and not h.startswith("#"):
                hosts.append(h)
    return hosts


def read_credentials(path, cli_user, cli_pass):
    """
    读取 input/password.txt:
      >=2 行 -> 行1=用户名, 行2=密码, 行3(可选)=enable 密码
       1 行 -> 该行=密码, 用户名取默认
    命令行 --username / --password 优先覆盖。
    """
    username, password, secret = DEFAULT_USERNAME, "", ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if len(lines) >= 2:
            username, password = lines[0], lines[1]
            if len(lines) >= 3:
                secret = lines[2]
        elif len(lines) == 1:
            password = lines[0]
    if cli_user:
        username = cli_user
    if cli_pass is not None:
        password = cli_pass
    return username, password, secret


# --------------------------------------------------------------------------- #
# 单设备检查
# --------------------------------------------------------------------------- #
def check_device(host, username, password, secret, device_type):
    """返回一个 dict: host / hostname / checks{} / ready / error / raw_snmp / raw_acl"""
    result = {
        "host": host,
        "hostname": "",
        "checks": {k: False for k in CHECK_LABELS},
        "ready": False,
        "error": "",
        "raw_snmp": "",
        "raw_user": "",
        "raw_acl": "",
    }

    conn = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "fast_cli": False,
    }
    if secret:
        conn["secret"] = secret

    try:
        log(f"{host:<16} 连接中 ...")
        net = ConnectHandler(**conn)
    except NetmikoAuthenticationException:
        result["error"] = "认证失败(用户名/密码错误)"
        return result
    except NetmikoTimeoutException:
        result["error"] = "连接超时(SSH 不可达)"
        return result
    except Exception as exc:
        result["error"] = f"连接异常: {exc}"
        return result

    try:
        if secret:
            try:
                net.enable()
            except Exception:
                pass

        # 主机名 (取提示符, 去掉结尾的 # 或 >)
        try:
            result["hostname"] = net.find_prompt().strip().rstrip("#>").strip()
        except Exception:
            result["hostname"] = ""

        snmp_out = net.send_command(SHOW_SNMP_CMD)
        user_out = net.send_command(SHOW_USER_CMD)
        acl_out = net.send_command(SHOW_ACL_CMD)
        result["raw_snmp"] = snmp_out or ""
        result["raw_user"] = user_out or ""
        result["raw_acl"] = acl_out or ""

        if any("% Invalid input" in (o or "") for o in (snmp_out, user_out, acl_out)):
            result["error"] = "命令报错(可能权限不足/语法不支持), 请人工确认"

        # SNMPv3 用户: 直接看 show snmp user 里是否有该用户
        # (snmp-server user 默认不在 running-config 显示, 抓配置判断不可靠)
        result["checks"]["snmp_user"] = token_in_text(user_out, SNMP_USER)

        # 其余 snmp-server 命令检查 (宽松: 大小写不敏感 + 折叠空白)
        norm_snmp = normalize(snmp_out)
        for key, cmd in EXPECTED_SNMP_CMDS.items():
            result["checks"][key] = normalize(cmd) in norm_snmp

        # ACL 是否存在
        result["checks"]["acl_exists"] = "access list" in (acl_out or "").lower()
        # ACL 内是否有 permit <server_ip>
        result["checks"]["acl_permit"] = any(
            ("permit" in ln.lower()) and ip_in_line(ln, SERVER_IP)
            for ln in (acl_out or "").splitlines()
        )

        result["ready"] = all(result["checks"].values())
        return result
    except Exception as exc:
        result["error"] = f"执行异常: {exc}"
        return result
    finally:
        try:
            net.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def missing_labels(result):
    """返回未通过项的中文标签列表 (连接失败则给出错误)。"""
    if result["error"] and not any(result["checks"].values()):
        return [f"错误: {result['error']}"]
    miss = [CHECK_LABELS[k] for k, ok in result["checks"].items() if not ok]
    if result["error"]:
        miss.append(f"备注: {result['error']}")
    return miss


def write_outputs(results, out_dir):
    ts = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    # 每次运行在 output/ 下按时间戳新建子文件夹, 本次结果放里面 (不覆盖历史)
    run_dir = os.path.join(out_dir, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)

    ready = [r for r in results if r["ready"]]
    not_ready = [r for r in results if not r["ready"]]

    f1 = os.path.join(run_dir, "1_ready_to_change.txt")
    f2 = os.path.join(run_dir, "2_not_ready.txt")
    fcsv = os.path.join(run_dir, "check_summary.csv")
    ftxt = os.path.join(run_dir, "check_summary.txt")

    # ---- 1_ready_to_change.txt ----
    with open(f1, "w", encoding="utf-8") as f:
        f.write(f"# 1_ready_to_change —— 6 项检查全部通过, 可进行变更\n")
        f.write(f"# 生成时间: {ts}   server_ip={SERVER_IP}  acl={ACL_NAME}\n")
        f.write(f"# 共 {len(ready)} 台\n\n")
        for r in ready:
            name = r["hostname"] or "(未知主机名)"
            f.write(f"{name}\t{r['host']}\n")

    # ---- 2_not_ready.txt ----
    with open(f2, "w", encoding="utf-8") as f:
        f.write(f"# 2_not_ready —— 存在缺失项或无法检查, 变更前需先处理\n")
        f.write(f"# 生成时间: {ts}   server_ip={SERVER_IP}  acl={ACL_NAME}\n")
        f.write(f"# 共 {len(not_ready)} 台\n\n")
        for r in not_ready:
            name = r["hostname"] or "(未知主机名)"
            f.write(f"{name}\t{r['host']}\n")
            for m in missing_labels(r):
                f.write(f"    - 缺失/问题: {m}\n")
            f.write("\n")

    # ---- check_summary.csv (方便检索/Excel 过滤) ----
    keys = list(CHECK_LABELS.keys())
    with open(fcsv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["host", "hostname", "result"]
                   + [CHECK_LABELS[k] for k in keys]
                   + ["missing", "error"])
        for r in results:
            w.writerow(
                [r["host"], r["hostname"], "READY" if r["ready"] else "NOT_READY"]
                + ["PASS" if r["checks"][k] else "MISSING" for k in keys]
                + ["; ".join(missing_labels(r)) if not r["ready"] else "", r["error"]]
            )

    # ---- check_summary.txt (人读) ----
    with open(ftxt, "w", encoding="utf-8") as f:
        f.write("=" * 78 + "\n")
        f.write("SNMPv3 变更前预检查 —— 总结\n")
        f.write("=" * 78 + "\n")
        f.write(f"时间      : {ts}\n")
        f.write(f"server_ip : {SERVER_IP}\nACL       : {ACL_NAME}\n")
        f.write(f"设备总数  : {len(results)}    READY: {len(ready)}    NOT_READY: {len(not_ready)}\n")
        f.write("-" * 78 + "\n")
        for r in results:
            name = r["hostname"] or "(未知)"
            tag = "READY" if r["ready"] else "NOT_READY"
            f.write(f"\n[{tag}] {name}  {r['host']}\n")
            for k in keys:
                mark = "PASS   " if r["checks"][k] else "MISSING"
                f.write(f"    {mark}  {CHECK_LABELS[k]}\n")
            if r["error"]:
                f.write(f"    备注: {r['error']}\n")
            # 原始回显 (方便复核)
            raw_snmp = (r.get("raw_snmp") or "").strip()
            f.write(f"    --- {SHOW_SNMP_CMD} 原始回显 ---\n")
            if raw_snmp:
                for ln in raw_snmp.splitlines():
                    f.write(f"      {ln}\n")
            else:
                f.write("      (无输出 / 未取到, 可能连接失败或该设备无相关 snmp-server 配置)\n")

            raw_user = (r.get("raw_user") or "").strip()
            f.write("    --- show snmp user 原始回显 ---\n")
            if raw_user:
                for ln in raw_user.splitlines():
                    f.write(f"      {ln}\n")
            else:
                f.write("      (无输出 / 未取到, 可能连接失败或该设备无 SNMPv3 用户)\n")

            raw_acl = (r.get("raw_acl") or "").strip()
            f.write(f"    --- show access-lists {ACL_NAME} 原始回显 ---\n")
            if raw_acl:
                for ln in raw_acl.splitlines():
                    f.write(f"      {ln}\n")
            else:
                f.write("      (无输出 / 未取到, 可能连接失败或该 ACL 不存在)\n")
        f.write("\n" + "=" * 78 + "\n")

    return f1, f2, fcsv, ftxt, ready, not_ready


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="SNMPv3 变更前预检查 (按 plan.md)")
    p.add_argument("--hostlist", default=HOSTLIST_FILE, help="主机清单文件")
    p.add_argument("--password-file", default=PASSWORD_FILE, help="凭据文件")
    p.add_argument("--username", default="", help="用户名(覆盖凭据文件)")
    p.add_argument("--password", default=None, help="密码(覆盖凭据文件)")
    p.add_argument("--server-ip", default=SERVER_IP, help=f"SNMP 服务器 IP, 默认 {SERVER_IP}")
    p.add_argument("--device-type", default=DEVICE_TYPE, help=f"netmiko 设备类型, 默认 {DEVICE_TYPE}")
    p.add_argument("--output-dir", default=OUTPUT_DIR, help="输出目录")
    return p.parse_args(argv)


def main(argv=None):
    # Windows 控制台默认 GBK, 重配为 UTF-8 防止打印崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = parse_args(argv)

    # 允许命令行覆盖 server_ip (同步更新待检查项)
    global SERVER_IP, SHOW_ACL_CMD
    if args.server_ip != SERVER_IP:
        SERVER_IP = args.server_ip
        EXPECTED_SNMP_CMDS["host_cmd"] = f"snmp-server host {SERVER_IP} version 3 priv {SNMP_USER}"
        CHECK_LABELS["host_cmd"] = f"snmp host {SERVER_IP}"
        CHECK_LABELS["acl_permit"] = f"ACL permit {SERVER_IP}"

    if not NETMIKO_AVAILABLE:
        log("错误: 未安装 netmiko, 请先 `pip install netmiko`")
        return 2

    try:
        hosts = read_hosts(args.hostlist)
    except Exception as exc:
        log(f"错误: {exc}")
        return 2
    if not hosts:
        log("主机清单为空")
        return 1

    username, password, secret = read_credentials(
        args.password_file, args.username, args.password)
    log(f"共 {len(hosts)} 台设备; 登录用户: {username}  (server_ip={SERVER_IP}, ACL={ACL_NAME})")
    if not password:
        log("提示: 未读到密码, 请检查 input/password.txt 或用 --password")

    results = []
    for host in hosts:
        r = check_device(host, username, password, secret, args.device_type)
        status = "READY" if r["ready"] else "NOT_READY"
        detail = "" if r["ready"] else " | 缺失: " + "; ".join(missing_labels(r))
        log(f"{host:<16} => {status}{detail}")
        results.append(r)

    f1, f2, fcsv, ftxt, ready, not_ready = write_outputs(results, args.output_dir)

    print("\n" + "=" * 60)
    print(f"检查完成: 共 {len(results)} 台   READY={len(ready)}   NOT_READY={len(not_ready)}")
    print(f"  就绪设备 : {f1}")
    print(f"  待处理   : {f2}")
    print(f"  汇总(CSV): {fcsv}")
    print(f"  汇总(TXT): {ftxt}")
    print("=" * 60)

    # 成功跑完即返回 0 (有设备 NOT_READY 属正常检查结果, 不算脚本失败);
    # 仅在环境/参数错误时返回非 0 (见上面的 return 2)。
    return 0


if __name__ == "__main__":
    sys.exit(main())
