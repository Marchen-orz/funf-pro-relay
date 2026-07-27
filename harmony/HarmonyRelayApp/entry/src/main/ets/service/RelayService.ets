/**
 * RelayService — 后台中继服务
 * 负责：WebSocket 长连接 + BLE 网关 + 心跳保活
 */
import { ServiceExtensionAbility, Want } from '@kit.AbilityKit';
import { webSocket } from '@kit.NetworkKit';
import { notificationManager } from '@kit.NotificationKit';
import { wantAgent } from '@kit.AbilityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { BleManager } from '../ble/BleManager';
import { buildControlFrame, buildStopFrames, hexToBytes, buildInitFrame, getFnModes } from '../protocol/ProtocolBuilder';
import { commonEventManager } from '@kit.BasicServicesKit';

// 通过 CommonEvent 与主页面通信
const EVENT_LOG = "com.funf.relay.log";
const EVENT_STATUS = "com.funf.relay.status";
const EVENT_BLE_STATE = "com.funf.relay.ble_state";
const EVENT_DEVICE_STATE = "com.funf.relay.device_state";
const EVENT_BATTERY = "com.funf.relay.battery";
const EVENT_DEVICE_FOUND = "com.funf.relay.device_found";

export default class RelayService extends ServiceExtensionAbility {
  private ws: webSocket.WebSocket | null = null;
  private wsConnected = false;
  private lastPong = 0;
  private serverUrl = "wss://your-domain.com/ws";
  private token = "";
  private ble: BleManager = new BleManager();
  private heartbeatTimer: number | null = null;
  private wsReconnectTimer: number | null = null;
  private state = { suction: 0, vibration: 0, electric: 0 };

  onCreate(want: Want): void {
    console.info("[Relay] onCreate");
    // 恢复配置
    const prefs = want.parameters as Record<string, string>;
    if (prefs?.["server"]) this.serverUrl = prefs["server"];
    if (prefs?.["token"]) this.token = prefs["token"];

    this.initBleCallbacks();
  }

  onRequest(want: Want, startId: number): void {
    const action = want.parameters?.["action"] as string ?? "start";
    console.info("[Relay] onRequest: " + action);

    switch (action) {
      case "start":
        this.serverUrl = (want.parameters?.["server"] as string) ?? this.serverUrl;
        this.token = (want.parameters?.["token"] as string) ?? this.token;
        this.connectWebSocket();
        break;
      case "stop":
        this.stopAll();
        this.context.terminateSelf();
        break;
      case "emergency_stop":
        this.ble.emergencyStop();
        this.emitEvent(EVENT_LOG, "急停已触发");
        break;
      case "scan_ble":
        this.ble.startScan();
        break;
      case "connect_device":
        const addr = want.parameters?.["address"] as string;
        if (addr) this.ble.connect(addr);
        break;
      case "probe":
        this.ble.probe();
        break;
    }
  }

  onDestroy(): void {
    this.stopAll();
  }

  // ========== WebSocket ==========

  private connectWebSocket(): void {
    if (this.wsConnected) return;
    this.emitEvent(EVENT_LOG, "WS 正在连接 " + this.serverUrl);
    try {
      this.ws = webSocket.createWebSocket();
      const separator = this.serverUrl.includes("?") ? "&" : "?";
      const uri = this.serverUrl + separator + "role=device&token=" + encodeURIComponent(this.token);

      this.ws.on('open', () => {
        this.wsConnected = true;
        this.lastPong = Date.now();
        this.emitEvent(EVENT_LOG, "WS 已连接服务器");
        this.emitEvent(EVENT_STATUS, "服务器在线");
        this.startHeartbeat();
        this.ble.startScan();
      });

      this.ws.on('message', (_err: BusinessError, value: string | ArrayBuffer) => {
        this.lastPong = Date.now();
        if (typeof value === 'string') this.handleMessage(value);
      });

      this.ws.on('close', (_err: BusinessError, value: webSocket.CloseResult) => {
        this.wsConnected = false;
        this.emitEvent(EVENT_LOG, `WS 断开 (${value.code}) ${value.reason}`);
        this.emitEvent(EVENT_STATUS, "服务器断开");
        if (this.wsReconnectTimer === null) {
          this.wsReconnectTimer = setTimeout(() => {
            this.wsReconnectTimer = null;
            this.connectWebSocket();
          }, 5000);
        }
      });

      this.ws.on('error', (err: BusinessError) => {
        this.wsConnected = false;
        this.emitEvent(EVENT_LOG, "WS 错: " + err.message);
        if (this.wsReconnectTimer === null) {
          this.wsReconnectTimer = setTimeout(() => {
            this.wsReconnectTimer = null;
            this.connectWebSocket();
          }, 5000);
        }
      });

      this.ws.connect(uri, (_err: BusinessError, _value: boolean) => {});
    } catch (e) {
      this.emitEvent(EVENT_LOG, "WS 创建失败: " + (e as BusinessError).message);
      this.wsReconnectTimer = setTimeout(() => {
        this.wsReconnectTimer = null;
        this.connectWebSocket();
      }, 5000);
    }
  }

