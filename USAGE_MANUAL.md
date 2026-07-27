# 啵啵贝 Pro 远程中继系统 · 使用手册

---

## 一、系统概览

```
你的浏览器 ──WSS──► VPS 中继服务器 ──WSS──► 对方手机 App ──BLE──► 啵啵贝 Pro
  (控制面板)         (FastAPI)            (前台服务)            (设备)
```

| 角色 | 设备 | 功能 |
|------|------|------|
| **Controller** | 你的电脑/手机浏览器 | 通过网页滑块和预设远程控制设备 |
| **Server** | VPS（Ubuntu） | WebSocket 中继 + 鉴权 + 心跳 |
| **Device** | 对方安卓/鸿蒙手机 | BLE 连接设备 + 接收远程指令 |

---

## 二、服务器部署

### 2.1 环境要求

- Ubuntu 20.04+ / Debian 11+
- Python 3.10+
- Caddy（反向代理 + TLS）
- 域名 + Cloudflare DNS

### 2.2 上传代码

```bash
sudo mkdir -p /opt/funf-relay
sudo cp -r server/ /opt/funf-relay/
sudo cp -r web/ /opt/funf-relay/
sudo cp -r deploy/ /opt/funf-relay/
```

### 2.3 安装依赖

```bash
sudo apt update
sudo apt install -y python3 python3-pip
sudo pip3 install --break-system-packages -r /opt/funf-relay/server/requirements.txt
```

### 2.4 创建运行账号和管理员凭据

```bash
sudo useradd --system --home /var/lib/funf-relay --shell /usr/sbin/nologin funf-relay
sudo install -d -o funf-relay -g funf-relay -m 0750 /var/lib/funf-relay

# 交互输入管理员密码并输出哈希；不要把明文密码写入 shell 历史
python3 - <<'PY'
import hashlib, secrets
from getpass import getpass
p = getpass("管理员密码: ").encode()
s = secrets.token_bytes(16)
i = 310000
d = hashlib.pbkdf2_hmac("sha256", p, s, i)
print(f"pbkdf2_sha256${i}${s.hex()}${d.hex()}")
PY

# 生成凭据库加密密钥
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

将 `server/.env.example` 复制为 `/etc/funf-relay.env`，填入上面两项输出，并设置：

```bash
sudo chown root:funf-relay /etc/funf-relay.env
sudo chmod 0640 /etc/funf-relay.env
```

### 2.5 安装 systemd 服务

使用仓库内已限制权限和写目录的服务模板：

```bash
sudo cp /opt/funf-relay/deploy/funf-relay.service.example /etc/systemd/system/funf-relay.service
sudo systemctl daemon-reload
sudo systemctl enable --now funf-relay
sudo systemctl status funf-relay
```

### 2.6 配置 Caddy 反向代理 + TLS

安装 Caddy 并配置 `/etc/caddy/Caddyfile`：

```
{
    servers {
        protocols h1
    }
}

your-domain.com {
    tls /etc/ssl/funf/funf_origin.crt /etc/ssl/funf/funf_origin.key

    reverse_proxy 127.0.0.1:3870 {
        header_up Host {host}
        header_up Connection "upgrade"
        flush_interval -1
    }
}
```

```bash
sudo systemctl reload caddy
```

### 2.7 更换凭据

普通用户可在页面中修改自己的密码；管理员可以重置普通用户密码。设备密钥在设备详情中点“重置密钥”即可轮换，旧密钥会立即失效。修改管理员初始密码哈希或 Fernet 密钥属于维护操作，修改 `/etc/funf-relay.env` 后重启服务：

```bash
sudo nano /etc/funf-relay.env
sudo systemctl restart funf-relay
```

已有数据库后不要随意更换 `FUNF_VAULT_KEY`，否则历史凭据将无法解密。

---

## 三、Web 控制面板

### 3.1 访问

浏览器打开 `https://你的域名/`

### 3.2 登录和绑定设备

1. 管理员登录后在“用户”页创建普通用户
2. 普通用户使用临时密码登录并按提示修改密码
3. 用户在“设备”页生成自己的设备密钥，将密钥填入 Android App
4. 选择该用户的设备后，状态栏应显示：
   - 🟢 你↔服务器（WebSocket 已连）
   - 🟢 对方App（手机在线）
   - 🟢 对方BLE↔设备（设备就绪）

### 3.3 控制

| 操作 | 说明 |
|------|------|
| **拖动滑块** | 实时调节吮吸/震动/电流/入体强度 (0-100) |
| **M1-M4 按钮** | 切换每路的工作模式 |
| **预设按钮** | 一键切换：温柔/日常/激烈/全停 |
| **急停** | 全部归零 |

### 3.4 状态指示

