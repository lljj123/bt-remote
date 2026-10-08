package com.orangepi.btremote

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothSocket
import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.UUID
import java.util.Timer
import java.util.TimerTask
import java.util.concurrent.LinkedBlockingQueue

/** One cancellable connection owns its socket, decoder, queue and worker threads. */
class SppClient(ctx: Context) {
    companion object {
        val SPP_UUID: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
    }
    var onLine: ((String) -> Unit)? = null
    var onRaw: ((ByteArray) -> Unit)? = null
    var onState: ((String, String) -> Unit)? = null
    private val adapter = (ctx.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
    private class Connection {
        var socket: BluetoothSocket? = null
        var connector: Thread? = null
        var reader: Thread? = null
        var writer: Thread? = null
        val queue = LinkedBlockingQueue<ByteArray>(256)
        var connected = false
        var raw = false
        var shellRequest: Any? = null
        val timer = Timer(true)
        var shellTimeout: TimerTask? = null
    }
    private var current: Connection? = null
    fun isSupported() = adapter != null
    fun isEnabled(): Boolean = try { adapter?.isEnabled == true } catch (_: SecurityException) { false }
    @Synchronized fun isConnected() = current?.connected == true
    @Synchronized fun isRaw() = current?.raw == true

    @SuppressLint("MissingPermission")
    fun bondedDevices(): String {
        val arr = JSONArray()
        try {
            adapter?.bondedDevices?.forEach {
                arr.put(JSONObject().put("name", it.name ?: "Bluetooth").put("mac", it.address))
            }
        } catch (_: SecurityException) { }
        return arr.toString()
    }

    @Synchronized
    @SuppressLint("MissingPermission")
    fun connect(mac: String) {
        disconnect()
        if (!isEnabled()) {
            onState?.invoke("error", "Bluetooth unavailable or permission denied")
            return
        }
        val c = Connection()
        current = c
        onState?.invoke("connecting", mac)
        c.connector = Thread({
            val errors = StringBuilder()
            try {
                val device = adapter!!.getRemoteDevice(mac)
                try { adapter.cancelDiscovery() } catch (_: SecurityException) { }
                for (strategy in 0..2) {
                    if (!owns(c)) return@Thread
                    var attempt: BluetoothSocket? = null
                    try {
                        val sock = openSocket(device, strategy)
                        attempt = sock
                        synchronized(this) {
                            if (current !== c) { sock.close(); return@Thread }
                            c.socket = sock // disconnect can cancel a blocking connect()
                        }
                        val deadline = object : TimerTask() {
                            override fun run() { try { sock.close() } catch (_: Exception) { } }
                        }
                        c.timer.schedule(deadline, 15000)
                        try { sock.connect() } finally { deadline.cancel() }
                        val input = sock.inputStream
                        val output = sock.outputStream
                        synchronized(this) {
                            if (current !== c) { sock.close(); return@Thread }
                            c.connected = true
                            c.writer = Thread({
                                try {
                                    while (owns(c)) {
                                        val data = c.queue.take()
                                        output.write(data)
                                        output.flush()
                                    }
                                } catch (e: Exception) { fail(c, e) }
                            }, "spp-writer").also { it.start() }
                            c.reader = Thread(reader@{
                                val decoder = WireDecoder(
                                    { line ->
                                        val msg = JSONObject(line)
                                        val ready = msg.optString("t") == "ev" && msg.optString("name") == "term.ready"
                                        if (ready && msg.optJSONObject("data")?.optString("protocol") != "escaped-v1")
                                            throw IOException("Please update board terminal protocol")
                                        if (msg.optString("t") == "res" && msg.opt("id") == c.shellRequest && !msg.optBoolean("ok")) {
                                            c.shellRequest = null
                                            c.shellTimeout?.cancel()
                                        }
                                        onLine?.invoke(line)
                                        ready
                                    },
                                    { onRaw?.invoke(it) },
                                    { raw -> c.raw = raw; c.shellRequest = null; c.shellTimeout?.cancel() }
                                )
                                try {
                                    val bytes = ByteArray(4096)
                                    while (owns(c)) {
                                        val n = input.read(bytes)
                                        if (n < 0) throw IOException("Connection closed")
                                        synchronized(this) {
                                            if (current !== c) return@reader
                                            decoder.feed(bytes, n)
                                        }
                                    }
                                } catch (e: Exception) { fail(c, e) }
                            }, "spp-reader")
                            onState?.invoke("connected", mac)
                            c.reader!!.start()
                        }
                        return@Thread
                    } catch (e: Exception) {
                        try { attempt?.close() } catch (_: Exception) { }
                        errors.append("Strategy ").append(strategy + 1).append(": ").append(e.message).append('\n')
                    }
                }
            } catch (e: Exception) { errors.append(e.message) }
            fail(c, IOException(errors.toString()))
        }, "spp-connect").also { it.start() }
    }

    @SuppressLint("MissingPermission")
    private fun openSocket(dev: BluetoothDevice, strategy: Int): BluetoothSocket = when (strategy) {
        0 -> dev.createRfcommSocketToServiceRecord(SPP_UUID)
        1 -> dev.createInsecureRfcommSocketToServiceRecord(SPP_UUID)
        else -> dev.javaClass.getMethod("createRfcommSocket", Int::class.javaPrimitiveType!!)
            .invoke(dev, 1) as BluetoothSocket
    }

    @Synchronized private fun owns(c: Connection) = current === c
    @Synchronized private fun fail(c: Connection, error: Exception) {
        if (current !== c) return
        val wasConnected = c.connected
        disconnect()
        onState?.invoke(if (wasConnected) "disconnected" else "error", error.message ?: "Bluetooth error")
    }

    @Synchronized fun send(line: String) {
        val c = current ?: throw IOException("Not connected")
        if (!c.connected || c.raw || c.shellRequest != null) throw IOException("Protocol channel is busy")
        val bytes = (line + "\n").toByteArray(Charsets.UTF_8)
        require(bytes.size <= 4097) { "Protocol line too long" }
        val msg = JSONObject(line)
        if (msg.optString("name") == "term.shell") {
            c.shellRequest = msg.get("id")
            c.shellTimeout = object : TimerTask() {
                override fun run() {
                    synchronized(this@SppClient) {
                        if (current === c && c.shellRequest != null)
                            fail(c, IOException("Terminal handshake timed out"))
                    }
                }
            }.also { c.timer.schedule(it, 20000) }
        }
        enqueue(c, bytes)
    }

    @Synchronized fun sendRaw(data: ByteArray) {
        val c = current ?: return
        if (c.raw && data.isNotEmpty()) enqueue(c, data)
    }
    fun sendResize(cols: Int, rows: Int) {
        sendRaw(byteArrayOf(1, 0x52, (cols shr 8).toByte(), cols.toByte(), (rows shr 8).toByte(), rows.toByte()))
    }
    fun sendClose() { sendRaw(byteArrayOf(1, 0x43)) }
    private fun enqueue(c: Connection, bytes: ByteArray) {
        if (bytes.size > 262144 || !c.queue.offer(bytes)) {
            fail(c, IOException("Bluetooth send buffer full"))
            throw IOException("Bluetooth send buffer full")
        }
    }

    @Synchronized fun disconnect() {
        val c = current ?: return
        current = null // stale workers can no longer emit events or close a new connection
        c.connected = false
        try { c.socket?.close() } catch (_: Exception) { }
        c.connector?.interrupt()
        c.reader?.interrupt()
        c.writer?.interrupt()
        c.queue.clear()
        c.timer.cancel()
    }
}
