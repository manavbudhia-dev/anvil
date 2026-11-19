// Anvil client for Android apps (Kotlin). Copy this file into your app and change the package.
//
// Gradle (app/build.gradle.kts):
//   implementation("com.squareup.okhttp3:okhttp:4.12.0")
//   implementation("com.squareup.okhttp3:okhttp-sse:4.12.0")
//   implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
// AndroidManifest.xml: <uses-permission android:name="android.permission.INTERNET" />
//
// org.json ships with Android, so no JSON library is needed.

package com.example.anvil

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.retryWhen
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.URLDecoder
import java.util.concurrent.TimeUnit

/** An HTTP error from Anvil. status 401 means the token is wrong or was rotated: pair again. */
class AnvilException(val status: Int, message: String) : IOException(message)

/**
 * One live event from Anvil. Common types:
 *  turn_start, text_start, text_delta(text), text_end, thinking_delta(text),
 *  tool_start(name, label, args), tool_result(name, label, result, isError),
 *  confirm(reqId, name, label, preview, dangers)  -> answer with approve(),
 *  confirm_done(reqId), notice(text), error(text), interrupted, turn_end(meta).
 */
data class AnvilEvent(val type: String, val sessionId: String, val json: JSONObject) {
    val text: String get() = json.optString("text")
    val reqId: String get() = json.optString("req_id")
    val toolName: String get() = json.optString("name")
    val label: String get() = json.optString("label")
    val preview: String get() = json.optString("preview")
    val result: String get() = json.optString("result")
    val isError: Boolean get() = json.optBoolean("is_error")
    /** Non-empty for destructive operations: show these and require an explicit "Allow once". */
    val dangers: List<String>
        get() = json.optJSONArray("dangers")?.let { a -> List(a.length()) { a.getString(it) } } ?: emptyList()
    val args: JSONObject get() = json.optJSONObject("args") ?: JSONObject()
}

data class AnvilSession(val id: String, val title: String, val projectFolder: String, val running: Boolean)

/** A transcript item from session(): kind is user | assistant | thinking | tool. */
data class AnvilItem(val kind: String, val text: String, val toolName: String, val label: String,
                     val result: String?, val isError: Boolean)

class AnvilClient(baseUrl: String, private val token: String, http: OkHttpClient = OkHttpClient()) {

    companion object {
        /** Build a client from the link in Anvil's QR code, e.g. https://myserver.tail1234.ts.net/#token=... */
        fun fromConnectLink(link: String): AnvilClient {
            val hashIndex = link.indexOf('#')
            require(hashIndex > 0) { "not an Anvil connect link (no #token=)" }
            val token = link.substring(hashIndex + 1).split('&')
                .firstOrNull { it.startsWith("token=") }?.removePrefix("token=")
                ?.let { URLDecoder.decode(it, "UTF-8") }
            require(!token.isNullOrBlank()) { "the link has no token" }
            return AnvilClient(link.substring(0, hashIndex), token)
        }
    }

