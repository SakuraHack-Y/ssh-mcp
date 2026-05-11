# SSH MCP Server

通过 MCP 协议管理和连接 SSH 主机。支持命令执行、文件传输、目录同步、远程备份、多主机批量操作、服务管理、SSH 隧道、远程文件编辑和反向代理。

## 项目结构

```
ssh/
├── example.yaml      # 主机配置模板
├── hosts.yaml        # 主机配置文件（需自行创建）
├── backups/          # 远程文件备份目录（自动创建）
├── ssh_mcp/
│   ├── __init__.py
│   └── server.py     # MCP 服务器
├── pyproject.toml
└── uv.lock
```

## 快速开始

### 1. 安装依赖

```bash
cd ssh
uv sync
```

### 2. 配置主机

复制模板并填入真实信息：

```bash
cp example.yaml hosts.yaml
```

`hosts.yaml` 格式：

```yaml
主机1:
  host: 192.168.1.100
  port: 22
  username: root
  password: "your_password"
  description: "生产服务器"
```

> `hosts.yaml` 包含密码等敏感信息，已被 `.gitignore` 屏蔽，不会提交到仓库。

### 3. 在 Claude Code 中配置

在项目的 `.claude/settings.json` 或全局 `~/.claude/settings.json` 中添加：

```json
{
  "mcpServers": {
    "ssh": {
      "command": "uv",
      "args": ["run", "--directory", "G:/vibecoding/ssh", "python", "-m", "ssh_mcp.server"]
    }
  }
}
```

### 4. 使用

配置完成后，在 Claude Code 对话中直接使用自然语言：

```
连接主机1，查看服务器状态
```

```
让主机1通过代理下载 https://example.com/file.tar.gz
```

## 工具清单

### 连接管理

| 工具 | 说明 |
|---|---|
| `list_hosts` | 列出所有已配置的主机及其连接状态 |
| `connect_host` | 通过别名或 IP 连接 SSH 主机 |
| `disconnect_host` | 断开指定主机的连接 |
| `get_host_status` | 获取系统状态（CPU/内存/磁盘/网络/负载） |

### 命令执行

| 工具 | 说明 |
|---|---|
| `exec_command` | 在远程主机执行任意 Shell 命令 |
| `exec_on_hosts` | 多台主机并行执行同一命令 |

### 多主机批量操作

| 工具 | 说明 |
|---|---|
| `batch_upload` | 将同一文件批量上传到多台主机 |

### 进程与服务管理

| 工具 | 说明 |
|---|---|
| `manage_service` | 管理服务（start/stop/restart/status/enable/disable） |
| `list_services` | 列出 systemd 服务，支持按状态过滤 |
| `list_processes` | 列出进程，支持按名称过滤 |
| `tail_log` | 读取远程日志文件最后 N 行 |

### 文件传输

| 工具 | 说明 |
|---|---|
| `upload_file` | 上传单个文件 |
| `download_file` | 下载单个文件，自动创建本地目录 |
| `upload_dir` | 递归上传整个目录 |
| `download_dir` | 递归下载整个目录 |
| `sync_file` | 比对 SHA256，仅在不一致时上传 |

### 文件检查、备份与编辑

| 工具 | 说明 |
|---|---|
| `remote_file_info` | 查看远程文件的大小、权限、修改时间、SHA256 |
| `backup_remote_file` | 将远程文件备份到本地 `backups/` 目录 |
| `restore_remote_file` | 从备份恢复到远程 |
| `read_remote_file` | 读取远程文件全部内容 |
| `edit_remote_file` | 写入内容到远程文件，默认自动备份 |

### 远程文件搜索

| 工具 | 说明 |
|---|---|
| `find_remote` | 按名称/类型/深度搜索远程文件 |

### SSH 隧道

| 工具 | 说明 |
|---|---|
| `create_tunnel` | 创建本地端口转发隧道 |
| `close_tunnel` | 关闭指定隧道 |
| `list_tunnels` | 列出所有活动的隧道 |

### 反向代理

| 工具 | 说明 |
|---|---|
| `setup_proxy` | 建立反向隧道，让远程服务器通过本地代理上网 |
| `exec_with_proxy` | 通过代理执行命令（自动注入 http_proxy 环境变量） |
| `teardown_proxy` | 关闭代理隧道 |

## 使用示例

### 连接与命令执行

```
连接主机1                          → connect_host("主机1")
在主机1上执行 df -h               → exec_command("主机1", "df -h")
断开主机1                         → disconnect_host("主机1")
```

