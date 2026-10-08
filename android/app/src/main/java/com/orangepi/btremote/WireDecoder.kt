package com.orangepi.btremote

import java.io.ByteArrayOutputStream
import java.io.IOException

/** NDJSON + escaped-v1. All state belongs to one connection, including split escapes. */
internal class WireDecoder(
    private val onLine: (String) -> Boolean,
    private val onRaw: (ByteArray) -> Unit,
    private val onPhase: (Boolean) -> Unit
) {
    var raw = false
        private set
    private var escaped = false
    private val line = ByteArrayOutputStream()

    fun feed(bytes: ByteArray, count: Int = bytes.size) {
        val output = ByteArrayOutputStream()
        fun flush() {
            if (output.size() > 0) {
                onRaw(output.toByteArray())
                output.reset()
            }
        }
        for (i in 0 until count) {
            val b = bytes[i].toInt() and 255
            if (raw) {
                if (escaped) {
                    escaped = false
                    when (b) {
                        1 -> output.write(1)
                        0x45 -> { // E: the following bytes are NDJSON again
                            flush()
                            raw = false
                            onPhase(false)
                        }
                        else -> throw IOException("Invalid terminal escape")
                    }
                } else if (b == 1) escaped = true else output.write(b)
            } else if (b == 10) {
                val text = line.toString("UTF-8").trim()
                line.reset()
                if (text.isNotEmpty() && onLine(text)) {
                    raw = true
                    onPhase(true)
                }
            } else {
                line.write(b)
                if (line.size() > 4096) throw IOException("Protocol line exceeds 4096 bytes")
            }
        }
        flush()
    }
}
