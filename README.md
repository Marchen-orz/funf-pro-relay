# FUNF 啵啵贝 Pro · 远程中继系统

异地情侣远程控制啵啵贝 Pro（蓝牙名 `SOSEXY`）的开源方案。

**非官方项目**，基于社区逆向协议，与 FUNF 官方无关联。

---

## 🎯 解决什么问题

官方 App 切到后台就掉 BLE 连接，远程/异地场景几乎不可用。

本方案用**安卓前台服务 + WebSocket 长连接**替代官方 App：
- 前台服务 + WakeLock + 电池优化白名单 → 后台不掉连接
- WebSocket 长连接 + 双向心跳 → 200ms 指令下发
- `autoConnect=true` BLE 保活 → 设备走远了自己找回
- 协议适配层 → 抓到新协议改两行 JSON 就能跑

---

## 🏗️ 架构

```
[多用户浏览器] ──WSS──► [VPS FastAPI + SQLite] ──WSS──► [各用户安卓手机] ──BLE──► 各自设备
 账号/设备/反馈隔离          会话+设备归属+审计              每台设备独立密钥
```

| 组件 | 技术栈 | 说明 |
|------|--------|------|
| **中继服务器** | Python FastAPI + WebSocket + SQLite | 多用户、设备归属、鉴权、心跳、控制锁、审计 |
| **Web 控制面板** | 原生 HTML/JS | 用户登录、设备管理、控制与隔离的问题反馈 |
| **Android App** | Kotlin + Java-WebSocket | **主力**：前台服务、BLE 扫描/连接/GATT |
| **HarmonyOS App** | ArkTS | 纯血鸿蒙备选（`harmony/` 目录） |

---

## 🚀 快速开始

### 1. 部署服务器

```bash
# VPS 上
sudo mkdir -p /opt/funf-relay
sudo cp -r server/ web/ deploy/ /opt/funf-relay/
cd /opt/funf-relay

# 安装依赖
sudo pip3 install --break-system-packages -r server/requirements.txt

# 创建运行用户与状态目录
sudo useradd --system --home /var/lib/funf-relay --shell /usr/sbin/nologin funf-relay
sudo install -d -o funf-relay -g funf-relay -m 0750 /var/lib/funf-relay

# 生成管理员密码哈希与 Fernet 密钥的方法见 USAGE_MANUAL.md
sudo cp server/.env.example /etc/funf-relay.env
sudo chown root:funf-relay /etc/funf-relay.env
sudo chmod 0640 /etc/funf-relay.env
sudo editor /etc/funf-relay.env

# 配置 systemd（见 deploy/funf-relay.service.example）
sudo cp deploy/funf-relay.service.example /etc/systemd/system/funf-relay.service
sudo systemctl daemon-reload
sudo systemctl enable --now funf-relay

# 配置 Caddy 反向代理（见 deploy/Caddyfile.example）
# 健康检查
curl https://你的域名/healthz
```

### 2. 打开控制面板

浏览器访问 `https://你的域名/`，使用管理员账号登录。管理员创建普通用户，用户首次登录修改临时密码后，再为自己的 App 生成设备密钥。

### 3. 对方安卓手机装 App

#### 编译

```bash
cd android/FunfRelayApp
./gradlew assembleDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
```

或用 **Android Studio** 打开 `android/FunfRelayApp/` 文件夹 → Build → Build APK。

#### 安装与设置

1. 把 APK 传到对方手机，安装
2. 打开「啵啵贝中继」App
3. 填入服务器地址：`wss://你的域名/ws`
4. 填入该用户在 Web 控制台生成的 **设备密钥**
5. 点 **「▶ 启动中继」**，授权所有权限（蓝牙、通知、位置）
6. 点 **「扫描」** 搜索附近 BLE 设备
7. 找到 🎯 目标设备，点 **「连接」**
8. 等待显示 **「✅ 设备就绪」**

#### 后台保活设置（重要！）

切到后台就断？按品牌设置：

| 品牌 | 操作 |
|------|------|
| **华为/荣耀** | 设置 → 应用 → 应用启动管理 → 关掉「自动管理」→ 三个开关全开 |
| **小米/红米** | 设置 → 应用 → 啵啵贝中继 → 省电策略 → 无限制 |
| **OPPO/一加** | 设置 → 电池 → 高耗电应用 → 允许 |
| **vivo** | 设置 → 电池 → 后台高耗电 → 允许 |
| **三星** | 设置 → 应用程序 → 啵啵贝中继 → 电池 → 不受限制 |
| **通用** | App 内点「🔋 电池优化」→ 选「不允许」 |

