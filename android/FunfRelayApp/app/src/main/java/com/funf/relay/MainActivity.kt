package com.funf.relay

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.method.ScrollingMovementMethod
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private lateinit var tvStatus: TextView
    private lateinit var tvBleState: TextView
    private lateinit var tvBattery: TextView
    private lateinit var tvIntensity: TextView
    private lateinit var tvLog: TextView
    private lateinit var etServer: EditText
    private lateinit var etToken: EditText
    private lateinit var btnScan: Button
    private lateinit var btnProbe: Button
    private lateinit var btnStart: Button
    private lateinit var deviceList: LinearLayout
    private lateinit var tvDeviceHint: TextView
    private lateinit var ivBackground: ImageView
    private val permRequest = 771
    private val backgroundPicker = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        uri ?: return@registerForActivityResult
        try {
            contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            ivBackground.setImageURI(uri)
            getSharedPreferences("funf", MODE_PRIVATE).edit()
                .putString("background_uri", uri.toString())
                .apply()
        } catch (error: SecurityException) {
            appendLog("无法读取所选背景: ${error.message}")
        }
    }

    private val discoveredDevices = LinkedHashMap<String, DeviceEntry>()
    private var connectedAddr: String? = null

    data class DeviceEntry(
        val name: String,
        val address: String,
        val rssi: Int,
        val isTarget: Boolean
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        tvStatus = findViewById(R.id.tvStatus)
        tvBleState = findViewById(R.id.tvBleState)
        tvBattery = findViewById(R.id.tvBattery)
        tvIntensity = findViewById(R.id.tvIntensity)
        tvLog = findViewById(R.id.tvLog)
        tvLog.movementMethod = ScrollingMovementMethod()
        etServer = findViewById(R.id.etServer)
        etToken = findViewById(R.id.etToken)
        btnScan = findViewById(R.id.btnScan)
        btnProbe = findViewById(R.id.btnProbe)
        btnStart = findViewById(R.id.btnStart)
        deviceList = findViewById(R.id.deviceList)
        tvDeviceHint = findViewById(R.id.tvDeviceHint)
        ivBackground = findViewById(R.id.ivBackground)

        val sp = getSharedPreferences("funf", MODE_PRIVATE)
        etServer.setText(sp.getString("server", "wss://your-domain.com/ws"))
        etToken.setText(sp.getString("token", ""))
        sp.getString("background_uri", null)?.let { value ->
            try {
                ivBackground.setImageURI(Uri.parse(value))
            } catch (_: Exception) {
                sp.edit().remove("background_uri").apply()
            }
        }

        findViewById<Button>(R.id.btnChooseBackground).setOnClickListener {
            backgroundPicker.launch(arrayOf("image/jpeg", "image/png", "image/webp"))
        }
        findViewById<Button>(R.id.btnClearBackground).setOnClickListener {
            ivBackground.setImageDrawable(null)
            sp.edit().remove("background_uri").apply()
        }

        // 启动 / 停止
        btnStart.setOnClickListener { startRelay() }
        findViewById<Button>(R.id.btnStop).setOnClickListener { stopRelay() }

        // 急停
        findViewById<Button>(R.id.btnEmergencyStop).setOnClickListener {
            startService(Intent(this, RelayService::class.java).putExtra("action", "emergency_stop"))
            appendLog("急停指令已发送")
        }

        // 电池优化
        findViewById<Button>(R.id.btnBatteryOpt).setOnClickListener {
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            intent.data = Uri.parse("package:$packageName")
            startActivity(intent)
        }

        // 协议探测按钮
        btnProbe.setOnClickListener {
            appendLog("🔬 开始协议探测...")
            startService(Intent(this, RelayService::class.java).putExtra("action", "probe"))
        }

        // 扫描设备按钮
        btnScan.setOnClickListener {
            discoveredDevices.clear()
            deviceList.removeAllViews()
            deviceList.addView(tvDeviceHint)
            tvDeviceHint.text = "扫描中..."
            tvDeviceHint.visibility = View.VISIBLE
            btnScan.isEnabled = false
            startService(Intent(this, RelayService::class.java).putExtra("action", "scan_ble"))
            // 12 秒后恢复按钮
            window.decorView.postDelayed({ btnScan.isEnabled = true }, 13000)
        }

        // Service → UI 回调
        RelayService.onStatus = { text -> runOnUiThread { tvStatus.text = text } }
        RelayService.onLog = { text -> runOnUiThread { appendLog(text) } }

        RelayService.onBleState = { state, ready ->
            runOnUiThread {
                tvBleState.text = state
                tvBleState.setBackgroundColor(
                    if (ready) Color.parseColor("#446633")  // 暗绿底
                    else Color.parseColor("#445566")         // 暗蓝底
                )
            }
        }

        RelayService.onDeviceFound = { name, addr, rssi, isTarget ->
            runOnUiThread {
                if (!discoveredDevices.containsKey(addr)) {
                    discoveredDevices[addr] = DeviceEntry(name, addr, rssi, isTarget)
                    addDeviceToUI(name, addr, rssi, isTarget)
                }
            }
        }

        RelayService.onDeviceState = { s, v, e, i ->
            runOnUiThread { tvIntensity.text = "吮$s 震$v 电$e 入$i" }
        }

        RelayService.onBatteryLevel = { level ->
            runOnUiThread {
                if (level != null) {
                    tvBattery.text = "🔋$level%"
                    tvBattery.setBackgroundColor(
                        if (level < 20) Color.parseColor("#882222")   // 暗红底
                        else if (level < 50) Color.parseColor("#886622")  // 暗黄底
                        else Color.parseColor("#226622")   // 暗绿底
                    )
                } else {
                    tvBattery.text = "🔋--"
                    tvBattery.setBackgroundColor(Color.parseColor("#444444"))
                }
            }
        }

        ensurePerms()
    }

    // ================================================================
    //  动态设备列表
    // ================================================================
    private fun addDeviceToUI(name: String, addr: String, rssi: Int, isTarget: Boolean) {
        tvDeviceHint.visibility = View.GONE

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(10, 8, 10, 8)
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundResource(if (isTarget) R.drawable.device_item_bg else android.R.color.transparent)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = 4 }
        }

        val icon = if (isTarget) "🎯 " else "📱 "
        val tvInfo = TextView(this).apply {
            text = "$icon$name"
            textSize = 12f
            setTextColor(if (isTarget) Color.parseColor("#81C784") else Color.parseColor("#CCCCCC"))
            setTypeface(null, if (isTarget) Typeface.BOLD else Typeface.NORMAL)
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }

        val tvRssi = TextView(this).apply {
            val bars = if (rssi > -50) "████" else if (rssi > -65) "███▁" else if (rssi > -80) "██▁▁" else "█▁▁▁"
            text = "$bars  $rssi dBm"
            textSize = 10f
            setTextColor(Color.parseColor("#AAAAAA"))
            setPadding(8, 0, 8, 0)
        }

        val btnConnect = Button(this).apply {
            text = if (addr == connectedAddr) "已连" else "连接"
            textSize = 10f
            setTextColor(Color.WHITE)
            setPadding(14, 4, 14, 4)
            background = getDrawable(if (addr == connectedAddr)
                R.drawable.btn_dark else R.drawable.btn_accent)
            isEnabled = addr != connectedAddr
            setOnClickListener {
                connectedAddr = addr
                appendLog("手动连接: $name ($addr)")
                updateConnectButtons()
                val intent = Intent(this@MainActivity, RelayService::class.java)
                    .putExtra("action", "connect_device")
                    .putExtra("address", addr)
                startService(intent)
            }
        }

        row.addView(tvInfo)
        row.addView(tvRssi)
        row.addView(btnConnect)
        deviceList.addView(row)
    }

    private fun updateConnectButtons() {
        for (i in 0 until deviceList.childCount) {
            val child = deviceList.getChildAt(i)
            if (child is LinearLayout) {
                for (j in 0 until child.childCount) {
                    val btn = child.getChildAt(j)
                    if (btn is Button) {
                        btn.isEnabled = true  // 允许切换连接
                        btn.text = "连接"
                    }
                }
            }
        }
    }

    // ================================================================
    //  启动 / 停止
    // ================================================================
    private fun startRelay() {
        val server = etServer.text.toString().trim()
        val token = etToken.text.toString().trim()
        if (server.isEmpty()) {
            appendLog("请填服务器地址")
            return
        }
        getSharedPreferences("funf", MODE_PRIVATE).edit()
            .putString("server", server).putString("token", token).apply()

        val intent = Intent(this, RelayService::class.java)
            .putExtra("action", "start")
            .putExtra("server", server)
            .putExtra("token", token)
        if (Build.VERSION.SDK_INT >= 26)
            startForegroundService(intent)
        else
            startService(intent)

        appendLog("中继服务已启动")
        btnScan.isEnabled = true
        btnProbe.isEnabled = true
        btnStart.isEnabled = false
    }

    private fun stopRelay() {
        startService(Intent(this, RelayService::class.java).putExtra("action", "stop"))
        btnScan.isEnabled = false
        btnProbe.isEnabled = false
        btnStart.isEnabled = true
        tvBleState.text = "BLE: 已停止"
        tvBleState.setBackgroundColor(Color.parseColor("#444444"))
        tvStatus.text = "已停止"
        appendLog("中继服务已停止")
    }

    // ================================================================
    //  日志
    // ================================================================
    private fun appendLog(s: String) {
        val ts = android.text.format.DateFormat.format("HH:mm:ss", System.currentTimeMillis())
        tvLog.append("[$ts] $s\n")
        val over = tvLog.layout?.let { it.getLineBottom(it.lineCount - 1) - tvLog.height } ?: 0
        if (over > 0) tvLog.scrollTo(0, over + tvLog.paddingBottom)
    }

    // ================================================================
    //  权限
    // ================================================================
    private fun ensurePerms() {
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 31) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN)
                != PackageManager.PERMISSION_GRANTED
            ) needed.add(Manifest.permission.BLUETOOTH_SCAN)
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                != PackageManager.PERMISSION_GRANTED
            ) needed.add(Manifest.permission.BLUETOOTH_CONNECT)
        } else {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED
            ) needed.add(Manifest.permission.ACCESS_FINE_LOCATION)
        }
        if (Build.VERSION.SDK_INT >= 33) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isNotEmpty())
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), permRequest)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<String?>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        val allGranted = grantResults.all { it == PackageManager.PERMISSION_GRANTED }
        appendLog(if (allGranted) "权限已授予" else "⚠️ 缺权限，部分功能可能不可用")
    }

    override fun onDestroy() {
        RelayService.onStatus = null
        RelayService.onLog = null
        RelayService.onDeviceFound = null
        RelayService.onBleState = null
        RelayService.onBatteryLevel = null
        RelayService.onDeviceState = null
        super.onDestroy()
    }
}
