# 测试报告

本文面向项目使用者、学习者和贡献者，公开说明当前测试覆盖范围与已知缺口。测试通过表示对应的软件行为在所述环境中通过检查，不表示真实硬件、网络和所有 Android 厂商系统均已获得认证。

## 报告信息

| 项目 | 内容 |
|---|---|
| 首次公开日期 | 2026-07-28 |
| 自动测试平台 | GitHub Actions: Ubuntu latest、Python 3.12、Node.js 22、Java 17 |
| 本地复核平台 | Windows、Python、Node.js、Java 17 / Android Gradle Plugin 8.2 |
| 自动测试入口 | `.github/workflows/tests.yml` |
| 执行时机 | 推送到 `main`、Pull Request、手动触发 |

最新公开结果请查看仓库首页的 `Tests` 徽章，或进入 GitHub 仓库的 **Actions > Tests**。每次提交的结果应以该提交对应的工作流为准。

## 自动测试范围

| 测试项 | 主要覆盖 | 通过标准 |
|---|---|---|
| Python 与 JSON 静态检查 | 服务端模块可编译、协议配置是有效 JSON | 命令退出码为 0 |
| 多租户集成测试 | 管理员登录、用户创建与改密、账号隔离、设备归属、设备密钥临时授权、越权拒绝、WebSocket 设备隔离、匿名/用户反馈隔离、管理员处理反馈、吊销与删除设备 | 输出 `MULTITENANT_INTEGRATION_OK` |
| Pro 通道兼容测试 | 仅保留吮吸、震动、微电流三通道；旧客户端 `insertion` 字段兼容映射到震动 | 相关断言全部通过 |
| HTTP 安全响应头 | `X-Frame-Options`、`Referrer-Policy` 和 CSP 防嵌入策略 | 相关断言全部通过 |
| Web JavaScript 检查 | `web/index.html` 内嵌脚本可被 JavaScript 引擎解析 | 输出 `FRONTEND_JS_OK` |
| Web 响应式视觉检查 | 未登录问题反馈入口在 1440x900 和 390x844 视口可见、弹窗不溢出页面 | 输出 `WEB_VISUAL_OK`，截图作为 Actions artifact 保留 14 天 |
| Android Debug 构建 | Kotlin/Android 源码、资源、Manifest 和依赖能够完成编译打包 | Gradle `assembleDebug` 成功 |

## 本地复核结果

2026-07-28 发布报告前，对当前代码执行了与自动测试对应的本地复核。服务端集成测试、协议配置与语法检查、Web JavaScript 检查及 Android Debug 构建均通过。GitHub Actions 的结果在工作流推送后生成，以仓库徽章和对应提交页面为最终公开记录。

## 尚未自动验证

以下项目需要真实手机、真实啵啵贝 Pro、特定厂商系统或故障注入环境，当前不能由 GitHub 托管运行器证明：

- BLE 实机扫描、配对、写入和设备实际动作是否与指令完全一致。
- 504、VPS 宕机、断网或 WebSocket 心跳超时后，设备固件是否保持最后强度或自行停止。
- Android 锁屏、长时间后台、系统回收、强制停止及各厂商省电策略下的稳定性。
- HarmonyOS 工程的真机编译、签名、后台服务与 BLE 行为。
- 公网端到端延迟、长时间压力、并发容量及弱网抖动。
- 小程序切入后台后的连接维持；本项目不建议用小程序承担 BLE 中继。

尤其需要注意：当前心跳用于发现连接失效和触发重连，不等同于硬件级自动归零保护。真实设备在断链后的行为，在完成实机故障注入测试前应视为未验证。

## 本地运行方法

服务端集成测试：

```bash
python -m pip install -r server/requirements-dev.txt
python -m json.tool server/protocol_config.json
python -m py_compile server/relay_server.py server/protocol.py server/test_multitenant.py
python server/test_multitenant.py
```

Web 脚本与视觉测试：

```bash
node -e "const fs=require('fs');const h=fs.readFileSync('web/index.html','utf8');new Function(h.slice(h.indexOf('<script>')+8,h.lastIndexOf('</script>')));console.log('FRONTEND_JS_OK')"
python -m playwright install chromium
python -m http.server 43871 --directory web
# 在另一个终端运行
python web/visual_test.py
```

Android 构建：

```bash
cd android/FunfRelayApp
./gradlew assembleDebug
```

Windows 下将最后一条命令改为 `gradlew.bat assembleDebug`。测试只使用临时数据库和运行时生成的随机凭据，不需要提交生产密钥、数据库或 `.env` 文件。

## 结果解释

- 绿色 `Tests` 徽章：该提交的自动检查与 Android 构建全部通过。
- 红色 `Tests` 徽章：至少一项失败，应进入 Actions 查看失败步骤和日志。
- 无结果或灰色徽章：工作流尚未运行、被取消，或 GitHub Actions 未启用。
- 自动测试通过不代表真实设备安全测试通过；部署和实机使用前仍应完成断网、急停和后台运行测试。

