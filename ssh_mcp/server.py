import contextlib
import functools
import hashlib
import json
import logging
import os
import socket
import stat
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import paramiko
import yaml
from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path(__file__).parent.parent / "hosts.yaml"
BACKUP_DIR = Path(__file__).parent.parent / "backups"
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

mcp = FastMCP("ssh-mcp")

connections: dict[str, paramiko.SSHClient] = {}
tunnels: dict[str, list[dict]] = {}
proxy_tunnels: dict[str, dict] = {}

# 日志配置：INFO 及以上写文件，DEBUG 及以上控制台
logger = logging.getLogger("ssh-mcp")
logger.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(
    LOG_DIR / "ssh-mcp.log", encoding="utf-8", mode="a"
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
))
logger.addHandler(_file_handler)

# 进程级异常兜底：捕获 threading/asyncio 等未处理异常
def _global_exception_handler(exc_type, exc_val, exc_tb):
    logger.critical(f"[进程级异常] {exc_type.__name__}: {exc_val}\n{''.join(traceback.format_tb(exc_tb))}")

import sys
sys.excepthook = _global_exception_handler
threading.excepthook = lambda args: logger.critical(
    f"[线程异常] {args.exc_type.__name__}: {args.exc_value}\n{''.join(traceback.format_tb(args.exc_traceback))}"
)

logger.info("=" * 50)
logger.info(f"[MCP启动] Python {sys.version}, PID={os.getpid()}")
logger.info("=" * 50)


# ============================================================
# 基础设施：异常兜底 + 连接管理
# ============================================================


