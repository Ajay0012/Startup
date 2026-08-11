package com.pangu.companion

import android.app.role.RoleManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity

class PanguDialerActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val roleManager = getSystemService(Context.ROLE_SERVICE) as RoleManager
        if (!roleManager.isRoleHeld(RoleManager.ROLE_DIALER)) {
            startActivityForResult(
                roleManager.createRequestRoleIntent(RoleManager.ROLE_DIALER),
                REQUEST_DIALER_ROLE,
            )
        }
    }

    companion object {
        private const val REQUEST_DIALER_ROLE = 1201
    }
}
