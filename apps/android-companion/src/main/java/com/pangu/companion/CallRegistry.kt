package com.pangu.companion

import android.os.Build
import android.telecom.Call
import android.telecom.VideoProfile
import java.util.concurrent.ConcurrentHashMap

object CallRegistry {
    private val calls = ConcurrentHashMap<String, Call>()

    fun add(call: Call): String {
        val id = Integer.toHexString(System.identityHashCode(call))
        calls[id] = call
        return id
    }

    fun remove(call: Call) {
        val id = Integer.toHexString(System.identityHashCode(call))
        calls.remove(id)
    }

    fun answer(callId: String): Boolean {
        val call = calls[callId] ?: return false
        call.answer(VideoProfile.STATE_AUDIO_ONLY)
        return true
    }

    fun disconnect(callId: String): Boolean {
        val call = calls[callId] ?: return false
        call.disconnect()
        return true
    }

    fun state(callId: String): Int? {
        val call = calls[callId] ?: return null
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            call.details.state
        } else {
            legacyState(call)
        }
    }

    @Suppress("DEPRECATION")
    private fun legacyState(call: Call): Int = call.state

    fun ids(): List<String> = calls.keys().toList()
}
