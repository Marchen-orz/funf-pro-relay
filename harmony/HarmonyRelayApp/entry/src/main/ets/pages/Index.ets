/**
 * 主页面 —— 啵啵贝 Pro 中继控制
 */
import { commonEventManager } from '@kit.BasicServicesKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { ServiceExtensionAbility, Want } from '@kit.AbilityKit';
import { wantAgent } from '@kit.AbilityKit';
import { notificationManager } from '@kit.NotificationKit';

const EVENT_LOG = "com.funf.relay.log";
const EVENT_STATUS = "com.funf.relay.status";
const EVENT_BLE_STATE = "com.funf.relay.ble_state";
const EVENT_DEVICE_STATE = "com.funf.relay.device_state";
const EVENT_BATTERY = "com.funf.relay.battery";
const EVENT_DEVICE_FOUND = "com.funf.relay.device_found";

interface BleDevice {
  name: string;
  address: string;
  rssi: number;
  isTarget: boolean;
}

@Entry
@Component
struct RelayPage {
  @State server: string = "wss://your-domain.com/ws";
  @State token: string = "";
  @State status: string = "未启动";
  @State bleState: string = "BLE: --";
  @State bleReady: boolean = false;
  @State battery: string = "--";
  @State batteryColor: string = "#E0E0E0";
  @State intensity: string = "吮0 震0 电0 入0";
  @State log: string = "";
  @State devices: BleDevice[] = [];
  @State serviceRunning: boolean = false;

  private subscriber: commonEventManager.CommonEventSubscriber | null = null;

  aboutToAppear(): void {
    this.subscribeEvents();
    // 读取保存的配置
    try {
      // 这里简化处理，实际可用 preferences 存储
    } catch (e) {}
  }

  aboutToDisappear(): void {
    if (this.subscriber) {
      commonEventManager.unsubscribe(this.subscriber);
    }
  }

  private async subscribeEvents(): Promise<void> {
    const info: commonEventManager.CommonEventSubscribeInfo = {
      events: [EVENT_LOG, EVENT_STATUS, EVENT_BLE_STATE,
               EVENT_DEVICE_STATE, EVENT_BATTERY, EVENT_DEVICE_FOUND]
    };
    this.subscriber = await commonEventManager.createSubscriber(info);
    commonEventManager.subscribe(this.subscriber, (_err, data) => {
      if (!data?.event || !data?.data) return;
      switch (data.event) {
        case EVENT_LOG:
          this.log = this.log + data.data + "\n";
          break;
        case EVENT_STATUS:
          this.status = data.data;
          break;
        case EVENT_BLE_STATE:
          const bs = JSON.parse(data.data) as Record<string,Object>;
          this.bleState = bs["state"] as string;
          this.bleReady = (bs["ready"] as boolean) ?? false;
          break;
        case EVENT_DEVICE_STATE:
          const st = JSON.parse(data.data) as Record<string,number>;
          this.intensity = `吮${st["suction"]??0} 震${st["vibration"]??0} 电${st["electric"]??0}`;
          break;
        case EVENT_BATTERY:
          const lvl = parseInt(data.data);
          this.battery = lvl + "%";
          this.batteryColor = lvl < 20 ? "#FFCDD2" : (lvl < 50 ? "#FFF9C4" : "#C8E6C9");
          break;
        case EVENT_DEVICE_FOUND:
          const dev = JSON.parse(data.data) as BleDevice;
          if (!this.devices.find(d => d.address === dev.address)) {
            this.devices = [...this.devices, dev];
          }
          break;
      }
    });
  }

  private sendAction(action: string, extras?: Record<string, string>): void {
    let context = getContext(this) as common.UIAbilityContext;
    let want: Want = {
      bundleName: "com.funf.relay",
      moduleName: "entry",
      abilityName: "RelayService",
      parameters: { action, ...extras }
    };
    context.startServiceExtensionAbility(want).catch((err: BusinessError) => {
      this.log = this.log + "启动服务失败: " + err.message + "\n";
    });
  }

