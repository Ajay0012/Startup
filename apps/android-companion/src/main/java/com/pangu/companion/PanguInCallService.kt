package com.pangu.companion

import android.telecom.Call
import android.telecom.InCallService

class PanguInCallService : InCallService() {
    override fun onCallAdded(call: Call) {
        super.onCallAdded(call)
        CallRegistry.add(call)
    }

    override fun onCallRemoved(call: Call) {
        CallRegistry.remove(call)
        super.onCallRemoved(call)
    }
}
