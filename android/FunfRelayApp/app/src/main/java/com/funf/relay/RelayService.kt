package com.funf.relay

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.util.Log
import org.java_websocket.client.WebSocketClient
import org.java_websocket.handshake.ServerHandshake
import org.json.JSONObject
import java.net.URI
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean

/**
 * RelayService —— 中继服务 + BLE 网关
 *
 * 角色：device 端。负责：
 * 1. WebSocket 连到 VPS，心跳保活、收指令回状态
 * 2. BLE 扫描 → 显示附近设备 → 连接啵啵贝 → 订阅 NOTIFY → 发初始化包
 * 3. 收到服务端 command 帧 hex → 逐包 writeGATT
 * 4. 收到 stop → 各功能发强度=0 的帧
 * 5. BLE 断自动重连，WS 断自动重连
 * 6. 紧急停止：让出 lock + 强制清零
 */
class RelayService : Service() {

    companion object {
        const val CHAN_ID = "funf_relay_fg"
        const val NOTIF_ID = 9527

        // ---- UI 回调 ----
        var onStatus: ((String) -> Unit)? = null
        var onLog: ((String) -> Unit)? = null
        /** 发现 BLE 设备：(名称, MAC地址, RSSI, 是否匹配目标) */
        var onDeviceFound: ((String, String, Int, Boolean) -> Unit)? = null
        /** BLE 状态变化：(阶段描述, 是否就绪) */
        var onBleState: ((String, Boolean) -> Unit)? = null
        /** 电池电量：(百分比 0-100, null=不支持) */
        var onBatteryLevel: ((Int?) -> Unit)? = null
        /** 当前设备强度：(吮吸, 震动, 电流, 入体) */
        var onDeviceState: ((Int, Int, Int, Int) -> Unit)? = null
    }

    private val tag = "FunfRelay"
    private var running = AtomicBoolean(false)
    private lateinit var wakeLock: PowerManager.WakeLock
    private lateinit var prefs: SharedPreferences
    private val handler = Handler(Looper.getMainLooper())
    private var serverUrl = ""
    private var token = ""

    // ---- BLE 状态 ----
    private var btAdapter: BluetoothAdapter? = null
    private var gatt: BluetoothGatt? = null
    private var writeChar: BluetoothGattCharacteristic? = null
    private var notifyChar: BluetoothGattCharacteristic? = null
    private var bleConnecting = false
    private var bleConnected = false
    private var bleReady = false
    private var targetDeviceAddr: String? = null  // 用户选定要连的设备地址

    // ---- 服务 UUID（Pro 抓包确认后只改这里）----
    private val svcUUID   = UUID.fromString("0000ee01-0000-1000-8000-00805f9b34fb")
    private val writeUUID = UUID.fromString("0000ee03-0000-1000-8000-00805f9b34fb")
    private val notifyUUID = UUID.fromString("0000ee02-0000-1000-8000-00805f9b34fb")
    private val cccdUUID  = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    // 标准 BLE 电池服务
    private val batterySvcUUID = UUID.fromString("0000180f-0000-1000-8000-00805f9b34fb")
    private val batteryCharUUID = UUID.fromString("00002a19-0000-1000-8000-00805f9b34fb")
    private var batteryChar: BluetoothGattCharacteristic? = null
    // 目标广播名列表（Pro 抓包后追加 "SOSEXY Pro" 等）
    private val targetNames = listOf("SOSEXY", "SOSEXY Pro", "Bubblebei")

    // ---- 已发现的设备（去重，用于 UI 展示）----
    private val discoveredDevices = ConcurrentHashMap<String, BleDeviceInfo>()

    data class BleDeviceInfo(
        val name: String,
        val address: String,
        val rssi: Int,
        val isTarget: Boolean
    )

    // ---- 协议帧构建（与服务端 protocol.py 同构）----
    private fun rndSeq(): Int = (Math.random() * 256).toInt()
    private fun clamp(v: Int, lo: Int, hi: Int) = if (v < lo) lo else if (v > hi) hi else v