def _safe_tool(func):
    """装饰器：捕获所有未处理异常，记录日志，防止 MCP 服务器进程崩溃。同时记录每次调用的入参和耗时。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # 构建调用摘要（截断过长的参数值）
        call_args = []
        for k, v in kwargs.items():
            s = str(v)
            call_args.append(f"{k}={s[:100]}{'...' if len(s) > 100 else ''}")
        call_summary = f"{func.__name__}({', '.join(call_args)})"
        logger.info(f"[调用] {call_summary}")
        start = time.monotonic()
        try:
            result = func(*args, **kwargs)
            elapsed = time.monotonic() - start
            # 截断结果用于日志
            result_preview = result[:200] + "..." if len(result) > 200 else result
            logger.info(f"[完成] {func.__name__} ({elapsed:.2f}s) -> {result_preview}")
            return result
        except paramiko.SSHException as e:
            elapsed = time.monotonic() - start
            logger.warning(f"[{func.__name__}] SSH异常 ({elapsed:.2f}s): {e}")
            # 清理死连接
            identifier = kwargs.get("identifier") or (args[0] if args else None)
            if identifier:
                info = _resolve_host(identifier)
                alias = info["alias"] if info else identifier
                dead = connections.pop(alias, None)
                if dead:
                    try:
                        dead.close()
                    except Exception:
                        pass
                    logger.info(f"[自动清理] {alias} 死连接已移除")
            return f"SSH 连接已断开（已自动清理）。请调用 connect_host 重新连接后重试。"
        except ConnectionError as e:
            elapsed = time.monotonic() - start
            logger.warning(f"[{func.__name__}] 连接错误 ({elapsed:.2f}s): {e}")
            return f"连接错误：{type(e).__name__}: {e}"
        except PermissionError as e:
            elapsed = time.monotonic() - start
            logger.warning(f"[{func.__name__}] 权限拒绝 ({elapsed:.2f}s): {e}")
            return f"权限不足：{e}"
        except TimeoutError as e:
            elapsed = time.monotonic() - start
            logger.warning(f"[{func.__name__}] 超时 ({elapsed:.2f}s): {e}")
            return f"操作超时：{e}"
        except FileNotFoundError as e:
            elapsed = time.monotonic() - start
            logger.warning(f"[{func.__name__}] 文件不存在 ({elapsed:.2f}s): {e}")
            return f"文件不存在：{e}"
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error(f"[{func.__name__}] 未捕获异常 ({elapsed:.2f}s): {traceback.format_exc()}")
            return f"内部错误：{type(e).__name__}: {e}"

    return wrapper


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_host(identifier: str) -> dict | None:
    config = _load_config()
    # 1. 精确匹配别名
    if identifier in config:
        entry = config[identifier]
        return {
            "alias": identifier,
            "host": entry["host"],
            "port": entry.get("port", 22),
            "username": entry.get("username", "root"),
            "password": entry.get("password", ""),
        }
    # 2. 匹配 host:port 格式（如 106.53.212.173:6001）
    if ":" in identifier:
        host_part, _, port_part = identifier.rpartition(":")
        if port_part.isdigit():
            for alias, entry in config.items():
                if entry.get("host") == host_part and entry.get("port", 22) == int(port_part):
                    return {
                        "alias": alias,
                        "host": entry["host"],
                        "port": entry.get("port", 22),
                        "username": entry.get("username", "root"),
                        "password": entry.get("password", ""),
                    }
    # 3. 匹配 IP（仅当唯一时）
    matches = []
    for alias, entry in config.items():
        if entry.get("host") == identifier:
            matches.append((alias, entry))
    if len(matches) == 1:
        alias, entry = matches[0]
        return {
            "alias": alias,
            "host": entry["host"],
            "port": entry.get("port", 22),
            "username": entry.get("username", "root"),
            "password": entry.get("password", ""),
        }
    if len(matches) > 1:
        # 多个主机同名 IP，返回特殊标记让调用者给出提示
        aliases = [a for a, _ in matches]
        return {
            "alias": "__ambiguous__",
            "ambiguous_aliases": aliases,
            "host": identifier,
        }
    return None


def _get_alias(identifier: str) -> str:
    """从 identifier 解析别名，处理歧义情况。"""
    info = _resolve_host(identifier)
    if info is None:
        return identifier
    if info.get("alias") == "__ambiguous__":
        return identifier
    return info["alias"]


def _is_client_alive(client: paramiko.SSHClient) -> bool:
    """检测 SSH 连接是否存活。"""
    try:
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            return False
        return True
    except Exception:
        return False


def _do_connect(host: str, port: int, username: str, password: str) -> paramiko.SSHClient:
    """建立 SSH 连接，带重试逻辑。"""
    last_err = None
    for attempt in range(3):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                timeout=10,
                banner_timeout=15,
                auth_timeout=15,
                allow_agent=False,
                look_for_keys=False,
            )
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)
            return client
        except Exception as e:
            last_err = e
            try:
                client.close()
            except Exception:
                pass
            logger.warning(f"[连接重试] 第{attempt+1}次失败: {e}")
            time.sleep(2)
    raise last_err



def _ensure_alive(alias: str) -> paramiko.SSHClient | None:
    """获取连接，如果已断开则清理并返回 None（不自动重连，避免阻塞 MCP 进程）。"""
    client = connections.get(alias)
    if client is not None and _is_client_alive(client):
        return client
    # 连接已断开，仅清理
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
        connections.pop(alias, None)
        logger.info(f"[连接清理] {alias} 已断开")
    return None


def _exec_on(client: paramiko.SSHClient, command: str, timeout: int = 30) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    exit_code = stdout.channel.recv_exit_status()
    result = ""
    if out:
        result += out
    if err:
        result += ("[STDERR]\n" + err) if result else err
    result += f"\n[Exit Code: {exit_code}]"
    return result.strip()


def _local_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


@contextlib.contextmanager
def _sftp(client: paramiko.SSHClient):
    """SFTP 上下文管理器，确保异常时也会关闭 SFTP 会话。"""
    sftp = client.open_sftp()
    try:
        yield sftp
    finally:
        try:
            sftp.close()
        except Exception:
            pass


def _remote_sha256(client: paramiko.SSHClient, path: str) -> str | None:
    result = _exec_on(client, f'sha256sum "{path}" 2>/dev/null', timeout=30)
    if "[Exit Code: 0]" in result:
        return result.split()[0]
    return None


def _mkdirs_remote(sftp: paramiko.SFTPClient, path: str):
    parts = path.strip("/").split("/")
    cur = "/" if path.startswith("/") else ""
    for part in parts:
        cur = cur + part + "/" if cur.endswith("/") else cur + "/" + part
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def _resolve_aliases(identifiers: list[str]) -> list[str]:
    aliases = []
    for ident in identifiers:
        info = _resolve_host(ident)
        aliases.append(info["alias"] if info else ident)
    return aliases


# ============================================================
# 连接管理
# ============================================================


@mcp.tool()
@_safe_tool
def health_check() -> str:
    """SSH MCP 健康检查。探测 MCP 服务是否存活、当前有多少 SSH 连接、每个连接是否正常。当 MCP 工具调用失败或断连时，先用此工具排查。"""
    config = _load_config()
    lines = [f"SSH MCP 服务运行正常 (连接数: {len(connections)}, 隧道: {len(tunnels)}, 代理: {len(proxy_tunnels)})"]

    if connections:
        lines.append("\n连接状态：")
        dead = []
        for alias, client in list(connections.items()):
            alive = _is_client_alive(client)
            status = "存活" if alive else "已断开"
            entry = config.get(alias, {})
            host = entry.get("host", "?")
            lines.append(f"  {alias} ({host}): {status}")
            if not alive:
                dead.append(alias)

        for alias in dead:
            try:
                connections[alias].close()
            except Exception:
                pass
            connections.pop(alias, None)
            lines.append(f"  -> 已清理断开的连接: {alias}")

    return "\n".join(lines)


@mcp.tool()
@_safe_tool
def list_hosts() -> str:
    """列出 hosts.yaml 中所有已配置的 SSH 主机，显示别名、IP、端口、用户名、描述和当前连接状态（已连接/未连接）。"""
    config = _load_config()
    if not config:
        return "没有找到主机配置。请在 hosts.yaml 中添加主机。"
    lines = ["已配置的主机：\n"]
    for alias, entry in config.items():
        host = entry.get("host", "N/A")
        port = entry.get("port", 22)
        username = entry.get("username", "root")
        desc = entry.get("description", "")
        client = connections.get(alias)
        status = "已连接" if (client and _is_client_alive(client)) else "未连接"
        lines.append(
            f"  [{status}] {alias} - {username}@{host}:{port}"
            + (f" ({desc})" if desc else "")
        )
    return "\n".join(lines)


@mcp.tool()
@_safe_tool
def connect_host(identifier: str, username: str = "", password: str = "", port: int = 0) -> str:
    """通过别名或 IP 建立 SSH 连接。连接成功后才能使用其他远程操作工具。如果主机已连接则跳过。使用前需在 hosts.yaml 中配置主机信息。连接失败时自动重试3次。连接断开后其他工具会自动尝试重连，无需手动调用此函数。

    Args:
        identifier: 主机别名（如 "主机1"）或 IP 地址
        username: 可选，覆盖配置中的用户名
        password: 可选，覆盖配置中的密码
        port: 可选，覆盖配置中的端口
    """
    info = _resolve_host(identifier)
    if info is None:
        if "@" in identifier or identifier.replace(".", "").isdigit():
            return f"未找到主机 '{identifier}' 的配置。请先在 hosts.yaml 中添加，或提供用户名和密码。"
        return f"未找到主机 '{identifier}'。请检查别名是否正确。"

    # 同名 IP 多主机歧义
    if info.get("alias") == "__ambiguous__":
        aliases = info.get("ambiguous_aliases", [])
        port_examples = ", ".join(f"{a}" for a in aliases)
        return f"IP {identifier} 对应多台主机：{port_examples}。请使用别名连接。"

    alias = info["alias"]

    # 检查是否已有存活连接
    existing = connections.get(alias)
    if existing is not None and _is_client_alive(existing):
        return f"主机 '{alias}' 已经连接。"
    # 清理死连接
    if existing is not None:
        try:
            existing.close()
        except Exception:
            pass
        connections.pop(alias, None)

    host = info["host"]
    p = port or info["port"]
    user = username or info["username"]
    pwd = password or info["password"]

    client = _do_connect(host, p, user, pwd)
    connections[alias] = client
    logger.info(f"[连接成功] {alias} ({user}@{host}:{p})")
    return f"成功连接到 {alias} ({user}@{host}:{p})"


@mcp.tool()
@_safe_tool
def disconnect_host(identifier: str) -> str:
    """断开指定主机的 SSH 连接并释放资源。断开后需重新 connect_host 才能继续操作该主机。

    Args:
        identifier: 主机别名或 IP 地址
    """
    info = _resolve_host(identifier)
    alias = info["alias"] if info else identifier

    # 清理关联的隧道
    for t in tunnels.pop(alias, []):
        t["stop_event"].set()
        try:
            t["server_sock"].close()
        except OSError:
            pass

    # 清理关联的代理
    proxy_info = proxy_tunnels.pop(alias, None)
    if proxy_info:
        proxy_info["stop_event"].set()
        try:
            proxy_info["transport"].cancel_port_forward("127.0.0.1", proxy_info["remote_proxy_port"])
        except Exception:
            pass

    client = connections.pop(alias, None)
    if client is None:
        return f"主机 '{identifier}' 未连接。"
    client.close()
    logger.info(f"[断开连接] {alias}")
    return f"已断开与 {alias} 的连接。"


@mcp.tool()
@_safe_tool
def exec_command(identifier: str, command: str, timeout: int = 30) -> str:
    """在远程主机上执行任意 Shell 命令，返回 stdout、stderr 和退出码。这是最基础的远程操作工具，其他高级工具内部也依赖它。

    Args:
        identifier: 主机别名或 IP 地址
        command: 要执行的命令
        timeout: 超时时间（秒），默认30
    """
    info = _resolve_host(identifier)
    alias = info["alias"] if info else identifier

    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先使用 connect_host 连接。"

    return _exec_on(client, command, timeout)


# ============================================================
# 文件传输
# ============================================================


@mcp.tool()
@_safe_tool
def upload_file(identifier: str, local_path: str, remote_path: str) -> str:
    """将本地单个文件上传到远程主机。remote_path 以 / 结尾则视为目录（自动拼接文件名），否则视为完整目标路径。

    Args:
        identifier: 主机别名或 IP 地址
        local_path: 本地文件路径
        remote_path: 远程目标路径（以 / 结尾视为目录，否则视为完整路径）
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先 connect_host。"
    if not os.path.isfile(local_path):
        return f"本地文件不存在：{local_path}"

    filename = os.path.basename(local_path)
    dest = remote_path.rstrip("/") + "/" + filename if remote_path.endswith("/") else remote_path
    with _sftp(client) as sftp:
        sftp.put(local_path, dest)
    return f"文件上传成功：{local_path} -> {alias}:{dest}"


