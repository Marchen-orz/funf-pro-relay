/**
 * BLE 管理器 —— 扫描、连接、GATT 操作
 * HarmonyOS NEXT / API 12
 */
import { ble } from '@kit.ConnectivityKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { buildInitFrame, buildProbeFrame, buildControlFrame, probePairs } from '../protocol/ProtocolBuilder';

// GATT UUID（与旧版一致，Pro 抓包后只改这里）
const SVC_UUID    = "0000EE01-0000-1000-8000-00805F9B34FB";
const WRITE_UUID  = "0000EE03-0000-1000-8000-00805F9B34FB";
const NOTIFY_UUID = "0000EE02-0000-1000-8000-00805F9B34FB";
const CCCD_UUID   = "00002902-0000-1000-8000-00805F9B34FB";
const BATTERY_SVC_UUID  = "0000180F-0000-1000-8000-00805F9B34FB";
const BATTERY_CHAR_UUID = "00002A19-0000-1000-8000-00805F9B34FB";

const TARGET_NAMES = ["SOSEXY", "SOSEXY Pro", "Bubblebei"];

export interface BleDevice {
  name: string;
  address: string;
  rssi: number;
  isTarget: boolean;
}

export class BleManager {
  private gattClient: ble.GattClientDevice | null = null;
  private writeChar: ble.BLECharacteristic | null = null;
  private notifyChar: ble.BLECharacteristic | null = null;
  private batteryChar: ble.BLECharacteristic | null = null;
  private connected = false;
  private ready = false;
  private connecting = false;
  private scanning = false;

  // 写入队列
  private writeQueue: Uint8Array[] = [];
  private writeBusy = false;

  // 回调
  onLog: ((msg: string) => void) | null = null;
  onBleState: ((state: string, ready: boolean) => void) | null = null;
  onDeviceFound: ((device: BleDevice) => void) | null = null;
  onBatteryLevel: ((level: number) => void) | null = null;

  private log(msg: string) { this.onLog?.(msg); console.log("[BLE]", msg); }

  /** 获取设备名称（兼容鸿蒙 API）*/
  private getDeviceName(d: ble.ScanResult): string {
    // HarmonyOS NEXT: ScanResult 有 deviceName 字段
    return (d as Record<string,string>).deviceName || d.data || "未知";
  }

  /** 扫描 BLE 设备 */
  startScan(): void {
    if (this.scanning) return;
    this.scanning = true;
    this.log("开始扫描 BLE 设备...");
    this.onBleState?.("扫描中...", false);

    try {
      ble.on("BLEDeviceFind", (results: Array<ble.ScanResult>) => {
        if (!this.scanning) return;
        for (const r of results) {
          const name = this.getDeviceName(r);
          const addr = r.deviceId;
          const rssi = r.rssi;
          const isTarget = TARGET_NAMES.some(n => name.toLowerCase().includes(n.toLowerCase()));
          this.onDeviceFound?.({ name: name || "未知", address: addr, rssi, isTarget });
          // 自动连接匹配的目标设备
          if (isTarget && !this.connected && !this.connecting) {
            this.stopScan();
            this.connect(addr);
            return;
          }
        }
      });

      ble.startBLEScan([], { interval: 0, dutyMode: 0, matchMode: 0 });
    } catch (e) {
      this.log("扫描失败: " + (e as BusinessError).message);
      this.scanning = false;
    }
  }

  stopScan(): void {
    if (!this.scanning) return;
    this.scanning = false;
    try { ble.stopBLEScan(); } catch (e) {}
    ble.off("BLEDeviceFind");
  }