  build() {
    Column() {
      // 标题
      Text("啵啵贝 Pro 中继")
        .fontSize(20).fontWeight(FontWeight.Bold).width('100%').textAlign(TextAlign.Center).padding(8)

      // 状态
      Text(this.status)
        .fontSize(13).width('100%').padding(8).backgroundColor('#E0E0E0').textAlign(TextAlign.Center).borderRadius(4)

      // BLE + 电池 + 扫描
      Row() {
        Text(this.bleState)
          .fontSize(11).layoutWeight(3).padding(6)
          .backgroundColor(this.bleReady ? '#C8E6C9' : '#FFF3E0')
          .textAlign(TextAlign.Center).borderRadius(4)
        Text("🔋" + this.battery)
          .fontSize(11).layoutWeight(1).padding(6)
          .backgroundColor(this.batteryColor as ResourceColor)
          .textAlign(TextAlign.Center).borderRadius(4).margin({ left: 4 })
        Button("扫描")
          .fontSize(11).height(32).margin({ left: 4 })
          .onClick(() => this.sendAction("scan_ble"))
      }.width('100%').margin({ bottom: 4 })

      // 强度显示
      Text(this.intensity)
        .fontSize(12).width('100%').padding(6).backgroundColor('#FFFFFF')
        .textAlign(TextAlign.Center).borderRadius(4).margin({ bottom: 6 })

      // 服务器配置
      Row() {
        Text("服务器").fontSize(12).layoutWeight(1)
        TextInput({ text: this.server, placeholder: "wss://relay.example.com/ws" })
          .fontSize(12).layoutWeight(3)
          .onChange(v => this.server = v)
      }.width('100%').margin({ bottom: 4 })

      Row() {
        Text("Token").fontSize(12).layoutWeight(1)
        TextInput({ text: this.token, placeholder: "设备 Token" })
          .fontSize(12).layoutWeight(3).type(InputType.Password)
          .onChange(v => this.token = v)
      }.width('100%').margin({ bottom: 8 })

      // 启动/停止
      Row() {
        Button("启动中继")
          .fontSize(13).layoutWeight(1)
          .onClick(() => {
            this.serviceRunning = true;
            this.devices = [];
            this.sendAction("start", { server: this.server, token: this.token });
          })
        Button("停止")
          .fontSize(13).layoutWeight(1).margin({ left: 6 })
          .onClick(() => {
            this.serviceRunning = false;
            this.sendAction("stop");
          })
      }.width('100%').margin({ bottom: 6 })

      // 设备列表
      Text("📱 附近设备").fontSize(13).fontWeight(FontWeight.Bold).padding(4)
      if (this.devices.length === 0) {
        Text("启动服务后点击扫描").fontSize(11).fontColor('#999').width('100%')
          .textAlign(TextAlign.Center).padding(12)
      }
      Scroll() {
        Column() {
          ForEach(this.devices, (dev: BleDevice) => {
            Row() {
              Text((dev.isTarget ? "🎯 " : "📱 ") + dev.name)
                .fontSize(12).layoutWeight(1).fontWeight(dev.isTarget ? FontWeight.Bold : FontWeight.Normal)
                .fontColor(dev.isTarget ? '#4CAF50' : '#333')
              Text(dev.rssi + " dBm").fontSize(10).margin({ left: 8, right: 8 })
              Button("连接")
                .fontSize(10).height(28)
                .onClick(() => this.sendAction("connect_device", { address: dev.address }))
            }.width('100%').padding(6).borderRadius(4)
            .backgroundColor(dev.isTarget ? '#E8F5E9' : '#FAFAFA').margin({ bottom: 2 })
          })
        }
      }.layoutWeight(1).backgroundColor('#FFFFFF').padding(4).margin({ bottom: 6 })

      // 底部按钮
      Row() {
        Button("🔬 探测")
          .fontSize(12).layoutWeight(1).backgroundColor('#FF9800')
          .onClick(() => this.sendAction("probe"))
        Button("🛑 急停")
          .fontSize(12).layoutWeight(1).backgroundColor('#E74C3C').margin({ left: 4 })
          .onClick(() => this.sendAction("emergency_stop"))
      }.width('100%').margin({ bottom: 6 })

      // 日志
      Scroll() {
        Text(this.log)
          .fontSize(10).fontFamily("monospace").width('100%').padding(6)
      }.layoutWeight(1).backgroundColor('#EEF2F8').borderRadius(4)
    }
    .width('100%').height('100%').padding(10).backgroundColor('#F5F5F5')
  }
}