@mcp.tool()
@_safe_tool
def download_file(identifier: str, remote_path: str, local_path: str) -> str:
    """从远程主机下载单个文件到本地。自动创建本地目录。local_path 以 / 或 \\ 结尾则视为目录（自动拼接文件名）。

    Args:
        identifier: 主机别名或 IP 地址
        remote_path: 远程文件路径
        local_path: 本地保存路径（以 / 或 \\ 结尾视为目录）
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先 connect_host。"

    if local_path.endswith(("/", "\\")):
        os.makedirs(local_path, exist_ok=True)
        local_path = os.path.join(local_path, os.path.basename(remote_path))
    else:
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    with _sftp(client) as sftp:
        sftp.get(remote_path, local_path)
    return f"文件下载成功：{alias}:{remote_path} -> {local_path}"


@mcp.tool()
@_safe_tool
def upload_dir(identifier: str, local_dir: str, remote_dir: str) -> str:
    """递归上传整个本地目录到远程主机，保留子目录结构。远程自动创建目标目录。

    Args:
        identifier: 主机别名或 IP 地址
        local_dir: 本地目录路径
        remote_dir: 远程目标目录路径
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先 connect_host。"
    if not os.path.isdir(local_dir):
        return f"本地目录不存在：{local_dir}"

    count = 0

    def _upload_recursive(sftp, local_base: str, remote_base: str):
        nonlocal count
        _mkdirs_remote(sftp, remote_base)
        for entry in os.scandir(local_base):
            remote_entry = remote_base.rstrip("/") + "/" + entry.name
            if entry.is_file():
                sftp.put(entry.path, remote_entry)
                count += 1
            elif entry.is_dir():
                _upload_recursive(sftp, entry.path, remote_entry)

    with _sftp(client) as sftp:
        _upload_recursive(sftp, local_dir, remote_dir)
    return f"目录上传成功：{local_dir} -> {alias}:{remote_dir}（共 {count} 个文件）"


