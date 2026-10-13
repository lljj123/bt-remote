package com.orangepi.btremote

import android.Manifest
import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.FileInputStream
import java.io.FileOutputStream
import java.security.MessageDigest

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
 *   pickUploadFile/readUploadChunk/finishUploadFile -> 从系统文件选择器分块读取
 *   createDownloadFile/writeDownloadChunk/finishDownloadFile -> 分块保存到系统文件选择器
 *   getClipboardText/setClipboardText -> 终端复制粘贴使用原生系统剪贴板
 *   disconnect()
 *
 * 原生 -> JS 回调：
 *   window.onBtLine(lineJson)        行相位：每收到一行
 *   window.onBtState({state,info})   连接状态变化
 *   window.onBtTraffic({up_bps,down_bps}) 手机→板子/板子→手机实时速率
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

    /** Android Storage Access Framework handles; file bytes never need broad storage permission. */
    private val fileLock = Any()
    private var uploadDescriptor: ParcelFileDescriptor? = null
    private var uploadInput: FileInputStream? = null
    private var uploadOffset = 0L
    private var uploadSize = 0L
    private var uploadDigest: MessageDigest? = null
    private var downloadUri: Uri? = null
    private var downloadDescriptor: ParcelFileDescriptor? = null
    private var downloadOutput: FileOutputStream? = null
    private var downloadOffset = 0L
    private var downloadDigest: MessageDigest? = null

    private val pickUpload = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri == null) {
            emit("window.onLocalUploadPicked", JSONObject().put("ok", false)
                .put("cancelled", true).toString())
        } else prepareUploadSource(uri)
    }

    private val createDownload = registerForActivityResult(ActivityResultContracts.CreateDocument("*/*")) { uri ->
        if (uri == null) {
            emit("window.onLocalDownloadPicked", JSONObject().put("ok", false)
                .put("cancelled", true).toString())
        } else prepareDownloadTarget(uri)
    }

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
        spp.onTraffic = { up, down ->
            val payload = JSONObject().put("up_bps", up).put("down_bps", down).toString()
            emit("window.onBtTraffic", payload)
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
     * WebView 会被打爆（每次调用都要过一次 JS 引擎边界）。这里在 24ms 窗口内
     * 把数据攒起来一起送；JS 侧再按 xterm 写入回调逐帧消费，给触摸滚动留出主线程时间。
     */
    private fun queueRaw(bytes: ByteArray) {
        synchronized(rawBuf) {
            if (destroyed) return
            rawBuf.write(bytes)
            if (rawBuf.size() >= 64 * 1024) flushRaw()
            if (rawScheduled) return
            rawScheduled = true
            ui.postDelayed({ flushRaw() }, 24)
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

    private fun uriMeta(uri: Uri, fallback: String): Pair<String, Long> {
        var name = fallback
        var size = -1L
        try {
            contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
                null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val nameColumn = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    val sizeColumn = cursor.getColumnIndex(OpenableColumns.SIZE)
                    if (nameColumn >= 0 && !cursor.isNull(nameColumn)) name = cursor.getString(nameColumn)
                    if (sizeColumn >= 0 && !cursor.isNull(sizeColumn)) size = cursor.getLong(sizeColumn)
                }
            }
        } catch (_: Exception) { }
        return Pair(name, size)
    }

    private fun prepareUploadSource(uri: Uri) {
        val payload = JSONObject()
        try {
            try {
                contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            } catch (_: Exception) { }
            synchronized(fileLock) {
                closeUploadSourceLocked()
                val descriptor = contentResolver.openFileDescriptor(uri, "r")
                    ?: throw IllegalStateException("无法打开所选文件")
                val meta = uriMeta(uri, "upload.bin")
                val size = if (meta.second >= 0) meta.second else descriptor.statSize
                if (size < 0) {
                    descriptor.close()
                    throw IllegalStateException("文件提供方没有返回大小")
                }
                uploadDescriptor = descriptor
                uploadInput = FileInputStream(descriptor.fileDescriptor)
                uploadOffset = 0
                uploadSize = size
                uploadDigest = MessageDigest.getInstance("SHA-256")
                payload.put("ok", true).put("name", meta.first).put("size", size)
            }
        } catch (error: Exception) {
            synchronized(fileLock) { closeUploadSourceLocked() }
            payload.put("ok", false).put("error", error.message ?: "无法读取所选文件")
        }
        emit("window.onLocalUploadPicked", payload.toString())
    }

    private fun prepareDownloadTarget(uri: Uri) {
        val payload = JSONObject()
        try {
            synchronized(fileLock) {
                closeDownloadTargetLocked(true)
                val descriptor = contentResolver.openFileDescriptor(uri, "rwt")
                    ?: throw IllegalStateException("无法创建目标文件")
                downloadUri = uri
                downloadDescriptor = descriptor
                downloadOutput = FileOutputStream(descriptor.fileDescriptor)
                downloadOffset = 0
                downloadDigest = MessageDigest.getInstance("SHA-256")
                payload.put("ok", true).put("name", uriMeta(uri, "download.bin").first)
            }
        } catch (error: Exception) {
            synchronized(fileLock) { closeDownloadTargetLocked(true) }
            payload.put("ok", false).put("error", error.message ?: "无法创建目标文件")
        }
        emit("window.onLocalDownloadPicked", payload.toString())
    }

    private fun closeUploadSourceLocked() {
        try { uploadInput?.close() } catch (_: Exception) { }
        try { uploadDescriptor?.close() } catch (_: Exception) { }
        uploadInput = null
        uploadDescriptor = null
        uploadOffset = 0
        uploadSize = 0
        uploadDigest = null
    }

    private fun closeDownloadTargetLocked(deletePartial: Boolean) {
        try { downloadOutput?.flush() } catch (_: Exception) { }
        try { downloadDescriptor?.fileDescriptor?.sync() } catch (_: Exception) { }
        try { downloadOutput?.close() } catch (_: Exception) { }
        try { downloadDescriptor?.close() } catch (_: Exception) { }
        val uri = downloadUri
        downloadOutput = null
        downloadDescriptor = null
        downloadUri = null
        downloadOffset = 0
        downloadDigest = null
        if (deletePartial && uri != null) {
            try { contentResolver.delete(uri, null, null) } catch (_: Exception) { }
        }
    }

    override fun onDestroy() {
        destroyed = true
        spp.disconnect()
        synchronized(fileLock) {
            closeUploadSourceLocked()
            closeDownloadTargetLocked(true)
        }
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

        @JavascriptInterface
        fun getClipboardText(): String = try {
            val clipboard = getSystemService(ClipboardManager::class.java)
            val clip = clipboard.primaryClip
            if (clip == null || clip.itemCount == 0) ""
            else clip.getItemAt(0).coerceToText(this@MainActivity).toString()
                .take(MAX_CLIPBOARD_CHARS)
        } catch (_: Exception) {
            ""
        }

        @JavascriptInterface
        fun setClipboardText(text: String): Boolean = try {
            val clipboard = getSystemService(ClipboardManager::class.java)
            clipboard.setPrimaryClip(ClipData.newPlainText("板子遥控终端", text.take(MAX_CLIPBOARD_CHARS)))
            true
        } catch (_: Exception) {
            false
        }

        @JavascriptInterface
        fun pickUploadFile() {
            ui.post { pickUpload.launch(arrayOf("*/*")) }
        }

        @JavascriptInterface
        fun readUploadChunk(offset: Long, maxBytes: Int): String = synchronized(fileLock) {
            val result = JSONObject()
            try {
                val input = uploadInput ?: throw IllegalStateException("没有已选择的上传文件")
                if (offset != uploadOffset) throw IllegalStateException("本地文件偏移应为 $uploadOffset")
                val amount = maxBytes.coerceIn(1, FILE_BRIDGE_CHUNK)
                val buffer = ByteArray(amount)
                val count = input.read(buffer).coerceAtLeast(0)
                if (count > 0) uploadDigest?.update(buffer, 0, count)
                uploadOffset += count
                result.put("ok", true)
                    .put("offset", offset)
                    .put("next_offset", uploadOffset)
                    .put("data_b64", Base64.encodeToString(buffer.copyOf(count), Base64.NO_WRAP))
                    .put("eof", uploadOffset >= uploadSize || count == 0)
            } catch (error: Exception) {
                result.put("ok", false).put("error", error.message ?: "读取本地文件失败")
            }
            result.toString()
        }

        @JavascriptInterface
        fun finishUploadFile(): String = synchronized(fileLock) {
            val result = JSONObject()
            try {
                if (uploadInput == null) throw IllegalStateException("没有活动的上传文件")
                if (uploadOffset != uploadSize) throw IllegalStateException("本地文件尚未读取完成")
                val digest = uploadDigest?.digest()?.joinToString("") {
                    "%02x".format(it.toInt() and 0xff)
                }
                    ?: throw IllegalStateException("没有校验值")
                result.put("ok", true).put("size", uploadOffset).put("sha256", digest)
                closeUploadSourceLocked()
            } catch (error: Exception) {
                result.put("ok", false).put("error", error.message ?: "结束上传失败")
            }
            result.toString()
        }

        @JavascriptInterface
        fun cancelUploadFile() = synchronized(fileLock) { closeUploadSourceLocked() }

        @JavascriptInterface
        fun createDownloadFile(name: String) {
            val safeName = name.substringAfterLast('/').substringAfterLast('\\').take(180)
                .ifBlank { "download.bin" }
            ui.post { createDownload.launch(safeName) }
        }

        @JavascriptInterface
        fun writeDownloadChunk(offset: Long, b64: String): String = synchronized(fileLock) {
            val result = JSONObject()
            try {
                val output = downloadOutput ?: throw IllegalStateException("没有已选择的保存位置")
                if (offset != downloadOffset) throw IllegalStateException("本地保存偏移应为 $downloadOffset")
                val bytes = Base64.decode(b64, Base64.NO_WRAP)
                if (bytes.size > FILE_BRIDGE_CHUNK) throw IllegalArgumentException("下载分块过大")
                output.write(bytes)
                downloadDigest?.update(bytes)
                downloadOffset += bytes.size
                result.put("ok", true).put("offset", downloadOffset)
            } catch (error: Exception) {
                result.put("ok", false).put("error", error.message ?: "写入下载文件失败")
            }
            result.toString()
        }

        @JavascriptInterface
        fun finishDownloadFile(success: Boolean, expectedSha256: String): String = synchronized(fileLock) {
            val result = JSONObject()
            try {
                if (downloadOutput == null) throw IllegalStateException("没有活动的下载文件")
                val size = downloadOffset
                val digest = if (success) downloadDigest?.digest()?.joinToString("") {
                    "%02x".format(it.toInt() and 0xff)
                } else null
                val verified = success && (expectedSha256.isBlank() ||
                    digest.equals(expectedSha256, ignoreCase = true))
                closeDownloadTargetLocked(!verified)
                result.put("ok", verified).put("size", size)
                if (digest != null) result.put("sha256", digest)
                if (success && !verified) result.put("error", "SHA256 校验失败，已删除下载文件")
            } catch (error: Exception) {
                closeDownloadTargetLocked(true)
                result.put("ok", false).put("error", error.message ?: "结束下载失败")
            }
            result.toString()
        }
    }

    companion object {
        private const val REQ_BT = 1001
        private const val FILE_BRIDGE_CHUNK = 1536
        private const val MAX_CLIPBOARD_CHARS = 256 * 1024
    }
}
