var errorCharacterLimit = 220

function boundedText(value, limit) {
  var text = String(value || "").replace(/\s+/g, " ").trim()
  var maximum = Math.max(1, Number(limit || errorCharacterLimit))
  return text.length > maximum ? text.substring(0, maximum - 1) + "…" : text
}

function parseJson(raw) {
  try {
    return { ok: true, value: JSON.parse(String(raw || "")) }
  } catch (error) {
    return { ok: false, error: "Cider returned an unreadable response" }
  }
}

function parseResponse(raw) {
  var parsed = parseJson(raw)
  if (!parsed.ok || !parsed.value || typeof parsed.value !== "object") {
    return { ok: false, code: "invalid_response", error: parsed.error || "Cider request failed" }
  }

  var value = parsed.value
  if (value.ok === true) return { ok: true, data: value.data || {} }

  var source = value.error && typeof value.error === "object" ? value.error : {}
  return {
    ok: false,
    code: boundedText(source.code || "request_failed", 80),
    error: boundedText(source.message || "Cider request failed", errorCharacterLimit)
  }
}

function friendlyError(result) {
  var code = String(result && result.code || "")
  if (code === "missing_api_key") return "CIDER_API_KEY is not available to Omarchy Shell."
  if (code === "unauthorized") return "Cider rejected CIDER_API_KEY. Create or export a valid app token."
  if (code === "unavailable") return "Cider RPC is not reachable on localhost:10767."
  if (code === "invalid_rpc_url") return "CIDER_RPC_URL must point to Cider on this machine."
  return boundedText(result && result.error || "Cider request failed", errorCharacterLimit)
}

function numberInRange(value, fallback, minimum, maximum) {
  var number = Number(value)
  if (!isFinite(number)) number = fallback
  return Math.max(minimum, Math.min(maximum, number))
}

function trackWithPosition(track, position) {
  var source = track && typeof track === "object" ? track : {}
  var next = {}
  for (var key in source) next[key] = source[key]
  next.positionSec = numberInRange(position, 0, 0, Number(source.durationSec || 86400))
  return next
}

function formatTime(value) {
  var seconds = Math.max(0, Math.floor(Number(value || 0)))
  var minutes = Math.floor(seconds / 60)
  var remainder = seconds % 60
  return minutes + ":" + (remainder < 10 ? "0" : "") + remainder
}

function repeatLabel(mode) {
  if (Number(mode) === 1) return "Repeat one"
  if (Number(mode) === 2) return "Repeat all"
  return "Repeat off"
}

function repeatIcon(mode) {
  return Number(mode) === 1 ? "󰑘" : "󰑖"
}

function volumeIcon(volume) {
  var value = Number(volume || 0)
  if (value <= 0.001) return "󰖁"
  if (value < 0.5) return "󰕿"
  return "󰕾"
}

function audioBadge(track) {
  var traits = track && Array.isArray(track.audioTraits) ? track.audioTraits : []
  if (traits.indexOf("atmos") !== -1 || traits.indexOf("spatial") !== -1) return "DOLBY ATMOS"
  if (traits.indexOf("lossless") !== -1) return "LOSSLESS"
  return ""
}

function queueMeta(item) {
  if (!item) return ""
  var parts = []
  if (item.artist) parts.push(String(item.artist))
  if (item.album && String(item.album) !== String(item.title || "")) parts.push(String(item.album))
  return parts.join(" · ")
}

function validAction(name) {
  return [
    "play", "pause", "playPause", "next", "previous", "seek", "volume",
    "toggleShuffle", "toggleRepeat", "toggleAutoplay"
  ].indexOf(String(name || "")) !== -1
}

function actionCommand(helperPath, action) {
  if (!action || !validAction(action.name)) return []
  var command = ["python3", String(helperPath || ""), "action", String(action.name)]
  if (action.value !== undefined && action.value !== null && String(action.value) !== "")
    command.push(String(action.value))
  return command
}

if (typeof module !== "undefined") {
  module.exports = {
    boundedText: boundedText,
    parseJson: parseJson,
    parseResponse: parseResponse,
    friendlyError: friendlyError,
    numberInRange: numberInRange,
    trackWithPosition: trackWithPosition,
    formatTime: formatTime,
    repeatLabel: repeatLabel,
    repeatIcon: repeatIcon,
    volumeIcon: volumeIcon,
    audioBadge: audioBadge,
    queueMeta: queueMeta,
    validAction: validAction,
    actionCommand: actionCommand
  }
}