    private val base = baseUrl.trimEnd('/')
    private val http = http.newBuilder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)  // compaction can take a while
        .build()
    private val streamHttp = this.http.newBuilder()
        .readTimeout(0, TimeUnit.SECONDS)    // the event stream stays open; the server pings every 15 s
        .build()
    private val jsonType = "application/json".toMediaType()

    // --- requests ---------------------------------------------------------------

    /** Settings (provider, model, key_set...), saved sessions and the default project folder. */
    suspend fun state(): JSONObject = call("GET", "/api/state")

    suspend fun sessions(): List<AnvilSession> {
        val arr = state().getJSONArray("sessions")
        return List(arr.length()) { i ->
            arr.getJSONObject(i).let {
                AnvilSession(it.getString("id"), it.optString("title"), it.optString("root"), it.optBoolean("running"))
            }
        }
    }

    /** Starts a session in a folder on the server (null = the default folder). Returns its id. */
    suspend fun newSession(projectFolder: String? = null): String {
        val body = JSONObject().apply { if (projectFolder != null) put("root", projectFolder) }
        return call("POST", "/api/sessions", body).getJSONObject("meta").getString("id")
    }

    /** A session's metadata (title, mode, cost...) and its transcript so far. */
    suspend fun session(id: String): Pair<JSONObject, List<AnvilItem>> {
        val d = call("GET", "/api/sessions/$id")
        val arr = d.getJSONArray("items")
        val items = List(arr.length()) { i ->
            arr.getJSONObject(i).let {
                AnvilItem(it.getString("kind"), it.optString("text"), it.optString("name"), it.optString("label"),
                          if (it.isNull("result")) null else it.optString("result"), it.optBoolean("is_error"))
            }
        }
        return d.getJSONObject("meta") to items
    }

    /** Sends a message (or "/skill-name args"). Results arrive through events(). */
    suspend fun send(sessionId: String, text: String) {
        call("POST", "/api/sessions/$sessionId/send", JSONObject().put("text", text))
    }

    /** Answers a confirm event: "yes", "always" (not allowed for destructive ones), "no", or instructions. */
    suspend fun approve(sessionId: String, reqId: String, decision: String) {
        call("POST", "/api/sessions/$sessionId/approve", JSONObject().put("req_id", reqId).put("decision", decision))
    }

    /** Stops the current turn. */
    suspend fun stop(sessionId: String) {
        call("POST", "/api/sessions/$sessionId/stop")
    }

    /** "ask", "edits" or "auto". Refused (status 400) above the server's permission limit. */
    suspend fun setMode(sessionId: String, mode: String) {
        call("POST", "/api/sessions/$sessionId/mode", JSONObject().put("mode", mode))
    }

    suspend fun skills(sessionId: String? = null): JSONArray =
        call("GET", "/api/skills" + (sessionId?.let { "?session=$it" } ?: "")).getJSONArray("skills")

    // --- live events -------------------------------------------------------------

    /**
     * Live events for every session (filter by sessionId). Reconnects automatically after network
     * drops; ends with AnvilException(401) if the token is no longer valid.
     */
    fun events(): Flow<AnvilEvent> = callbackFlow {
        val request = Request.Builder().url("$base/api/events").header("X-Anvil-Token", token).build()
        val source = EventSources.createFactory(streamHttp).newEventSource(request, object : EventSourceListener() {
            override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                val json = try { JSONObject(data) } catch (e: Exception) { return }
                trySend(AnvilEvent(json.optString("type"), json.optString("session"), json))
            }
            override fun onClosed(eventSource: EventSource) {
                close(IOException("event stream closed"))
            }
            override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) {
                val code = response?.code
                close(if (code != null && code >= 400) AnvilException(code, "event stream refused: HTTP $code")
                      else (t ?: IOException("event stream failed")))
            }
        })
        awaitClose { source.cancel() }
    }.retryWhen { cause, attempt ->
        if (cause is AnvilException && cause.status in 400..499) return@retryWhen false
        delay(minOf(1000L shl minOf(attempt.toInt(), 4), 15_000L))
        true
    }

    // --- plumbing -------------------------------------------------------------------

    private suspend fun call(method: String, path: String, body: JSONObject? = null): JSONObject =
        withContext(Dispatchers.IO) {
            val url = (base + path).toHttpUrl()
            val reqBody = when {
                body != null -> body.toString().toRequestBody(jsonType)
                method == "POST" -> "{}".toRequestBody(jsonType)
                else -> null
            }
            val request = Request.Builder().url(url).header("X-Anvil-Token", token).method(method, reqBody).build()
            http.newCall(request).execute().use { resp ->
                val text = resp.body?.string().orEmpty()
                val json = try { JSONObject(text.ifBlank { "{}" }) } catch (e: Exception) { JSONObject() }
                if (!resp.isSuccessful) throw AnvilException(resp.code, json.optString("error", "HTTP ${resp.code}"))
                json
            }
        }
}
