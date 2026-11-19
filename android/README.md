# Connecting your Android app to Anvil

Your app talks to the Anvil web app running on your server, over Tailscale. Messages you type in the app run as Anvil tasks on the server; approvals ("Allow this command?") come back to the app.

## 1. One-time setup

**On the server**, as the user Anvil runs as (e.g. `anvil`):

```bash
sudo tailscale up                                   # once, if Tailscale isn't connected yet
sudo tailscale set --operator=anvil                 # lets this user publish the app on Tailscale
anvil-desktop --tailscale --install-service -C ~/your-project
sudo loginctl enable-linger anvil                   # keep it running after logout and reboots
```

This serves Anvil at `https://<server>.<tailnet>.ts.net/` (only devices in your Tailscale network can reach it, with a real HTTPS certificate), installs a background service, and prints a QR code. Print the QR again any time with `anvil-desktop --qr`. If the QR leaks, run `anvil-desktop --rotate-token` and pair again.

In the Tailscale admin console, turn on **MagicDNS** and **HTTPS certificates** (DNS page) if `--tailscale` asks for it.

**On the phone:** install the Tailscale app and log in with the same account.

The QR code holds a link like `https://myserver.tail1234.ts.net/#token=...`. Your app needs that link: the address and the token.

## 2a. Quickest: embed Anvil's own screen (WebView)

Anvil's web app already works on phones (approvals, live output, skills, sessions). Show it in a WebView:

```kotlin
@SuppressLint("SetJavaScriptEnabled")
fun showAnvil(webView: WebView, connectLink: String) {
    webView.settings.javaScriptEnabled = true
    webView.settings.domStorageEnabled = true      // Anvil remembers the token in localStorage
    webView.webViewClient = object : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val sameSite = request.url.host == Uri.parse(connectLink).host
            if (!sameSite) view.context.startActivity(Intent(Intent.ACTION_VIEW, request.url))
            return !sameSite                        // open other links in the browser
        }
    }
    webView.loadUrl(connectLink)                   // after the first load, loading the base URL is enough
}
```

Even simpler without code: open the link in Chrome on the phone and use **Add to Home screen**.

## 2b. Native: use the API from your own UI

Copy `AnvilClient.kt` into your app (change its `package` line) and add:

```kotlin
// app/build.gradle.kts
implementation("com.squareup.okhttp3:okhttp:4.12.0")
implementation("com.squareup.okhttp3:okhttp-sse:4.12.0")
implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
```

```xml
<!-- AndroidManifest.xml -->
<uses-permission android:name="android.permission.INTERNET" />
```

A minimal ViewModel:

```kotlin
class AnvilViewModel(link: String) : ViewModel() {
    private val anvil = AnvilClient.fromConnectLink(link)
    private var sessionId: String? = null

    val output = MutableStateFlow("")                       // streamed reply text
    val pendingApproval = MutableStateFlow<AnvilEvent?>(null)
    val error = MutableStateFlow<String?>(null)

    init {
        viewModelScope.launch {
            try {
                anvil.events().collect { ev ->
                    if (ev.sessionId != sessionId) return@collect
                    when (ev.type) {
                        "text_delta" -> output.value += ev.text
                        "tool_start" -> output.value += "\n▸ ${ev.toolName} ${ev.label}\n"
                        "tool_result" -> if (ev.isError) output.value += "  ✗ ${ev.result.lineSequence().first()}\n"
                        "confirm" -> pendingApproval.value = ev
                        "confirm_done" -> if (pendingApproval.value?.reqId == ev.reqId) pendingApproval.value = null
                        "error", "notice" -> output.value += "\n${ev.text}\n"
                    }
                }
            } catch (e: AnvilException) {
                error.value = if (e.status == 401) "Pair again: the token changed" else e.message
            }
        }
    }

    fun send(text: String) = viewModelScope.launch {
        try {
            val id = sessionId ?: anvil.newSession().also { sessionId = it }
            output.value += "\n> $text\n"
            anvil.send(id, text)
        } catch (e: Exception) { error.value = e.message }
    }

    fun answer(decision: String) = viewModelScope.launch {
        val ev = pendingApproval.value ?: return@launch
        anvil.approve(ev.sessionId, ev.reqId, decision)    // "yes", "always", "no" or instructions
        pendingApproval.value = null
    }

    fun stop() = viewModelScope.launch { sessionId?.let { anvil.stop(it) } }
}
```

And the approval dialog in Compose. Destructive operations come with `dangers`; for those, offer only "Allow once" and "Deny":

```kotlin
val ev by vm.pendingApproval.collectAsState()
ev?.let { e ->
    AlertDialog(
        onDismissRequest = {},
        title = { Text(if (e.dangers.isNotEmpty()) "⚠ Destructive: ${e.toolName}" else "Allow ${e.toolName}?") },
        text = {
            Column {
                Text(if (e.toolName == "bash") "$ ${e.args.optString("command")}" else e.preview,
                     fontFamily = FontFamily.Monospace, maxLines = 20)
                e.dangers.forEach { Text("• $it", color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = { TextButton(onClick = { vm.answer("yes") }) { Text(if (e.dangers.isEmpty()) "Allow" else "Allow once") } },
        dismissButton = { TextButton(onClick = { vm.answer("no") }) { Text("Deny") } },
    )
}
```

### Pairing by scanning the QR code

Google's code scanner needs no camera permission:

```kotlin
// implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
GmsBarcodeScanning.getClient(context).startScan()
    .addOnSuccessListener { barcode ->
        val link = barcode.rawValue ?: return@addOnSuccessListener
        AnvilClient.fromConnectLink(link)   // throws if it isn't an Anvil link
        saveLink(link)                       // store it, e.g. in EncryptedSharedPreferences
    }
```

The link contains the token, which can control Anvil on your server: store it encrypted, and don't log it.

## API reference

All requests need the header `X-Anvil-Token: <token>`. Bodies are JSON.

| Method and path | Body | Returns |
|---|---|---|
| `GET /api/state` | | `config` (model, provider, key_set...), `sessions`, `default_root` |
| `POST /api/sessions` | `{"root": "/path"}` (optional) | `{"meta": {"id", "title", "root", "mode", "ceiling", ...}}` |
| `GET /api/sessions/{id}` | | `meta` and `items` (transcript: kind user/assistant/thinking/tool) |
| `POST /api/sessions/{id}/send` | `{"text": "..."}` | starts a turn; progress arrives as events |
| `POST /api/sessions/{id}/approve` | `{"req_id": "...", "decision": "yes" \| "always" \| "no" \| "instructions"}` | |
| `POST /api/sessions/{id}/stop` | | stops the current turn |
| `POST /api/sessions/{id}/mode` | `{"mode": "ask" \| "edits" \| "auto"}` | 400 if above the server's permission limit |
| `POST /api/sessions/{id}/compact` | | summarizes the conversation |
| `DELETE /api/sessions/{id}` | | deletes a session |
| `GET /api/skills?session={id}` | | `skills`: name, description, source |
| `GET /api/events` | | Server-sent events, one JSON object per `data:` line, for all sessions |

Event objects have `session` and `type`, plus: `text` (text_delta, thinking_delta, notice, error), `name`/`label`/`args` (tool_start, tool_result, confirm), `result`/`is_error` (tool_result), `req_id`/`preview`/`dangers` (confirm, confirm_done), `meta` (turn_end).

Errors return `{"error": "..."}` with status 400, 401 (bad token), 404 or 502.