  private startHeartbeat(): void {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = setInterval(() => {
      if (Date.now() - this.lastPong > 40000) {
        this.emitEvent(EVENT_LOG, "心跳超时，重建 WS");
        try { this.ws?.close(); } catch (e) {}
        this.wsConnected = false;
        this.connectWebSocket();
        return;
      }
      try { this.ws?.send('{"type":"heartbeat"}'); } catch (e) {}
    }, 15000);
  }

  // ========== 服务端消息处理 ==========

  private handleMessage(raw: string): void {
    let m: Record<string, Object>;
    try { m = JSON.parse(raw) as Record<string, Object>; } catch (e) { return; }
    const type = m["type"] as string;

    switch (type) {
      case "hello": {
        const ds = m["device_state"] as Record<string, number>;
        if (ds) {
          this.emitEvent(EVENT_LOG, `远端: 吮${ds["suction"]??0} 震${ds["vibration"]??0} 电${ds["electric"]??0}`);
        }
        break;
      }
      case "ping": {
        try { this.ws?.send('{"type":"pong"}'); } catch (e) {}
        break;
      }
      case "command": {
        const frames = m["frames"] as string[];
        const state = m["state"] as Record<string, number>;
        if (!frames) return;
        // 更新本地状态并通知 UI
        if (state) {
          this.state.suction = state["suction"] ?? 0;
          this.state.vibration = state["vibration"] ?? 0;
          this.state.electric = state["electric"] ?? 0;
          this.emitEvent(EVENT_DEVICE_STATE, JSON.stringify(this.state));
        }
        let ok = 0;
        for (const hex of frames) {
          const bytes = hexToBytes(hex);
          if (this.ble.write(bytes)) ok++;
        }
        this.emitEvent(EVENT_LOG, `指令: 吮${this.state.suction} 震${this.state.vibration} 电${this.state.electric} · ${ok}/${frames.length}帧`);
        try {
          this.ws?.send(JSON.stringify({ type: "command_result", result: "ok", frames_ok: ok }));
        } catch (e) {}
        break;
      }
      case "stop": {
        const frames = m["frames"] as string[];
        if (frames) {
          for (const hex of frames) this.ble.write(hexToBytes(hex));
        }
        this.emitEvent(EVENT_LOG, "收到停止");
        try {
          this.ws?.send(JSON.stringify({ type: "command_result", result: "stopped" }));
        } catch (e) {}
        break;
      }
      case "lock_released": {
        this.emitEvent(EVENT_LOG, "远端释放控制锁");
        break;
      }
    }
  }

  // ========== BLE 回调 ==========

  private initBleCallbacks(): void {
    this.ble.onLog = (msg) => this.emitEvent(EVENT_LOG, msg);
    this.ble.onBleState = (state, ready) => {
      this.emitEvent(EVENT_BLE_STATE, JSON.stringify({ state, ready }));
      if (ready) this.emitEvent(EVENT_STATUS, "设备就绪 · 对方可控制");
    };
    this.ble.onDeviceFound = (dev) => {
      this.emitEvent(EVENT_DEVICE_FOUND, JSON.stringify(dev));
    };
    this.ble.onBatteryLevel = (level) => {
      this.emitEvent(EVENT_BATTERY, String(level));
    };
  }

  // ========== 工具 ==========

  private emitEvent(event: string, data: string): void {
    commonEventManager.publish(event, (_err: BusinessError) => {}, { data });
  }

  private stopAll(): void {
    if (this.heartbeatTimer) { clearInterval(this.heartbeatTimer); this.heartbeatTimer = null; }
    if (this.wsReconnectTimer) { clearTimeout(this.wsReconnectTimer); this.wsReconnectTimer = null; }
    try { this.ws?.close(); } catch (e) {}
    this.wsConnected = false;
    this.ble.disconnect();
  }
}