| 指示灯 | 颜色 | 含义 |
|--------|------|------|
| 你↔服务器 | 🟢绿 | WebSocket 已连接 |
| 你↔服务器 | 🔴红 | 连接断开，3秒自动重连 |
| 对方App | 🟢绿 | 对方手机在线 |
| 对方BLE↔设备 | 🟢绿 | 设备 BLE 已连接 |
| 对方BLE↔设备 | 🟡黄 | BLE 连接中 |
| 对方BLE↔设备 | 🔴红 | BLE 未连接 |
| 控制锁 | 🟢绿 | 你持有控制锁 |

---

## 四、Android App（对方手机）⭐ 主力

### 4.1 编译安装

**方式一：命令行编译**
```bash
cd android/FunfRelayApp
./gradlew assembleDebug
# APK 产出：app/build/outputs/apk/debug/app-debug.apk
```

**方式二：Android Studio**
1. 用 Android Studio 打开 `android/FunfRelayApp/` 文件夹
2. 等待 Gradle 同步完成
3. Build → Build Bundle(s) / APK(s) → Build APK(s)

**安装：** 将 APK 传到对方手机，直接安装。首次安装需允许「未知来源」应用。

### 4.2 界面说明

| 区域 | 说明 |
|------|------|
| **状态栏** | 显示服务器连接状态（在线/断开/重连中） |
| **BLE + 电池** | BLE 连接状态 + 设备电池电量（支持的话） |
| **强度显示** | 实时显示当前四路强度：吮X 震X 电X 入X |
| **服务器配置** | 服务器地址 + 当前用户的设备密钥输入框 |
| **设备列表** | 扫描到的 BLE 设备，🎯 标记目标设备 |
| **日志区** | 实时操作日志，排查问题用 |

### 4.3 操作步骤

1. **填入配置**
   - 服务器地址：`wss://你的域名/ws`
   - 密钥：在当前用户 Web 控制台中生成的设备密钥

2. **启动服务** → 点「▶ 启动中继」
   - 自动连接 WebSocket 到服务器
   - 自动开始 BLE 扫描

3. **连接设备** → 点「扫描」
   - 列表显示附近所有 BLE 设备
   - 🎯 标记的是匹配的目标设备（`SOSEXY` / `SOSEXY Pro` / `Bubblebei`）
   - 点目标设备的「连接」按钮

4. **等待就绪** → 显示「✅ 设备就绪」
   - App 自动完成：服务发现 → 订阅 NOTIFY → 发初始化帧

5. **远程控制** → 现在对方可以通过 Web UI 远程控制了

### 4.4 后台保活（必读！）

Android 系统会自动杀后台进程。**必须按品牌做以下设置**，否则切后台 BLE 就断：

| 品牌 | 设置路径 |
|------|---------|
| **华为 / 荣耀** | 设置 → 应用 → 应用启动管理 → 找到「啵啵贝中继」→ 关掉「自动管理」→ 弹窗三个开关**全部打开**（自启动 + 关联启动 + 后台活动） |
| **小米 / 红米** | 设置 → 应用设置 → 应用管理 → 啵啵贝中继 → 省电策略 → 选「**无限制**」。再回到应用信息页 → 自启动 → 打开 |
| **OPPO / 一加** | 设置 → 电池 → 更多 → 高耗电应用 → 打开。设置 → 应用 → 自启动 → 打开 |
| **vivo** | 设置 → 电池 → 后台高耗电 → 允许。手机管家 → 权限管理 → 自启动 → 打开 |
| **三星** | 设置 → 应用程序 → 啵啵贝中继 → 电池 → 选「**不受限制**」 |
| **通用** | App 内点「🔋 电池优化」按钮 → 选择「**不允许**」 |

> ⚠️ **华为/荣耀 特别注意**：还要在最近任务列表里把 App 卡片**下拉锁住**（出现小锁图标），防止一键清理杀掉。

### 4.5 协议探测

如果某些功能（震动、电流）不生效：

1. 确保设备已连接（显示「设备就绪」）
2. 点「🔬 协议探测」
3. App 自动发送 8 组测试帧，每组 50% 强度，间隔 5 秒：
   - 测试1 (01/02)：旧震动
   - 测试2 (03/04)：旧电流
   - 测试3 (05/06)：候选A
   - 测试4 (07/08)：旧吮吸（已知可用）
   - 测试5 (09/0A)：候选B
   - 测试6 (0B/0C)：候选C
   - 测试7 (0D/0E)：候选D
   - 测试8 (0F/10)：候选E
4. **记录每组体感**（无感/震动/电流/吮吸）
5. 把结果反馈，更新协议配置

### 4.6 更新 App

1. 卸载旧版
2. 安装新版 APK
3. 服务器地址和设备密钥会保留（SharedPreferences）

### 4.7 协议更新流程

抓到 Pro 新协议后：

1. 编辑 `server/protocol_config.json` → 更新对应功能的 `mode1`/`mode2`
2. 编辑 `RelayService.kt` → 更新 `fnModes` Map
3. 重新编译 APK
4. VPS 上重启服务：`systemctl restart funf-relay`

---

## 五、HarmonyOS NEXT App（纯血鸿蒙备选）

