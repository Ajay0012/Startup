package com.pangu.companion

import android.os.SystemClock

object PhoneSecurityLease {
    @Volatile
    private var authenticatedUntilElapsedMs: Long = 0L

    fun grant(durationMs: Long = 120_000L) {
        require(durationMs in 15_000L..600_000L)
        authenticatedUntilElapsedMs = SystemClock.elapsedRealtime() + durationMs
    }

    fun revoke() {
        authenticatedUntilElapsedMs = 0L
    }

    fun isFresh(): Boolean = SystemClock.elapsedRealtime() <= authenticatedUntilElapsedMs
}
