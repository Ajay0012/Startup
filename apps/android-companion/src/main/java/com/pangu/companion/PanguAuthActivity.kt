package com.pangu.companion

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat

class PanguAuthActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        PhoneSecurityLease.revoke()

        val authenticators =
            BiometricManager.Authenticators.BIOMETRIC_STRONG or
                BiometricManager.Authenticators.DEVICE_CREDENTIAL
        val biometricManager = BiometricManager.from(this)
        if (biometricManager.canAuthenticate(authenticators) != BiometricManager.BIOMETRIC_SUCCESS) {
            setResult(RESULT_CANCELED)
            finish()
            return
        }

        val executor = ContextCompat.getMainExecutor(this)
        val prompt = BiometricPrompt(
            this,
            executor,
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    super.onAuthenticationSucceeded(result)
                    PhoneSecurityLease.grant()
                    setResult(RESULT_OK)
                    finish()
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                    super.onAuthenticationError(errorCode, errString)
                    PhoneSecurityLease.revoke()
                    setResult(RESULT_CANCELED)
                    finish()
                }
            },
        )

        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle("Authorize PANGU")
            .setSubtitle("Confirm your identity before PANGU controls protected phone actions")
            .setAllowedAuthenticators(authenticators)
            .setConfirmationRequired(true)
            .build()
        prompt.authenticate(info)
    }
}