    private fun frameBytes(mode1: Int, intensity: Int, mode2: Int): ByteArray {
        return byteArrayOf(
            rndSeq().toByte(), 0x01.toByte(), 0x00.toByte(), 0x02.toByte(), 0x00.toByte(),
            mode1.toByte(), 0x11.toByte(), clamp(intensity, 0, 100).toByte(), 0x00.toByte(),
            mode2.toByte(), 0x11.toByte(), 0x01.toByte()
        )
    }

    // 功能 -> (mode1, mode2)  已探测：吮吸✓ 震动✓(Pro) 电流待确认
    private val fnModes: Map<String, Pair<Int, Int>> = mapOf(
        "suction" to (0x07 to 0x08),
        "vibration" to (0x01 to 0x02),
        "electric" to (0x03 to 0x04)
        // "insertion" to (0x?? to 0x??)  // 入体 待抓包
    )

    private fun initFrame(): ByteArray = byteArrayOf(
        rndSeq().toByte(), 0x01.toByte(), 0x00.toByte(), 0x01.toByte(), 0x00.toByte(),
        0xC8.toByte(), 0x11.toByte(), 0x01.toByte()
    )

    override fun onCreate() {
        super.onCreate()
        prefs = getSharedPreferences("funf", MODE_PRIVATE)
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Funf:relay")
        wakeLock.setReferenceCounted(false)
        val bm = getSystemService(BLUETOOTH_SERVICE) as BluetoothManager
        btAdapter = bm.adapter
        // 恢复上次连接的设备地址
        targetDeviceAddr = prefs.getString("last_ble_addr", null)
        if (targetDeviceAddr != null) {
            uiLog("已记忆设备: $targetDeviceAddr")
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.getStringExtra("action") ?: "start"
        when (action) {
            "stop" -> { stopAllJob(); stopSelf(); return START_NOT_STICKY }
            "emergency_stop" -> {
                handleEmergencyStop()
                return START_STICKY
            }
            "scan_ble" -> {
                // 手动触发 BLE 扫描
                discoveredDevices.clear()
                handler.post { startBleScan() }
                return START_STICKY
            }
            "probe" -> {
                // 协议探测：逐个测试模式字节对，找出 Pro 版各功能对应关系
                handler.post { startProtocolProbe() }
                return START_STICKY
            }
            "connect_device" -> {
                // 连接到指定地址的设备
                val addr = intent?.getStringExtra("address")
                if (addr != null) {
                    targetDeviceAddr = addr
                    prefs.edit().putString("last_ble_addr", addr).apply()
                    uiLog("手动连接: $addr")
                    disconnectBle()
                    handler.postDelayed({ tryConnectByAddress(addr) }, 500)
                }
                return START_STICKY
            }
            "start" -> {
                serverUrl = intent?.getStringExtra("server") ?: serverUrl
                token = intent?.getStringExtra("token") ?: token
                startForegroundNotif("中继启动中")
                if (running.compareAndSet(false, true)) {
                    if (!wakeLock.isHeld) wakeLock.acquire(60 * 60 * 1000L)
                    startWebSocketLoop()
                }
            }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        running.set(false)
        try { if (wakeLock.isHeld) wakeLock.release() } catch (e: Exception) {}
        disconnectBle()
        try { ws?.close() } catch (e: Exception) {}
        super.onDestroy()
    }

    // ================================================================
    //  前台通知
    // ================================================================
    private fun startForegroundNotif(text: String) {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHAN_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHAN_ID, "中继服务", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val n = Notification.Builder(this, CHAN_ID)
            .setContentTitle("啵啵贝中继运行中")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .setContentIntent(pi)
            .build()
        if (Build.VERSION.SDK_INT >= 30)
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        else
            startForeground(NOTIF_ID, n)
    }

    // ================================================================
    //  WebSocket 客户端
    // ================================================================
    private var ws: WebSocketClient? = null
    private var wsConnecting = false
    private var legacyQueryAuth = false
    private var lastPong = 0L

    private fun startWebSocketLoop() {
        if (wsConnecting) return
        wsConnecting = true
        uiLog("WS 正在连接 $serverUrl")
        try {
            val separator = if (serverUrl.contains("?")) "&" else "?"
            val authQuery = if (legacyQueryAuth) "&token=$token" else ""
            val uri = URI("$serverUrl${separator}role=device$authQuery")
            val client = object : WebSocketClient(uri) {
                override fun onOpen(handshake: ServerHandshake?) {
                    wsConnecting = false
                    lastPong = System.currentTimeMillis()
                    uiLog("WS 已连接服务器")
                    uiStatus("服务器在线 · 等待对方")
                    onBleState?.invoke("服务器已连", false)
                    handler.post { startBleIfNotConnected() }
                }
                override fun onMessage(message: String?) {
                    lastPong = System.currentTimeMillis()
                    handleServerMessage(message ?: "")
                }
                override fun onClose(code: Int, reason: String?, remote: Boolean) {
                    wsConnecting = false
                    if (code == 1008 && !legacyQueryAuth && running.get()) {
                        legacyQueryAuth = true
                        uiLog("服务器需要旧版鉴权，切换兼容模式")
                        handler.postDelayed({ startWebSocketLoop() }, 1000)
                        return
                    }
                    uiLog("WS 断开 ($code) $reason")
                    uiStatus("服务器断开 · 5s 重连")
                    onBleState?.invoke("服务器断开", false)
                    reportBleStatus()
                    if (running.get()) handler.postDelayed({ startWebSocketLoop() }, 5000)
                }
                override fun onError(e: Exception?) {
                    wsConnecting = false
                    uiLog("WS 错: ${e?.message}")
                    if (running.get()) handler.postDelayed({ startWebSocketLoop() }, 5000)
                }
            }
            client.addHeader("Authorization", "Bearer $token")
            client.setConnectionLostTimeout(30)
            client.connect()
            ws = client
            startHeartbeat()
        } catch (e: Exception) {
            wsConnecting = false
            uiLog("WS 创建失败: ${e.message}")
            if (running.get()) handler.postDelayed({ startWebSocketLoop() }, 5000)
        }
    }

    private var hbTask: Runnable? = null
    private fun startHeartbeat() {
        hbTask?.let { handler.removeCallbacks(it) }
        val r = object : Runnable {
            override fun run() {
                if (!running.get()) return
                try {
                    if (System.currentTimeMillis() - lastPong > 40_000) {
                        uiLog("≈ 心跳超时，重建 WS")
                        try { ws?.close() } catch (e: Exception) {}
                        startWebSocketLoop()
                        return
                    }
                    ws?.send(JSONObject().put("type", "heartbeat").toString())
                } catch (e: Exception) {}
                handler.postDelayed(this, 15_000)
            }
        }
        hbTask = r
        handler.postDelayed(r, 15_000)
    }

    // ================================================================
    //  收服务端消息
    // ================================================================
    private fun handleServerMessage(raw: String) {
        val m = try { JSONObject(raw) } catch (e: Exception) { return }
        when (m.optString("type")) {
            "hello" -> {
                val ds = m.optJSONObject("device_state")
                if (ds != null) {
                    uiLog("远端同步: 吮${ds.optInt("suction")} 震${ds.optInt("vibration")} 电${ds.optInt("electric")} 入${ds.optInt("insertion")}")
                }
                reportBleStatus()
            }
            "heartbeat_ack" -> { /* lastPong 已在 onMessage 更新 */ }
            "command" -> {
                val frames = m.optJSONArray("frames")
                if (frames == null) return
                val state = m.optJSONObject("state")
                val s = state?.optInt("suction", 0) ?: 0
                val v = state?.optInt("vibration", 0) ?: 0
                val e = state?.optInt("electric", 0) ?: 0
                val i = state?.optInt("insertion", 0) ?: 0
                onDeviceState?.invoke(s, v, e, i)
                var ok = 0
                for (idx in 0 until frames.length()) {
                    val hex = frames.getString(idx)
                    val bytes = hexToBytes(hex)
                    if (writeFrameToBle(bytes)) ok++
                }
                uiLog("指令: 吮$s 震$v 电$e 入$i · ${ok}/${frames.length()}帧")
                try {
                    ws?.send(JSONObject()
                        .put("type", "command_result")
                        .put("result", "ok")
                        .put("frames_ok", ok)
                        .toString())
                } catch (e: Exception) {}
            }
            "stop" -> {
                val frames = m.optJSONArray("frames")
                if (frames != null) {
                    for (i in 0 until frames.length()) {
                        writeFrameToBle(hexToBytes(frames.getString(i)))
                    }
                }
                uiLog("收到停止指令")
                try {
                    ws?.send(JSONObject().put("type", "command_result").put("result", "stopped").toString())
                } catch (e: Exception) {}
            }
            "lock_released" -> {
                uiLog("远端释放控制锁（by ${m.optString("by")}）")
            }
            "ping" -> {
                // 服务端保活 ping → 回复 pong
                try { ws?.send(JSONObject().put("type", "pong").toString()) } catch (e: Exception) {}
            }
        }
    }

    // ================================================================
    //  BLE 扫描
    // ================================================================
    private fun startBleIfNotConnected() {
        if (bleConnecting || bleConnected) return
        if (btAdapter == null || btAdapter?.isEnabled != true) {
            uiLog("⚠️ 蓝牙未开启，5s 后重试")
            onBleState?.invoke("蓝牙未开启", false)
            sendBleStatus(false, false, "蓝牙未开")
            try {
                val i = Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE)
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                startActivity(i)
            } catch (e: Exception) {}
            handler.postDelayed({ startBleIfNotConnected() }, 5000)
            return
        }
        // 如果有已记忆的目标设备地址，先尝试直连
        if (targetDeviceAddr != null && !bleConnecting) {
            uiLog("尝试连接已记忆设备: $targetDeviceAddr")
            onBleState?.invoke("连接已记忆设备...", false)
            tryConnectByAddress(targetDeviceAddr!!)
            return
        }
        startBleScan()
    }

    private fun tryConnectByAddress(address: String) {
        if (bleConnecting || bleConnected) return
        try {
            val device = btAdapter?.getRemoteDevice(address)
            if (device != null) {
                connectGatt(device)
            } else {
                uiLog("未找到设备 $address，开始扫描")
                startBleScan()
            }
        } catch (e: Exception) {
            uiLog("直连失败: ${e.message}，开始扫描")
            startBleScan()
        }
    }

    private var scanning = false
    private var scanTimeoutTask: Runnable? = null

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult?) {
            val d = result?.device ?: return
            val rssi = result.rssi
            val name = d.name ?: try { result.scanRecord?.deviceName } catch (e: Exception) { null }
            val displayName = name ?: "(未知)"
            val addr = d.address ?: return
            val isTarget = targetNames.any { displayName.contains(it, ignoreCase = true) }

            // 记录已发现设备
            discoveredDevices[addr] = BleDeviceInfo(displayName, addr, rssi, isTarget)
            onDeviceFound?.invoke(displayName, addr, rssi, isTarget)

            // 自动连接匹配的目标设备（如果用户没有手动指定地址）
            if (isTarget && targetDeviceAddr == null) {
                uiLog("发现目标设备: $displayName ($addr) RSSI=$rssi")
                targetDeviceAddr = addr
                prefs.edit().putString("last_ble_addr", addr).apply()
                stopBleScan()
                connectGatt(d)
            }
        }

