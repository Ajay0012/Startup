package com.pangu.companion

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

    fun state(callId: String): Int? = calls[callId]?.state

    fun ids(): List<String> = calls.keys().toList()
}
