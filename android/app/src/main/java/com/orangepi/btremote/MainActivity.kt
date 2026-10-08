package com.orangepi.btremote

import android.Manifest
import android.annotation.SuppressLint
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import org.json.JSONObject
import java.io.ByteArrayOutputStream

/**
 * 唯一的 Activity：一个 WebView 承载仪表盘界面，通过 JS 桥把
 * 蓝牙收发能力暴露给页面。
 *
 * JS 侧约定（window.AndroidBt.*）：
 *   devices()            -> 已配对设备列表（JSON 数组字符串）
 *   isEnabled()          -> 蓝牙是否开启
 *   isSupported()        -> 本机是否有蓝牙适配器
 *   isConnected()        -> 当前是否已建链
 *   connect(mac)
 *   send(line)           -> 发送一行 JSON（行相位）
 *   sendRaw(b64)         -> 发送原始字节（裸相位，base64 编码）
 *   sendResize(cols,rows)-> 调整远端 PTY 窗口大小（走带内控制帧）
 *   sendClose()          -> 请求远端结束终端会话
 *   disconnect()
 *
 * 原生 -> JS 回调：
 *   window.onBtLine(lineJson)        行相位：每收到一行
 *   window.onBtState({state,info})   连接状态变化
 *   window.onBtRaw(b64)              裸相位：终端字节流（base64）
 *
 * 为什么用 base64：JavascriptInterface 只支持基本类型与 String，
 * 传二进制必须先编码。base64 比数字数组快得多，且是 ASCII，过桥安全。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var spp: SppClient
    private lateinit var tokens: TokenStore
    @Volatile private var destroyed = false
    @Volatile private var deliveryEpoch = 0L

    private val ui = Handler(Looper.getMainLooper())

    /** 终端输出的合帧缓冲：见 flushRaw 注释 */
    private val rawBuf = ByteArrayOutputStream(16 * 1024)
    private var rawScheduled = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        web = WebView(this)
        setContentView(web)

        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = true
            // xterm.js / vendor 资源是 file:// 相对路径加载，放开更保险
            @Suppress("DEPRECATION")
            allowFileAccessFromFileURLs = false
            loadWithOverviewMode = true
            useWideViewPort = false
        }

        tokens = TokenStore(this)
        spp = SppClient(this)
        spp.onLine = { line -> flushRaw(); emit("window.onBtLine", line) }
        spp.onRaw = { bytes -> queueRaw(bytes) }
        spp.onState = { state, info ->
            val payload = JSONObject().put("state", state).put("info", info).toString()
            emit("window.onBtState", payload)
        }

        web.addJavascriptInterface(Bridge(), "AndroidBt")
        web.loadUrl("file:///android_asset/dashboard.html")

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                // 交给页面自己决定要不要回退，页面回 true 表示已处理
                web.evaluateJavascript("(window.onAndroidBack && window.onAndroidBack()) || false") { r ->
                    if (r != "true") finish()
                }
            }
        })

        requestBluetoothPermissions()
    }

    private fun requestBluetoothPermissions() {
        val perms: Array<String> = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            arrayOf(
                Manifest.permission.BLUETOOTH_CONNECT,
                Manifest.permission.BLUETOOTH_SCAN
            )
        } else {
            // Only bonded devices are used; no discovery or location on pre-S devices.
            emptyArray()
        }
        if (perms.isNotEmpty()) ActivityCompat.requestPermissions(this, perms, REQ_BT)
    }

    /**
     * 终端输出合帧。
     *
     * 一次 `ls -l` 会产生几十上百个 socket 块，如果每块都调一次 evaluateJavascript，
     * WebView 会被打爆（每次调用都要过一次 JS 引擎边界）。这里在 16ms（约一帧）窗口内
     * 把数据攒起来一起送，既降低调用次数又保证显示流畅（60fps 上限）。
     */
    private fun queueRaw(bytes: ByteArray) {
        synchronized(rawBuf) {
            if (destroyed) return
            rawBuf.write(bytes)
            if (rawBuf.size() >= 64 * 1024) flushRaw()
            if (rawScheduled) return
            rawScheduled = true
            ui.postDelayed({ flushRaw() }, 16)
        }
    }

    private fun flushRaw() {
        synchronized(rawBuf) {
            rawScheduled = false
            val data = rawBuf.toByteArray()
            rawBuf.reset()
            if (data.isNotEmpty()) {
                emit("window.onBtRaw", Base64.encodeToString(data, Base64.NO_WRAP))
            }
        }
    }

    private fun emit(fn: String, json: String) {
        val epoch = deliveryEpoch
        ui.post {
            if (destroyed || epoch != deliveryEpoch) return@post
            // JSONObject.quote 负责把整串安全地嵌成 JS 字符串字面量
            web.evaluateJavascript("$fn(${JSONObject.quote(json)})", null)
        }
    }

    override fun onDestroy() {
        destroyed = true
        spp.disconnect()
        ui.removeCallbacksAndMessages(null)
        web.removeJavascriptInterface("AndroidBt")
        web.destroy()
        super.onDestroy()
    }

    /** 暴露给网页的桥 */
    inner class Bridge {
        @JavascriptInterface
        fun getToken(): String = tokens.read()

        @JavascriptInterface
        fun setToken(token: String): Boolean = tokens.write(token)

        @JavascriptInterface
        fun devices(): String = spp.bondedDevices()

        @JavascriptInterface
        fun isEnabled(): Boolean = spp.isEnabled()

        @JavascriptInterface
        fun isSupported(): Boolean = spp.isSupported()

        @JavascriptInterface
        fun isConnected(): Boolean = spp.isConnected()

        @JavascriptInterface
        fun isRaw(): Boolean = spp.isRaw()

        @JavascriptInterface
        fun connect(mac: String) {
            synchronized(spp) {
                deliveryEpoch++
                synchronized(rawBuf) { rawBuf.reset() }
                spp.connect(mac)
            }
        }

        @JavascriptInterface
        fun send(line: String): Boolean = try { spp.send(line); true } catch (_: Exception) { false }

        /** 裸相位发送：b64 是 base64 编码的原始字节 */
        @JavascriptInterface
        fun sendRaw(b64: String) {
            try {
                spp.sendRaw(Base64.decode(b64, Base64.NO_WRAP))
            } catch (_: Exception) {
            }
        }

        @JavascriptInterface
        fun sendResize(cols: Int, rows: Int) {
            spp.sendResize(cols, rows)
        }

        @JavascriptInterface
        fun sendClose() {
            spp.sendClose()
        }

        @JavascriptInterface
        fun disconnect() {
            synchronized(spp) {
                deliveryEpoch++
                synchronized(rawBuf) { rawBuf.reset() }
                spp.disconnect()
            }
        }
    }

    companion object {
        private const val REQ_BT = 1001
    }
}