        override fun onScanFailed(errorCode: Int) {
            uiLog("扫描失败 code=$errorCode")
            scanning = false
            onBleState?.invoke("扫描失败", false)
            handler.postDelayed({ startBleIfNotConnected() }, 3000)
        }
    }

    private fun startBleScan() {
        if (scanning) return
        val scanner = btAdapter?.bluetoothLeScanner
        if (scanner == null) {
            uiLog("⚠️ BLE 扫描器不可用")
            handler.postDelayed({ startBleIfNotConnected() }, 3000)
            return
        }
        scanning = true
        discoveredDevices.clear()
        uiLog("🔍 开始扫描 BLE 设备...")
        onBleState?.invoke("扫描中...", false)
        try {
            scanner.startScan(scanCallback)
        } catch (e: SecurityException) {
            uiLog("⚠️ 缺扫描权限: ${e.message}")
            scanning = false
            return
        }

        // 12 秒扫描超时
        scanTimeoutTask?.let { handler.removeCallbacks(it) }
        scanTimeoutTask = Runnable {
            if (scanning) {
                stopBleScan()
                uiLog("扫描结束 · 发现 ${discoveredDevices.size} 个设备")
                if (discoveredDevices.isEmpty()) {
                    uiLog("未发现设备，3s 后重试")
                    onBleState?.invoke("未发现设备", false)
                    handler.postDelayed({ startBleIfNotConnected() }, 3000)
                } else {
                    onBleState?.invoke("发现 ${discoveredDevices.size} 个设备", false)
                    // 如果有匹配的目标设备，自动连接
                    val target = discoveredDevices.values.firstOrNull { it.isTarget }
                    if (target != null && targetDeviceAddr == null) {
                        targetDeviceAddr = target.address
                        prefs.edit().putString("last_ble_addr", target.address).apply()
                        uiLog("自动连接: ${target.name}")
                        tryConnectByAddress(target.address)
                    }
                }
            }
        }
        handler.postDelayed(scanTimeoutTask!!, 12000)
    }

    private fun stopBleScan() {
        if (!scanning) return
        scanning = false
        scanTimeoutTask?.let { handler.removeCallbacks(it) }
        try { btAdapter?.bluetoothLeScanner?.stopScan(scanCallback) } catch (e: Exception) {}
    }

    // ================================================================
    //  BLE GATT 连接
    // ================================================================
    private fun connectGatt(device: BluetoothDevice) {
        stopBleScan()
        bleConnecting = true
        bleConnected = false
        bleReady = false
        sendBleStatus(false, true, null)
        onBleState?.invoke("连接中: ${device.name ?: device.address}", false)
        uiLog("正在连接 ${device.name ?: "?"} (${device.address})")

        val cb = object : BluetoothGattCallback() {
            override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
                when (newState) {
                    BluetoothProfile.STATE_CONNECTED -> {
                        bleConnected = true
                        bleConnecting = false
                        uiLog("BLE 已连接 · 发现服务中...")
                        sendBleStatus(true, false, null)
                        onBleState?.invoke("已连接 · 发现服务", false)
                        handler.post {
                            try { g.discoverServices() } catch (e: Exception) {}
                        }
                    }
                    BluetoothProfile.STATE_DISCONNECTED -> {
                        bleConnected = false
                        bleConnecting = false
                        bleReady = false
                        writeChar = null
                        notifyChar = null
                        uiLog("⚠️ BLE 断开（status=$status）")
                        sendBleStatus(false, false, "断开 $status")
                        onBleState?.invoke("BLE 断开 · 自动重连中", false)
                        // autoConnect=true 时 Android 会自动重连，不要立即 close GATT
                        // 等 15 秒如果还没连上，再触发主动重连
                        handler.postDelayed({
                            if (!bleConnected && running.get()) {
                                uiLog("15s 未恢复，触发主动重连")
                                try { gatt?.close() } catch (e: Exception) {}
                                gatt = null
                                startBleIfNotConnected()
                            }
                        }, 15000)
                    }
                }
            }

            override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    uiLog("⚠️ 服务发现失败 status=$status")
                    sendBleStatus(false, false, "服务发现失败")
                    return
                }
                val svc = g.getService(svcUUID)
                if (svc == null) {
                    uiLog("⚠️ 未找到目标服务 $svcUUID")
                    uiLog("  可能不是啵啵贝设备，或 Pro 版 UUID 已变更")
                    // 列出实际服务帮助排查
                    for (s in g.services) {
                        uiLog("  实际服务: ${s.uuid}")
                    }
                    sendBleStatus(false, false, "未找到服务")
                    onBleState?.invoke("非目标设备", false)
                    return
                }
                writeChar = svc.getCharacteristic(writeUUID)
                notifyChar = svc.getCharacteristic(notifyUUID)
                if (writeChar == null || notifyChar == null) {
                    uiLog("⚠️ 未找到写/通知特征，Pro 协议可能不同")
                    uiLog("  写特征: ${writeChar != null} 通知特征: ${notifyChar != null}")
                    sendBleStatus(false, false, "未找到特征")
                    onBleState?.invoke("特征不匹配", false)
                    return
                }
                // 读电池电量（标准 BLE 电池服务）
                val batterySvc = g.getService(batterySvcUUID)
                if (batterySvc != null) {
                    batteryChar = batterySvc.getCharacteristic(batteryCharUUID)
                    if (batteryChar != null) {
                        uiLog("发现电池服务")
                        // 读一次当前电量
                        try { g.readCharacteristic(batteryChar) } catch (e: Exception) {}
                        // 订阅电量变化通知
                        try { g.setCharacteristicEnabled(batteryChar!!, true) } catch (e: Exception) {}
                    }
                } else {
                    uiLog("设备不支持电池服务")
                }

                // 订阅 NOTIFY
                try { g.setCharacteristicEnabled(notifyChar!!, true) } catch (e: Exception) {}
                uiLog("已订阅 NOTIFY，500ms 后发初始化帧")
                handler.postDelayed({
                    writeFrameToBle(initFrame())
                    uiLog("已发初始化帧 · 等 NOTIFY 2s")
                    handler.postDelayed({
                        bleReady = true
                        uiStatus("✅ 设备就绪 · 对方可控制")
                        uiLog("✅ 设备就绪")
                        onBleState?.invoke("设备就绪", true)
                        sendBleStatus(true, false, null)
                        startBleKeepalive()  // 定期发小帧保活
                    }, 2000)
                }, 500)
            }

            override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
                val v = c.value ?: return
                // 电池电量通知
                if (c.uuid == batteryCharUUID && v.isNotEmpty()) {
                    val level = v[0].toInt() and 0xFF
                    uiLog("🔋 电量: $level%")
                    onBatteryLevel?.invoke(level)
                    return
                }
                // Pro 协议 NOTIFY 分片
                val hex = v.joinToString(" ") { "%02x".format(it) }
                uiLog("← NOTIFY: $hex")
            }

            override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
                if (status == BluetoothGatt.GATT_SUCCESS && c.uuid == batteryCharUUID) {
                    val v = c.value
                    if (v != null && v.isNotEmpty()) {
                        val level = v[0].toInt() and 0xFF
                        uiLog("🔋 电量: $level%")
                        onBatteryLevel?.invoke(level)
                    }
                }
            }

            override fun onCharacteristicWrite(
                g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int
            ) {
                // 写入完成 → 触发队列下一帧
                onWriteComplete(status)
            }
        }

        // autoConnect=true：后台保活 + 自动重连 + 出范围后回来自动恢复
        try {
            gatt = if (Build.VERSION.SDK_INT >= 23)
                device.connectGatt(this, true, cb, BluetoothDevice.TRANSPORT_LE)
            else
                device.connectGatt(this, true, cb)
        } catch (e: SecurityException) {
            uiLog("⚠️ 连接权限不足: ${e.message}")
            sendBleStatus(false, false, "权限不足")
            onBleState?.invoke("权限不足", false)
        }
    }

    // ---- BLE 后台保活：定期读 RSSI 防止空闲断开 ----
    private var bleKeepaliveTask: Runnable? = null

    private fun startBleKeepalive() {
        bleKeepaliveTask?.let { handler.removeCallbacks(it) }
        val r = object : Runnable {
            override fun run() {
                if (!bleConnected || gatt == null) {
                    bleKeepaliveTask = null
                    return
                }
                try {
                    // 读 RSSI 不写数据，不干扰正常使用，但能保持连接活跃
                    gatt?.readRemoteRssi()
                } catch (e: Exception) {}
                handler.postDelayed(this, 30_000)  // 每 30 秒
            }
        }
        bleKeepaliveTask = r
        handler.postDelayed(r, 30_000)
    }

    private fun disconnectBle() {
        bleKeepaliveTask?.let { handler.removeCallbacks(it) }
        bleKeepaliveTask = null
        try { gatt?.disconnect() } catch (e: Exception) {}
        try { gatt?.close() } catch (e: Exception) {}
        gatt = null
        writeChar = null
        notifyChar = null
        bleConnected = false
        bleConnecting = false
        bleReady = false
    }

    // ================================================================
    //  BLE 写入队列（关键！逐帧排队，等上一帧写完再发下一帧）
    // ================================================================
    private val writeQueue = java.util.concurrent.ConcurrentLinkedQueue<ByteArray>()
    private var writeBusy = false
    private var writeTimeoutTask: Runnable? = null

    private fun BluetoothGatt.setCharacteristicEnabled(
        c: BluetoothGattCharacteristic, enable: Boolean
    ) {
        setCharacteristicNotification(c, enable)
        val d = c.getDescriptor(cccdUUID)
        if (d != null) {
            d.value = if (enable)
                BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            else
                BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE
            writeDescriptor(d)
        }
    }

    /** 将帧加入写入队列，自动排队发送 */
    private fun enqueueFrame(b: ByteArray) {
        writeQueue.offer(b)
        if (!writeBusy) dequeueAndWrite()
    }

    private fun dequeueAndWrite() {
        if (!bleConnected || writeChar == null) {
            writeQueue.clear()
            writeBusy = false
            return
        }
        val frame = writeQueue.poll()
        if (frame == null) {
            writeBusy = false
            return
        }
        writeBusy = true
        try {
            writeChar!!.value = frame
            writeChar!!.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            val ok = gatt?.writeCharacteristic(writeChar) ?: false
            if (!ok) {
                // 写入队列满，等 50ms 重试
                // 把当前帧放回队首
                val retryList = mutableListOf(frame)
                while (writeQueue.isNotEmpty()) writeQueue.poll()?.let { retryList.add(it) }
                for (f in retryList) writeQueue.offer(f)
                writeBusy = false
                handler.postDelayed({ dequeueAndWrite() }, 50)
                return
            }
            // 设置 3 秒超时，如果没收到 onCharacteristicWrite 回调则继续下一帧
            writeTimeoutTask?.let { handler.removeCallbacks(it) }
            writeTimeoutTask = Runnable {
                writeBusy = false
                dequeueAndWrite()
            }
            handler.postDelayed(writeTimeoutTask!!, 3000)
        } catch (e: Exception) {
            uiLog("⚠️ 写入异常 ${e.message}")
            writeBusy = false
            handler.postDelayed({ dequeueAndWrite() }, 100)
        }
    }

    /** GATT 写入完成回调——触发下一帧 */
    private fun onWriteComplete(status: Int) {
        writeTimeoutTask?.let { handler.removeCallbacks(it) }
        writeBusy = false
        if (status != BluetoothGatt.GATT_SUCCESS) {
            uiLog("⚠️ BLE 写入失败 status=$status，继续下一帧")
        }
        // 帧间小间隔，防止设备缓冲区溢出
        handler.postDelayed({ dequeueAndWrite() }, 60)
    }

    /** @return 是否成功加入队列 */
    private fun writeFrameToBle(b: ByteArray): Boolean {
        if (!bleConnected || writeChar == null) {
            return false
        }
        enqueueFrame(b)
        return true
    }

    // ================================================================
    //  反馈给服务器
    // ================================================================
    private fun sendBleStatus(connected: Boolean, connecting: Boolean, error: String?) {
        try {
            val m = JSONObject()
                .put("type", "ble_status")
                .put("ble_connected", connected)
                .put("ble_connecting", connecting)
            if (error != null) m.put("error", error)
            ws?.send(m.toString())
        } catch (e: Exception) {}
    }

    private fun reportBleStatus() {
        sendBleStatus(bleConnected, bleConnecting, null)
    }

    // ================================================================
    //  紧急停止
    // ================================================================
    private fun handleEmergencyStop() {
        uiLog("🛑 急停 · 全部清零")
        for (fn in listOf("suction", "vibration", "electric")) {
            val (m1, m2) = fnModes[fn] ?: continue
            writeFrameToBle(frameBytes(m1, 0, m2))
        }
        try {
            ws?.send(JSONObject().put("type", "emergency_stop").toString())
        } catch (e: Exception) {}
    }

    // ================================================================
    //  协议探测：找出 Pro 版各功能的正确通道字节
    // ================================================================
    private var probeRunning = false
    private val probePairs = listOf(
        // (mode1, mode2, 标签)
        0x01 to 0x02,   // 测试1：旧版震动
        0x03 to 0x04,   // 测试2：旧版电流
        0x05 to 0x06,   // 测试3：候选A
        0x07 to 0x08,   // 测试4：旧版吮吸（已知可用）
        0x09 to 0x0A,   // 测试5：候选B
        0x0B to 0x0C,   // 测试6：候选C
        0x0D to 0x0E,   // 测试7：候选D
        0x0F to 0x10,   // 测试8：候选E
    )

    private fun startProtocolProbe() {
        if (probeRunning) {
            uiLog("⚠️ 探测已在运行中")
            return
        }
        if (!bleConnected || writeChar == null) {
            uiLog("⚠️ 请先连接 BLE 设备再探测")
            return
        }
        probeRunning = true
        uiLog("=" .repeat(30))
        uiLog("🔬 开始协议探测")
        uiLog("  每次发一个 50% 强度的帧，间隔 5 秒")
        uiLog("  请记录每个编号的体感反馈")
        uiLog("=" .repeat(30))

        // 先发全停清理状态
        for ((m1, m2) in probePairs) {
            writeFrameToBle(frameBytes(m1, 0, m2))
            Thread.sleep(50)
        }
        uiLog("已发送全停帧，1秒后开始...")
        handler.postDelayed({ runProbeStep(0) }, 1000)
    }

    private fun runProbeStep(index: Int) {
        if (index >= probePairs.size) {
            probeRunning = false
            uiLog("=" .repeat(30))
            uiLog("✅ 探测完成！")
            uiLog("  请反馈每组的体感结果：")
            uiLog("  测试1(01/02)=? 测试2(03/04)=?")
            uiLog("  测试3(05/06)=? 测试4(07/08)=吮吸✓")
            uiLog("  测试5(09/0A)=? 测试6(0B/0C)=?")
            uiLog("  测试7(0D/0E)=? 测试8(0F/10)=?")
            uiLog("=" .repeat(30))
            onStatus?.invoke("探测完成 · 查看日志")
            return
        }

        val (m1, m2) = probePairs[index]
        val intensity = 50  // 0x32
        val frame = frameBytes(m1, intensity, m2)
        val hex = frame.joinToString(" ") { "%02x".format(it) }

        val label = when (index) {
            0 -> "测试1 - 旧震动位置"
            1 -> "测试2 - 旧电流位置"
            2 -> "测试3 - 候选A"
            3 -> "测试4 - 旧吮吸位置(已知)"
            4 -> "测试5 - 候选B"
            5 -> "测试6 - 候选C"
            6 -> "测试7 - 候选D"
            7 -> "测试8 - 候选E"
            else -> "测试${index + 1}"
        }

        uiLog("")
        uiLog(">>> $label <<<")
        uiLog("   字节: $hex")
        uiLog("   mode1=0x%02x mode2=0x%02x intensity=$intensity".format(m1, m2))

        val ok = writeFrameToBle(frame)
        uiLog("   写入: ${if (ok) "OK" else "失败"}")

        // 2 秒后归零，4 秒后继续下一个
        handler.postDelayed({
            writeFrameToBle(frameBytes(m1, 0, m2))
        }, 2500)

        handler.postDelayed({
            runProbeStep(index + 1)
        }, 5000)
    }

    private fun stopAllJob() {
        running.set(false)
        try { if (wakeLock.isHeld) wakeLock.release() } catch (e: Exception) {}
        disconnectBle()
        try { ws?.close() } catch (e: Exception) {}
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    // ================================================================
    //  辅助
    // ================================================================
    private fun hexToBytes(hex: String): ByteArray {
        val clean = hex.replace(" ", "")
        val b = ByteArray(clean.length / 2)
        for (i in b.indices) b[i] = (clean.substring(i * 2, i * 2 + 2).toInt(16)).toByte()
        return b
    }

    private fun uiLog(s: String) {
        Log.i(tag, s)
        onLog?.invoke(s)
    }

    private fun uiStatus(s: String) {
        Log.i(tag, "STATUS: $s")
        onStatus?.invoke(s)
        startForegroundNotif(s)
    }
}