  /** 连接设备 */
  connect(deviceId: string): void {
    if (this.connecting || this.connected) return;
    this.connecting = true;
    this.log("连接: " + deviceId);
    this.onBleState?.("连接中...", false);

    try {
      this.gattClient = ble.createGattClientDevice(deviceId);
      this.gattClient.connect();
      this.gattClient.on("BLEConnectionStateChange", (data: ble.BLEConnectionChangedState) => {
        if (data.state === 2) { // CONNECTED
          this.connected = true;
          this.connecting = false;
          this.log("BLE 已连接 · 发现服务");
          this.onBleState?.("已连接 · 发现服务", false);
          this.gattClient?.getServices();
        } else { // DISCONNECTED
          this.connected = false;
          this.connecting = false;
          this.ready = false;
          this.log("BLE 断开 · 5s 重连");
          this.onBleState?.("BLE 断开", false);
          setTimeout(() => {
            if (!this.connected) this.startScan();
          }, 5000);
        }
      });

      this.gattClient.on("BLECharacteristicChange", (data: ble.BLECharacteristic) => {
        const v = data.characteristicValue;
        if (!v) return;
        // 电池电量
        if (data.characteristicUuid.toUpperCase() === BATTERY_CHAR_UUID && v.length > 0) {
          const level = v[0];
          this.log("电池: " + level + "%");
          this.onBatteryLevel?.(level);
          return;
        }
        // 协议 NOTIFY
        const hex = Array.from(v).map(b => b.toString(16).padStart(2, '0')).join(' ');
        this.log("← NOTIFY: " + hex);
      });

      this.gattClient.on("BLECharacteristicWrite", () => {
        this.onWriteComplete();
      });

      this.gattClient.on("BLECharacteristicRead", (data: ble.BLECharacteristic) => {
        if (data.characteristicUuid.toUpperCase() === BATTERY_CHAR_UUID) {
          const v = data.characteristicValue;
          if (v && v.length > 0) {
            this.log("电池: " + v[0] + "%");
            this.onBatteryLevel?.(v[0]);
          }
        }
      });

      this.gattClient.on("BLEServicesDiscovered", () => {
        this.log("服务已发现");
        const svc = this.gattClient?.getService(SVC_UUID);
        if (!svc) {
          // 列出实际服务帮助排查
          const services = this.gattClient?.getServices() ?? [];
          this.log("未找到目标服务，实际服务:");
          for (const s of services) {
            this.log("  " + s.serviceUuid);
          }
          this.onBleState?.("非目标设备", false);
          return;
        }
        this.writeChar = svc.getCharacteristic(WRITE_UUID);
        this.notifyChar = svc.getCharacteristic(NOTIFY_UUID);
        if (!this.writeChar || !this.notifyChar) {
          this.log("未找到写/通知特征");
          this.onBleState?.("特征不匹配", false);
          return;
        }

        // 订阅 NOTIFY
        if (this.notifyChar) {
          this.gattClient?.setNotifyCharacteristicChanged(this.notifyChar, true);
        }

        // 读电池
        const batterySvc = this.gattClient?.getService(BATTERY_SVC_UUID);
        if (batterySvc) {
          this.batteryChar = batterySvc.getCharacteristic(BATTERY_CHAR_UUID);
          if (this.batteryChar) {
            this.log("发现电池服务");
            this.gattClient?.readCharacteristicValue(this.batteryChar);
            this.gattClient?.setNotifyCharacteristicChanged(this.batteryChar, true);
          }
        } else {
          this.log("设备不支持标准电池服务");
        }

        // 发初始化帧
        setTimeout(() => {
          this.write(buildInitFrame());
          this.log("已发初始化帧 · 2s 后就绪");
          setTimeout(() => {
            this.ready = true;
            this.log("设备就绪");
            this.onBleState?.("设备就绪", true);
          }, 2000);
        }, 500);
      });

    } catch (e) {
      this.log("连接失败: " + (e as BusinessError).message);
      this.connecting = false;
    }
  }

  /** 写入帧（加入队列）*/
  write(frame: Uint8Array): boolean {
    if (!this.connected || !this.writeChar) return false;
    this.writeQueue.push(frame);
    if (!this.writeBusy) this.dequeueWrite();
    return true;
  }

  private dequeueWrite(): void {
    if (!this.connected || !this.writeChar || !this.gattClient) {
      this.writeQueue.length = 0;
      this.writeBusy = false;
      return;
    }
    const frame = this.writeQueue.shift();
    if (!frame) { this.writeBusy = false; return; }
    this.writeBusy = true;
    this.writeChar.characteristicValue = frame.buffer as ArrayBuffer;
    this.gattClient.writeCharacteristicValue(this.writeChar, ble.GattWriteType.WRITE);
    // 3 秒超时
    setTimeout(() => {
      if (this.writeBusy) { this.writeBusy = false; this.dequeueWrite(); }
    }, 3000);
  }

  private onWriteComplete(): void {
    this.writeBusy = false;
    setTimeout(() => this.dequeueWrite(), 60);
  }

  /** 协议探测 */
  probe(): void {
    if (!this.connected) { this.log("请先连接设备"); return; }
    this.log("=".repeat(20));
    this.log("开始协议探测");
    this.runProbeStep(0);
  }

  private runProbeStep(index: number): void {
    if (index >= probePairs.length) {
      this.log("探测完成！查看日志反馈");
      return;
    }
    const [m1, m2, label] = probePairs[index];
    const frame = buildProbeFrame(m1, m2, 50);
    const hex = Array.from(frame).map(b => b.toString(16).padStart(2, '0')).join(' ');
    this.log(`>>> 测试${index+1}: ${label} <<<`);
    this.log(`   ${hex}`);
    this.write(frame);
    // 2.5s 归零，5s 下一个
    setTimeout(() => this.write(buildProbeFrame(m1, m2, 0)), 2500);
    setTimeout(() => this.runProbeStep(index + 1), 5000);
  }

  isReady(): boolean { return this.ready; }
  isConnected(): boolean { return this.connected; }

  /** 急停 */
  emergencyStop(): void {
    for (const fn of ["suction","vibration","electric"]) {
      const f = buildControlFrame(fn, 0);
      if (f) this.write(f);
    }
  }

  disconnect(): void {
    this.stopScan();
    this.writeQueue.length = 0;
    this.writeBusy = false;
    this.ready = false;
    this.connected = false;
    this.connecting = false;
    try { this.gattClient?.disconnect(); } catch (e) {}
    try { this.gattClient?.close(); } catch (e) {}
    this.gattClient = null;
    this.writeChar = null;
    this.notifyChar = null;
    this.batteryChar = null;
  }
}
