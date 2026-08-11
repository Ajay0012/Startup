package com.pangu.companion

import android.Manifest
import android.app.role.RoleManager
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.telecom.TelecomManager
import android.telephony.TelephonyManager
import androidx.core.content.ContextCompat

sealed class PhoneCommandResult {
    data class Success(val message: String) : PhoneCommandResult()
    data class Denied(val code: String) : PhoneCommandResult()
    data class Failed(val code: String) : PhoneCommandResult()
}

class PanguCommandDispatcher(private val context: Context) {
    private val roleManager = context.getSystemService(Context.ROLE_SERVICE) as RoleManager
    private val telecom = context.getSystemService(Context.TELECOM_SERVICE) as TelecomManager
    private val telephony = context.getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager

    private fun privilegedReady(): Boolean =
        roleManager.isRoleHeld(RoleManager.ROLE_DIALER) && PhoneSecurityLease.isFresh()

    fun placeCall(number: String): PhoneCommandResult {
        val clean = number.trim()
        if (clean.isEmpty() || clean.length > 80) {
            return PhoneCommandResult.Denied("INVALID_NUMBER")
        }
        if (!privilegedReady()) {
            return PhoneCommandResult.Denied("FRESH_DEVICE_AUTH_AND_DIALER_ROLE_REQUIRED")
        }
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CALL_PHONE) != PackageManager.PERMISSION_GRANTED) {
            return PhoneCommandResult.Denied("CALL_PHONE_PERMISSION_REQUIRED")
        }
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q && telephony.isEmergencyNumber(clean)) {
            return PhoneCommandResult.Denied("AUTONOMOUS_EMERGENCY_CALL_BLOCKED")
        }
        return try {
            telecom.placeCall(Uri.fromParts("tel", clean, null), Bundle())
            PhoneCommandResult.Success("Call placement requested through Android Telecom.")
        } catch (error: SecurityException) {
            PhoneCommandResult.Failed("TELECOM_PERMISSION_FAILURE")
        }
    }

    fun answerCall(callId: String): PhoneCommandResult {
        if (!privilegedReady()) {
            return PhoneCommandResult.Denied("FRESH_DEVICE_AUTH_AND_DIALER_ROLE_REQUIRED")
        }
        return if (CallRegistry.answer(callId)) {
            PhoneCommandResult.Success("Incoming call answer requested.")
        } else {
            PhoneCommandResult.Failed("CALL_NOT_FOUND")
        }
    }

    fun endCall(callId: String): PhoneCommandResult {
        if (!privilegedReady()) {
            return PhoneCommandResult.Denied("FRESH_DEVICE_AUTH_AND_DIALER_ROLE_REQUIRED")
        }
        return if (CallRegistry.disconnect(callId)) {
            PhoneCommandResult.Success("Call disconnect requested.")
        } else {
            PhoneCommandResult.Failed("CALL_NOT_FOUND")
        }
    }

    fun speakOnCarrierCall(text: String): PhoneCommandResult {
        if (text.isBlank()) {
            return PhoneCommandResult.Denied("EMPTY_SPEECH")
        }
        return PhoneCommandResult.Denied("CARRIER_CALL_MEDIA_NOT_EXPOSED")
    }
}