---

## 📁 文件结构

```
funf-pro-relay/
├── server/
│   ├── relay_server.py        # FastAPI + WebSocket 中继服务
│   ├── protocol.py            # BLE 帧构建器（读 config）
│   ├── protocol_config.json   # ← 协议真理源，抓包后改这个
│   ├── requirements.txt
│   └── .env.example
├── web/
│   └── index.html             # Web 控制面板
├── android/FunfRelayApp/      # ★ 安卓 App（主力）
│   └── app/src/main/java/com/funf/relay/
│       ├── MainActivity.kt    #   主界面 + 设备列表
│       └── RelayService.kt    #   前台服务 + WS + BLE
├── harmony/HarmonyRelayApp/   # HarmonyOS NEXT（纯血鸿蒙备选）
├── deploy/
│   ├── Caddyfile.example
│   └── funf-relay.service.example
├── USAGE_MANUAL.md            # 详细使用手册
└── README.md
```

---

## 🔐 安全

- 账号会话与设备密钥分离；普通用户只能看到并控制自己的设备
- 管理员可创建/停用用户、重置密码并查看审计记录
- 普通用户的账号、设备和反馈记录相互隔离，管理接口必须登录后访问
- 密码使用 PBKDF2-SHA256，凭据库内容使用 Fernet 加密
- 控制锁：防止双方同时操作冲突，60s 无指令自动释放
- 支持 WSS（WebSocket over TLS）
- 推荐通过 Cloudflare 代理隐藏源站 IP
- Android 使用系统证书链校验 TLS；当前服务优先通过 `Authorization` 传递设备密钥
- 环境文件、数据库、日志、证书、签名密钥和构建产物不会纳入版本库

完整威胁边界、部署要求和漏洞报告方式见 **[SECURITY.md](SECURITY.md)**。管理员和服务器运营者属于可信边界，部署前必须理解凭据库的权限范围。

### 本地验证

```bash
python -m pip install -r server/requirements-dev.txt
python server/test_multitenant.py
cd android/FunfRelayApp && ./gradlew assembleDebug
```

浏览器视觉测试还需要先执行 `playwright install chromium`，并通过 `FUNF_VISUAL_TEST_URL` 指向测试实例后运行 `python web/visual_test.py`。

---

## 🔧 协议适配

啵啵贝 Pro 的 BLE 协议通过 `server/protocol_config.json` 配置：

```json
{
  "functions": {
    "suction":   {"mode1": "0x07", "mode2": "0x08"},
    "vibration": {"mode1": "0x01", "mode2": "0x02"},
    "electric":  {"mode1": "0x03", "mode2": "0x04"}
  }
}
```

抓包发现新通道后，改 JSON 重启服务即可。Android App 同步更新 `RelayService.kt` 中的 `fnModes`。

---

## ⚠️ 已知限制

- `get_status` 读的是服务端缓存状态，非设备实时遥测
- 协议无加密，BLE 范围内任何设备均可连接
- 每个设备通道仍限制为一个 controller 和一个 device；服务可同时承载多个相互隔离的设备通道
- Pro 没有独立“入体”模块；产品所称入体与震动为同一功能，系统统一使用震动通道

---

## 📄 License

MIT — 见 [LICENSE](LICENSE)。

本软件对硬件厂商无关联，使用者风险自负。请遵守当地法律法规。

---

## 📖 详细文档

- 用户手册入口：**[USER_GUIDE.md](USER_GUIDE.md)**
- 普通网页用户：**[WEB_USER_GUIDE.md](WEB_USER_GUIDE.md)**
- 设备持有者（Android App）：**[DEVICE_HOLDER_GUIDE.md](DEVICE_HOLDER_GUIDE.md)**
- 临时共享控制者：**[SHARED_CONTROLLER_GUIDE.md](SHARED_CONTROLLER_GUIDE.md)**
- 管理员：**[ADMIN_GUIDE.md](ADMIN_GUIDE.md)**
- 服务器部署、协议配置、BLE 抓包和开发维护，见 **[USAGE_MANUAL.md](USAGE_MANUAL.md)**。

感谢 Linux.do 社区。

给我小猫做的。