@mcp.tool()
@_safe_tool
def download_dir(identifier: str, remote_dir: str, local_dir: str) -> str:
    """递归下载远程目录到本地，保留子目录结构。本地自动创建目标目录。

    Args:
        identifier: 主机别名或 IP 地址
        remote_dir: 远程目录路径
        local_dir: 本地目标目录路径
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先 connect_host。"

    count = 0

    def _download_recursive(sftp, remote_base: str, local_base: str):
        nonlocal count
        os.makedirs(local_base, exist_ok=True)
        for entry in sftp.listdir_attr(remote_base):
            remote_entry = remote_base.rstrip("/") + "/" + entry.filename
            local_entry = os.path.join(local_base, entry.filename)
            if stat.S_ISDIR(entry.st_mode):
                _download_recursive(sftp, remote_entry, local_entry)
            else:
                sftp.get(remote_entry, local_entry)
                count += 1

    with _sftp(client) as sftp:
        _download_recursive(sftp, remote_dir, local_dir)
    return f"目录下载成功：{alias}:{remote_dir} -> {local_dir}（共 {count} 个文件）"


@mcp.tool()
@_safe_tool
def remote_file_info(identifier: str, remote_path: str) -> str:
    """查看远程文件或目录的详细信息：类型、大小、权限、修改时间、SHA256（仅文件）。用于确认文件状态或比对是否被修改。

    Args:
        identifier: 主机别名或 IP 地址
        remote_path: 远程文件路径
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先 connect_host。"

    with _sftp(client) as sftp:
        attr = sftp.stat(remote_path)

    perms = stat.filemode(attr.st_mode)
    size = attr.st_size
    mtime = datetime.fromtimestamp(attr.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    file_type = "目录" if stat.S_ISDIR(attr.st_mode) else "文件"

    lines = [
        f"=== {alias}:{remote_path} ===",
        f"类型: {file_type}",
        f"大小: {size} 字节 ({size / 1024:.1f} KB)",
        f"权限: {perms}",
        f"修改时间: {mtime}",
    ]

    if file_type == "文件":
        sha = _remote_sha256(client, remote_path)
        if sha:
            lines.append(f"SHA256: {sha}")

    return "\n".join(lines)


@mcp.tool()
@_safe_tool
def sync_file(identifier: str, local_path: str, remote_path: str) -> str:
    """智能文件同步：先比对本地和远程文件的 SHA256，相同则跳过，不同才上传。适合重复部署相同文件的场景，避免无谓传输。

    Args:
        identifier: 主机别名或 IP 地址
        local_path: 本地文件路径
        remote_path: 远程目标路径
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先 connect_host。"
    if not os.path.isfile(local_path):
        return f"本地文件不存在：{local_path}"

    local_hash = _local_sha256(local_path)
    remote_hash = _remote_sha256(client, remote_path)

    if local_hash == remote_hash:
        return f"文件已是最新，无需同步（SHA256: {local_hash[:16]}...）"

    with _sftp(client) as sftp:
        sftp.put(local_path, remote_path)
    return f"文件已同步：{local_path} -> {alias}:{remote_path}\n  本地  SHA256: {local_hash[:16]}...\n  远程旧 SHA256: {remote_hash[:16] if remote_hash else 'N/A'}..."


# ============================================================
# 文件备份与编辑
# ============================================================


@mcp.tool()
@_safe_tool
def backup_remote_file(identifier: str, remote_path: str, tag: str = "") -> str:
    """将远程文件备份到本地 backups/<主机别名>/ 目录，带时间戳和可选标签。修改远程配置文件前建议先备份，配合 restore_remote_file 可随时回退。

    Args:
        identifier: 主机别名或 IP 地址
        remote_path: 远程文件路径
        tag: 可选标签，用于区分同一文件的多个备份
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先 connect_host。"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.basename(remote_path)
    tag_suffix = f"_{tag}" if tag else ""
    backup_name = f"{alias}_{filename}{tag_suffix}_{timestamp}"
    backup_path = BACKUP_DIR / alias / backup_name
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    with _sftp(client) as sftp:
        sftp.get(remote_path, str(backup_path))
    return f"备份成功：{alias}:{remote_path} -> {backup_path}"


@mcp.tool()
@_safe_tool
def restore_remote_file(identifier: str, remote_path: str, tag: str = "", backup_file: str = "") -> str:
    """从本地 backups/ 目录恢复备份到远程主机。默认恢复最新的匹配备份，可按 tag 或精确文件名指定备份版本。配合 backup_remote_file 使用。

    Args:
        identifier: 主机别名或 IP 地址
        remote_path: 远程文件路径
        tag: 可选，恢复指定标签的备份
        backup_file: 可选，精确指定备份文件名（优先于 tag）
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先 connect_host。"

    alias_backup_dir = BACKUP_DIR / alias
    if not alias_backup_dir.exists():
        return f"没有找到主机 '{alias}' 的备份目录。"

    filename = os.path.basename(remote_path)
    tag_suffix = f"_{tag}" if tag else ""

    if backup_file:
        candidates = list(alias_backup_dir.glob(backup_file))
    else:
        pattern = f"{alias}_{filename}{tag_suffix}_*"
        candidates = sorted(alias_backup_dir.glob(pattern))

    if not candidates:
        return f"没有找到匹配的备份文件。模式：{pattern if not backup_file else backup_file}"

    latest = candidates[-1]

    with _sftp(client) as sftp:
        sftp.put(str(latest), remote_path)
    return f"恢复成功：{latest.name} -> {alias}:{remote_path}"


# ============================================================
# 主机状态
# ============================================================


@mcp.tool()
@_safe_tool
def get_host_status(identifier: str) -> str:
    """一键获取远程主机的综合状态报告：系统信息、运行时间、CPU、内存、磁盘、网络监听端口。用于快速了解服务器健康状况。

    Args:
        identifier: 主机别名或 IP 地址
    """
    info = _resolve_host(identifier)
    alias = info["alias"] if info else identifier

    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先使用 connect_host 连接。"

    commands = {
        "系统信息": "uname -a",
        "运行时间和负载": "uptime",
        "CPU 信息": "lscpu | head -20",
        "内存使用": "free -h",
        "磁盘使用": "df -h",
        "网络连接状态": "ss -tuln | head -20",
    }

    results = [f"=== 主机 {alias} ({info['host'] if info else identifier}) 状态 ===\n"]
    for label, cmd in commands.items():
        try:
            result = _exec_on(client, cmd, timeout=10)
            results.append(f"--- {label} ---\n{result}\n")
        except Exception as e:
            results.append(f"--- {label} ---\n获取失败: {e}\n")

    return "\n".join(results)


# ============================================================
# 多主机批量操作
# ============================================================


@mcp.tool()
@_safe_tool
def exec_on_hosts(identifiers: list[str], command: str, timeout: int = 30) -> str:
    """在多台主机上并行执行同一命令并汇总结果。适合集群运维场景，如同时检查多台机器的运行状态。内部使用线程池并发执行。

    Args:
        identifiers: 主机别名或 IP 列表，如 ["主机1", "主机2"]
        command: 要执行的命令
        timeout: 超时时间（秒）
    """
    aliases = _resolve_aliases(identifiers)
    results = {}

    def _run(alias: str) -> tuple[str, str]:
        client = _ensure_alive(alias)
        if client is None:
            return alias, "未连接或已断开，请先 connect_host"
        try:
            return alias, _exec_on(client, command, timeout)
        except Exception as e:
            return alias, f"执行失败：{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=min(len(aliases), 10)) as pool:
        futures = {pool.submit(_run, a): a for a in aliases}
        for fut in as_completed(futures):
            alias, output = fut.result()
            results[alias] = output

    lines = [f"=== 批量执行: {command} ==="]
    for alias in sorted(results):
        lines.append(f"\n--- {alias} ---\n{results[alias]}")
    return "\n".join(lines)


@mcp.tool()
@_safe_tool
def batch_upload(identifiers: list[str], local_path: str, remote_path: str) -> str:
    """将同一个本地文件并发上传到多台主机的同一远程路径。适合批量分发配置文件、脚本等。

    Args:
        identifiers: 主机别名或 IP 列表
        local_path: 本地文件路径
        remote_path: 远程目标路径
    """
    if not os.path.isfile(local_path):
        return f"本地文件不存在：{local_path}"

    aliases = _resolve_aliases(identifiers)
    results = {}

    def _upload(alias: str) -> tuple[str, str]:
        client = _ensure_alive(alias)
        if client is None:
            return alias, "未连接或已断开"
        try:
            with _sftp(client) as sftp:
                sftp.put(local_path, remote_path)
            return alias, "成功"
        except Exception as e:
            return alias, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=min(len(aliases), 10)) as pool:
        futures = {pool.submit(_upload, a): a for a in aliases}
        for fut in as_completed(futures):
            alias, status = fut.result()
            results[alias] = status

    ok = sum(1 for s in results.values() if s == "成功")
    lines = [f"批量上传 {local_path} -> {remote_path}  (成功 {ok}/{len(results)})"]
    for alias in sorted(results):
        lines.append(f"  {alias}: {results[alias]}")
    return "\n".join(lines)


# ============================================================
# 进程与服务管理
# ============================================================


@mcp.tool()
@_safe_tool
def manage_service(identifier: str, service: str, action: str) -> str:
    """管理远程主机的 systemd 服务。支持 start、stop、restart、status、enable、disable 操作。自动检测 systemctl 或 service 命令，操作后回查当前状态。

    Args:
        identifier: 主机别名或 IP 地址
        service: 服务名称，如 nginx、docker、sshd
        action: 操作：start / stop / restart / status / enable / disable
    """
    valid = {"start", "stop", "restart", "status", "enable", "disable"}
    if action not in valid:
        return f"无效操作 '{action}'，可选：{', '.join(valid)}"

    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。请先 connect_host。"

    use_systemctl = _exec_on(client, "which systemctl", timeout=5)
    if "[Exit Code: 0]" in use_systemctl:
        cmd = f"sudo systemctl {action} {service}"
    else:
        cmd = f"sudo service {service} {action}"

    result = _exec_on(client, cmd, timeout=30)

    if action == "status":
        return f"--- {alias}: {service} {action} ---\n{result}"

    check = _exec_on(client, f"sudo systemctl is-active {service} 2>/dev/null || echo unknown", timeout=5)
    active = check.split("\n")[0].strip().replace("[Exit Code: 0]", "").strip()
    return f"--- {alias}: sudo {action} {service} ---\n{result}\n当前状态: {active}"


@mcp.tool()
@_safe_tool
def list_processes(identifier: str, filter_name: str = "") -> str:
    """列出远程主机上的进程。默认按内存占用排序显示前 30 个，可按名称过滤（支持正则表达式）。

    Args:
        identifier: 主机别名或 IP 地址
        filter_name: 可选，按进程名过滤（支持 grep 模式）
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。"

    cmd = "ps aux --sort=-%mem | head -30"
    if filter_name:
        cmd = f"ps aux | grep -E '{filter_name}' | grep -v grep"

    return _exec_on(client, cmd, timeout=10)


@mcp.tool()
@_safe_tool
def tail_log(identifier: str, log_path: str, lines: int = 50) -> str:
    """读取远程日志文件的最后 N 行。用于快速查看服务日志、系统日志等，无需下载整个文件。

    Args:
        identifier: 主机别名或 IP 地址
        log_path: 日志文件路径
        lines: 读取行数，默认 50
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。"

    return _exec_on(client, f"tail -n {lines} '{log_path}'", timeout=15)


@mcp.tool()
@_safe_tool
def list_services(identifier: str, status_filter: str = "") -> str:
    """列出远程主机上的 systemd 服务。默认显示 running 状态的服务，可按状态过滤（running/failed/exited/active）。

    Args:
        identifier: 主机别名或 IP 地址
        status_filter: 可选，按状态过滤：running / failed / exited / active
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。"

    if status_filter:
        cmd = f"sudo systemctl list-units --type=service --state={status_filter} --no-pager"
    else:
        cmd = "sudo systemctl list-units --type=service --state=running --no-pager | head -30"

    return _exec_on(client, cmd, timeout=15)


# ============================================================
# SSH 隧道 / 端口转发（线程模式，兼容 Windows）
# ============================================================


def _pipe_thread(src, dst, stop_event):
    """单向数据管道：从 src 读，写到 dst。Windows 兼容，不使用 select。"""
    try:
        src.settimeout(1.0)
    except Exception:
        pass
    try:
        while not stop_event.is_set():
            try:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
            except socket.timeout:
                continue
            except (OSError, socket.error):
                break
    finally:
        try:
            dst.close()
        except Exception:
            pass


def _forward_pair(local_sock, chan, stop_event):
    """启动两个 pipe_thread 做双向转发。"""
    t1 = threading.Thread(target=_pipe_thread, args=(local_sock, chan, stop_event), daemon=True)
    t2 = threading.Thread(target=_pipe_thread, args=(chan, local_sock, stop_event), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    try:
        local_sock.close()
    except Exception:
        pass
    try:
        chan.close()
    except Exception:
        pass


def _start_forwarder(alias, local_port, remote_host, remote_port):
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_sock.bind(("127.0.0.1", local_port))
    except OSError as e:
        return None, str(e)
    server_sock.listen(5)
    server_sock.settimeout(1.0)

    transport = connections[alias].get_transport()
    stop_event = threading.Event()

    def accept_loop():
        while not stop_event.is_set():
            try:
                local_sock, _ = server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                chan = transport.open_channel(
                    "direct-tcpip",
                    (remote_host, remote_port),
                    local_sock.getpeername(),
                )
            except Exception:
                local_sock.close()
                continue
            threading.Thread(target=_forward_pair, args=(local_sock, chan, stop_event), daemon=True).start()

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    return {"thread": t, "server_sock": server_sock, "stop_event": stop_event}, None


@mcp.tool()
@_safe_tool
def create_tunnel(identifier: str, local_port: int, remote_host: str = "127.0.0.1", remote_port: int = 0) -> str:
    """创建 SSH 本地端口转发隧道（相当于 ssh -L）。将本地端口映射到远程网络的指定地址，可访问远程内网服务。例如转发本地 3306 到远程 MySQL。

    Args:
        identifier: 主机别名或 IP 地址
        local_port: 本地监听端口
        remote_host: 远程目标地址，默认 127.0.0.1（即远程主机自身）
        remote_port: 远程目标端口，默认与 local_port 相同
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。"

    rp = remote_port or local_port

    if alias in tunnels:
        for t in tunnels[alias]:
            if t["local_port"] == local_port:
                return f"隧道已存在：127.0.0.1:{local_port} -> {alias}:{remote_host}:{rp}"

    forwarder, err = _start_forwarder(alias, local_port, remote_host, rp)
    if err:
        return f"隧道创建失败：{err}"

    forwarder["local_port"] = local_port
    forwarder["remote_host"] = remote_host
    forwarder["remote_port"] = rp
    tunnels.setdefault(alias, []).append(forwarder)

    return f"隧道已建立：127.0.0.1:{local_port} -> {alias}({remote_host}:{rp})"


@mcp.tool()
@_safe_tool
def close_tunnel(identifier: str, local_port: int) -> str:
    """关闭指定的 SSH 本地端口转发隧道，释放本地端口。

    Args:
        identifier: 主机别名或 IP 地址
        local_port: 要关闭的本地端口号
    """
    alias = _get_alias(identifier)
    active = tunnels.get(alias, [])
    for i, t in enumerate(active):
        if t.get("local_port") == local_port:
            t["stop_event"].set()
            try:
                t["server_sock"].close()
            except OSError:
                pass
            active.pop(i)
            if not active:
                tunnels.pop(alias, None)
            return f"隧道已关闭：127.0.0.1:{local_port}"
    return f"未找到隧道：127.0.0.1:{local_port}"


@mcp.tool()
@_safe_tool
def list_tunnels(identifier: str = "") -> str:
    """列出当前所有活动的 SSH 端口转发隧道，显示本地端口到远程地址的映射关系。不传参数则列出所有主机。

    Args:
        identifier: 可选，主机别名或 IP 地址
    """
    if identifier:
        info = _resolve_host(identifier)
        alias = info["alias"] if info else identifier
        targets = {alias: tunnels.get(alias, [])}
    else:
        targets = tunnels

    if not targets:
        return "当前没有活动的隧道。"

    lines = []
    for alias, active in targets.items():
        if not active:
            continue
        lines.append(f"--- {alias} ---")
        for t in active:
            lines.append(f"  127.0.0.1:{t['local_port']} -> {t['remote_host']}:{t['remote_port']}")

    return "\n".join(lines) if lines else "当前没有活动的隧道。"


# ============================================================
# 远程文件搜索与编辑
# ============================================================


@mcp.tool()
@_safe_tool
def find_remote(identifier: str, path: str = "/", name: str = "", type_filter: str = "", max_depth: int = 0, limit: int = 50) -> str:
    """在远程主机上按条件搜索文件，封装 Linux find 命令。支持按名称通配符、文件类型、搜索深度过滤，限制返回数量。

    Args:
        identifier: 主机别名或 IP 地址
        path: 搜索起始路径，默认 /
        name: 文件名匹配模式（支持通配符），如 "*.log"、"nginx*"
        type_filter: 类型过滤：f=文件, d=目录, l=链接
        max_depth: 最大搜索深度，0=不限制
        limit: 最多返回条数，默认 50
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。"

    parts = [f"find '{path}'"]
    if max_depth > 0:
        parts.append(f"-maxdepth {max_depth}")
    if name:
        parts.append(f"-name '{name}'")
    if type_filter:
        parts.append(f"-type {type_filter}")
    parts.append(f"| head -{limit}")

    return _exec_on(client, " ".join(parts), timeout=30)


@mcp.tool()
@_safe_tool
def read_remote_file(identifier: str, remote_path: str, encoding: str = "utf-8") -> str:
    """读取远程文件的全部内容并返回文本。适合查看配置文件、日志等，无需下载。支持指定编码。

    Args:
        identifier: 主机别名或 IP 地址
        remote_path: 远程文件路径
        encoding: 文件编码，默认 utf-8
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。"

    with _sftp(client) as sftp:
        with sftp.open(remote_path, "r") as f:
            content = f.read().decode(encoding, errors="replace")
    return f"=== {alias}:{remote_path} ===\n{content}"


@mcp.tool()
@_safe_tool
def edit_remote_file(identifier: str, remote_path: str, content: str, auto_backup: bool = True) -> str:
    """将完整内容写入远程文件，覆盖原有内容。默认先自动备份原文件（配合 backup_remote_file），可通过 auto_backup=False 关闭。

    Args:
        identifier: 主机别名或 IP 地址
        remote_path: 远程文件路径
        content: 要写入的完整内容
        auto_backup: 是否自动备份原文件，默认 True
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。"

    if auto_backup:
        try:
            backup_remote_file(identifier, remote_path, tag="edit")
        except Exception:
            pass

    with _sftp(client) as sftp:
        with sftp.open(remote_path, "w") as f:
            f.write(content.encode("utf-8"))
    return f"文件已写入：{alias}:{remote_path}"


# ============================================================
# 代理（SSH 远程端口转发，线程模式，兼容 Windows）
# ============================================================


def _reverse_accept_loop(transport, local_host, local_port, stop_event):
    """循环接受远程转发来的连接，每个连接起线程对转发到本地代理。"""
    while not stop_event.is_set():
        try:
            chan = transport.accept(1.0)
        except Exception:
            break
        if chan is None:
            continue
        try:
            local_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            local_sock.connect((local_host, local_port))
        except Exception:
            chan.close()
            continue
        threading.Thread(target=_forward_pair, args=(local_sock, chan, stop_event), daemon=True).start()


@mcp.tool()
@_safe_tool
def setup_proxy(identifier: str, local_proxy_port: int = 10808, remote_proxy_port: int = 11080) -> str:
    """建立反向代理隧道（SSH 远程端口转发），让远程服务器通过你本地电脑的代理（如 Clash、V2Ray）上网。使用前需确保本地代理已在运行。配合 exec_with_proxy 使用。

    流量路径：远程服务器 -> SSH隧道 -> 本地代理(10808) -> 目标网站

    Args:
        identifier: 主机别名或 IP 地址
        local_proxy_port: 本地代理端口，默认 10808
        remote_proxy_port: 远程服务器上的监听端口，默认 1080
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。"

    if alias in proxy_tunnels:
        return "代理隧道已存在。请先 teardown_proxy 再重新建立。"

    transport = client.get_transport()

    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test_sock.connect(("127.0.0.1", local_proxy_port))
        test_sock.close()
    except ConnectionRefusedError:
        return f"本地代理 127.0.0.1:{local_proxy_port} 未运行，请先启动代理。"
    except Exception as e:
        return f"本地代理连接失败：{e}"

    try:
        transport.request_port_forward("127.0.0.1", remote_proxy_port)
    except Exception as e:
        return f"远程端口转发请求失败（可能端口 {remote_proxy_port} 已被占用）：{e}"

    stop_event = threading.Event()
    t = threading.Thread(
        target=_reverse_accept_loop,
        args=(transport, "127.0.0.1", local_proxy_port, stop_event),
        daemon=True,
    )
    t.start()

    proxy_tunnels[alias] = {
        "transport": transport,
        "local_proxy_port": local_proxy_port,
        "remote_proxy_port": remote_proxy_port,
        "stop_event": stop_event,
        "thread": t,
    }

    return (
        f"代理隧道已建立：\n"
        f"  远程服务器 127.0.0.1:{remote_proxy_port} -> SSH隧道 -> 本地代理 127.0.0.1:{local_proxy_port}\n"
        f"  使用方式：exec_with_proxy 执行命令，或手动设置 http_proxy=http://127.0.0.1:{remote_proxy_port}"
    )


@mcp.tool()
@_safe_tool
def teardown_proxy(identifier: str) -> str:
    """关闭反向代理隧道，远程服务器不再通过本地代理上网。停止后 exec_with_proxy 将不可用。

    Args:
        identifier: 主机别名或 IP 地址
    """
    alias = _get_alias(identifier)

    info = proxy_tunnels.pop(alias, None)
    if info is None:
        return f"主机 '{alias}' 没有活动的代理隧道。"

    info["stop_event"].set()
    try:
        info["transport"].cancel_port_forward("127.0.0.1", info["remote_proxy_port"])
    except Exception:
        pass

    return f"代理隧道已关闭：{alias} (远程端口 {info['remote_proxy_port']})"


@mcp.tool()
@_safe_tool
def exec_with_proxy(identifier: str, command: str, timeout: int = 60) -> str:
    """通过本地代理在远程主机上执行命令。自动在命令前注入 http_proxy/https_proxy 环境变量，使 curl、wget、pip 等工具走代理。需先 setup_proxy 建立隧道。

    Args:
        identifier: 主机别名或 IP 地址
        command: 要执行的命令
        timeout: 超时时间（秒），默认 60
    """
    alias = _get_alias(identifier)
    client = _ensure_alive(alias)
    if client is None:
        return f"主机 '{identifier}' 未连接或已断开。"

    info = proxy_tunnels.get(alias)
    if info is None:
        return "代理隧道未建立。请先 setup_proxy。"

    rp = info["remote_proxy_port"]
    proxy_url = f"http://127.0.0.1:{rp}"
    proxied_cmd = (
        f"export http_proxy={proxy_url} https_proxy={proxy_url} HTTP_PROXY={proxy_url} HTTPS_PROXY={proxy_url} && {command}"
    )

    return _exec_on(client, proxied_cmd, timeout)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