### 多主机批量操作

```
在主机1和主机2上同时执行 uptime   → exec_on_hosts(["主机1", "主机2"], "uptime")
将 update.sh 分发到所有主机       → batch_upload(["主机1", "主机2"], "./update.sh", "/opt/update.sh")
```

### 服务管理

```
重启主机1的 nginx                 → manage_service("主机1", "nginx", "restart")
查看 sshd 状态                    → manage_service("主机1", "sshd", "status")
列出所有运行中的服务              → list_services("主机1")
查看 docker 相关进程              → list_processes("主机1", "docker")
查看 nginx 错误日志最后 50 行     → tail_log("主机1", "/var/log/nginx/error.log", 50)
```

### SSH 隧道

```
转发本地 3306 到远程的 MySQL      → create_tunnel("主机1", 3306)
转发本地 8080 到远程内网 10.0.0.5:80 → create_tunnel("主机1", 8080, "10.0.0.5", 80)
查看当前隧道                      → list_tunnels()
关闭 3306 隧道                    → close_tunnel("主机1", 3306)
```

### 反向代理

让远程服务器通过你本地的代理（如 Clash）访问外网：

```
建立代理隧道                      → setup_proxy("主机1", local_proxy_port=10808, remote_proxy_port=11080)
通过代理下载文件                  → exec_with_proxy("主机1", "wget https://example.com/file.tar.gz")
通过代理安装 pip 包               → exec_with_proxy("主机1", "pip install requests")
关闭代理                          → teardown_proxy("主机1")
```

流量路径：`远程服务器 → SSH隧道 → 本地代理(10808) → 目标网站`

### 文件操作

```
上传 app.py 到主机1 的 /opt/app/  → upload_file("主机1", "./app.py", "/opt/app/")
下载主机1的 /var/log/syslog       → download_file("主机1", "/var/log/syslog", "./logs/")
上传整个项目目录                  → upload_dir("主机1", "./dist", "/opt/app/dist")
```

### 远程文件搜索与编辑

```
搜索 /etc 下所有 .conf 文件       → find_remote("主机1", "/etc", name="*.conf", max_depth=2)
读取 nginx 配置                   → read_remote_file("主机1", "/etc/nginx/nginx.conf")
修改远程文件（自动备份原文件）    → edit_remote_file("主机1", "/tmp/test.txt", "new content")
```

### 智能同步

```
同步 config.yaml 到主机1          → sync_file("主机1", "./config.yaml", "/opt/app/config.yaml")
```

首次上传会正常传输，再次调用时如果文件内容未变化，会跳过传输。

### 备份与恢复

```
备份主机1的 nginx 配置            → backup_remote_file("主机1", "/etc/nginx/nginx.conf", tag="调整前")
修改后恢复                        → restore_remote_file("主机1", "/etc/nginx/nginx.conf", tag="调整前")
```

备份文件存储在 `backups/<主机别名>/` 下，文件名格式为 `<别名>_<文件名>_<tag>_<时间戳>`。
`edit_remote_file` 默认自动调用 `backup_remote_file`（可通过 `auto_backup=False` 关闭）。

### 查看远程文件信息

```
查看主机1的 /etc/hostname         → remote_file_info("主机1", "/etc/hostname")
```

返回示例：

```
=== 主机1:/etc/hostname ===
类型: 文件
大小: 15 字节 (0.0 KB)
权限: -rw-r--r--
修改时间: 2026-01-30 15:13:11
SHA256: 3788b2a17aa7daa77b892c28734db35db9bc854194315c998f271c5df2787dbd
```

## 注意事项

- **敏感信息**：`hosts.yaml` 包含密码，已被 `.gitignore` 屏蔽，使用前从 `example.yaml` 复制
- **认证方式**：密码认证（`hosts.yaml` 中配置）
- **连接保持**：进程内 `dict` 缓存，连接后可反复使用
- **自动重连**：当前不支持，断开后需手动 `connect_host`
- **主机识别**：支持别名和 IP 双向查找
- **sudo 支持**：服务管理工具自动使用 `sudo` 执行
- **代理端口**：远程端口默认 11080（避免与常见端口冲突），本地代理端口默认 10808

## 技术栈

- **MCP SDK** `mcp >= 1.27.1` — MCP 协议实现
- **Paramiko** `paramiko >= 5.0.0` — SSH2 客户端
- **PyYAML** `pyyaml >= 6.0.3` — 配置文件解析
- **Python** `>= 3.12`