> 仅当对方手机是 **HarmonyOS NEXT（5.x / 纯血鸿蒙）** 时使用。普通鸿蒙 4.x 及之前直接用 Android APK。

### 5.1 构建

1. 安装 DevEco Studio（华为官网下载）
2. 打开 `harmony/HarmonyRelayApp`
3. Build → Build Hap(s)
4. 产物：`entry/build/default/outputs/default/entry-default-unsigned.hap`

### 5.2 部署

DevEco Studio 通过 USB 连接手机，一键部署调试。

### 5.3 使用

操作流程与 Android 版相同。注意鸿蒙 NEXT 的权限授权方式略有不同，按屏幕提示操作即可。

---

## 六、协议配置

### 6.1 协议文件

`server/protocol_config.json` 是协议真理源，修改后重启服务即可生效。

### 6.2 通道定义

```json
"functions": {
    "suction":   {"mode1": "0x07", "mode2": "0x08"},
    "vibration": {"mode1": "0x01", "mode2": "0x02"},
    "electric":  {"mode1": "0x03", "mode2": "0x04"},
    "insertion": {"mode1": null,   "mode2": null}
}
```

- `null` = 该功能暂不可用（待抓包）
- `0x01`-`0xFF` = 功能对应的 BLE 模式字节

### 6.3 添加新功能

抓到 Pro 新协议的通道字节后：

1. 编辑 `server/protocol_config.json`
2. 填入正确的 `mode1`/`mode2`
3. 编辑 Android `RelayService.kt` 的 `fnModes`
4. 编辑 HarmonyOS `ProtocolBuilder.ets` 的 `fnModes`
5. 重启服务：`systemctl restart funf-relay`
6. 重新编译 App

---

## 七、BLE 抓包指南

### 7.1 Android HCI 日志

1. 设置 → 开发者选项 → 打开「蓝牙 HCI 信息收集日志」
2. **关闭蓝牙再打开**（必须！否则不记录）
3. 用官方 App 操作设备（各功能依次操作，间隔 3 秒）
4. 设置 → 开发者选项 → 提交错误报告
5. 解压 bugreport.zip，找到 `FS/data/misc/bluetooth/logs/btsnoop_hci.log`

### 7.2 分析

用 Wireshark 打开 `.log` 文件，过滤 ATT Write Request（opcode 0x12），对比不同功能的 12 字节 payload，找出 mode1/mode2 位置。

### 7.3 nRF Connect（快速验证）

1. Google Play 搜索 nRF Connect 安装
2. 连接 SOSEXY 设备
3. 找到 `0000EE03-...` Write Characteristic
4. 手动发送测试帧，验证各通道字节

---

## 八、故障排查

### 8.1 Web UI 频繁断连

- 检查服务端日志：`journalctl -u funf-relay -f`
- 检查 Caddy 日志：`journalctl -u caddy -f`
- 刷新浏览器（Ctrl+Shift+R 强制刷新，清除缓存）

### 8.2 Android 后台断连

- 确保电池优化已关闭
- 检查应用启动管理设置（华为/小米特别重要）
- 更新到最新版 APK（含 `autoConnect=true` 修复）

### 8.3 某些功能不生效

- 用协议探测工具测试通道字节
- 检查 `protocol_config.json` 中对应功能的 `mode1`/`mode2` 是否为 `null`
- 查看 Android 日志确认帧是否成功写入

### 8.4 设备找不到

- 确认啵啵贝 Pro 已开机且在蓝牙范围内
- 确认蓝牙已开启
- 检查 Pro 的广播名称是否变化（可能不是 "SOSEXY"）
- 查看 App 日志中的实际扫描结果

---

## 九、常用命令速查

```bash
# 服务管理
systemctl restart funf-relay
systemctl status funf-relay
journalctl -u funf-relay -f --no-pager

# Caddy
systemctl restart caddy
systemctl status caddy
caddy validate --config /etc/caddy/Caddyfile

# 文件上传
scp -i "你的密钥.pem" 本地文件 root@你的VPS:/opt/funf-relay/

# 健康检查
curl https://你的域名/healthz

# WebSocket 测试
curl -sk -o /dev/null -w "%{http_code}" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  "https://你的域名/ws?role=controller&device=设备ID"
```

---

## 十、安全建议

1. **账号隔离**：不要共用账号；每位用户只使用自己的设备密钥和反馈记录
2. **密钥保密**：设备密钥应视为密码，泄露后立即在页面中重置
3. **HTTPS 强制**：始终通过 `wss://` 连接，不要在公网使用 `ws://`
4. **环境文件权限**：保持 `/etc/funf-relay.env` 为 `root:funf-relay 0640`
5. **防火墙**：VPS 上只开放 80/443 端口，3870 端口仅绑定 127.0.0.1
6. **备份**：同时备份 SQLite 数据库与 `backgrounds/`，恢复时保持所有权和权限
7. **系统更新**：定期 `apt update && apt upgrade`

---

_最后更新：2026-07-27_
