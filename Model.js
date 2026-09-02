var errorCharacterLimit = 220
var helperOutputCharacterLimit = 65536
var trackStringLimit = 512
var idStringLimit = 256
var typeStringLimit = 32
var maxAudioTraits = 8
var maxQueueItems = 20

function boundedText(value, limit) {
  var source = typeof value === "string" || typeof value === "number" ? value : ""
  var text = String(source).replace(/\s+/g, " ").trim()
  var maximum = Math.max(1, Number(limit || errorCharacterLimit))
  return text.length > maximum ? text.substring(0, maximum - 3) + "..." : text
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function parseJson(raw) {
  var text = String(raw || "")
  if (text.length > helperOutputCharacterLimit)
    return { ok: false, error: "Cider helper output exceeded its limit" }
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch (error) {
    return { ok: false, error: "Cider returned an unreadable response" }
  }
}

function parseResponse(raw) {
  var parsed = parseJson(raw)
  if (!parsed.ok || !isRecord(parsed.value)) {
    return { ok: false, code: "invalid_response", error: parsed.error || "Cider request failed" }
  }

  var value = parsed.value
  if (value.ok === true) {
    if (!isRecord(value.data))
      return { ok: false, code: "invalid_response", error: "Cider returned an invalid response schema" }
    return { ok: true, data: value.data }
  }

  var source = isRecord(value.error) ? value.error : {}
  return {
    ok: false,
    code: boundedText(source.code || "request_failed", 80),
    error: boundedText(source.message || "Cider request failed", errorCharacterLimit)
  }
}

function invalidSchema() {
  return { ok: false, code: "invalid_response", error: "Cider returned an invalid response schema" }
}

function stringField(value, limit) {
  if (typeof value !== "string") return ""
  return value.replace(/[\u0000\r\n]/g, " ").substring(0, limit)
}

function finiteNumber(value, fallback, minimum, maximum) {
  if (typeof value !== "number" || !isFinite(value)) return fallback
  return Math.max(minimum, Math.min(maximum, value))
}

function integerField(value, fallback, minimum, maximum) {
  return Math.round(finiteNumber(value, fallback, minimum, maximum))
}

function artworkSource(value, cacheRoot) {
  if (typeof value !== "string" || typeof cacheRoot !== "string" || cacheRoot === "") return ""
  var prefix = cacheRoot.replace(/\/$/, "") + "/"
  if (value.indexOf(prefix) !== 0) return ""
  var name = value.substring(prefix.length)
  if (!/^[0-9a-f]{64}\.png$/.test(name)) return ""
  var parts = value.split("/")
  for (var index = 0; index < parts.length; index++) parts[index] = encodeURIComponent(parts[index])
  return "file://" + parts.join("/")
}

function sanitizeTraits(value) {
  if (!Array.isArray(value)) return []
  var traits = []
  for (var index = 0; index < value.length && index < maxAudioTraits; index++) {
    var trait = stringField(value[index], typeStringLimit)
    if (trait !== "") traits.push(trait)
  }
  return traits
}

function sanitizeTrack(value, cacheRoot) {
  if (value === null) return null
  if (!isRecord(value)) return undefined
  return {
    id: stringField(value.id, idStringLimit),
    type: stringField(value.type, typeStringLimit) || "song",
    title: stringField(value.title, trackStringLimit),
    artist: stringField(value.artist, trackStringLimit),
    album: stringField(value.album, trackStringLimit),
    artSource: artworkSource(value.artPath, cacheRoot),
    durationSec: finiteNumber(value.durationSec, 0, 0, 86400),
    positionSec: finiteNumber(value.positionSec, 0, 0, 86400),
    inLibrary: value.inLibrary === true,
    inFavorites: value.inFavorites === true,
    audioTraits: sanitizeTraits(value.audioTraits)
  }
}

function parseStatusResponse(raw, cacheRoot) {
  var result = parseResponse(raw)
  if (!result.ok) return result
  var data = result.data
  if (typeof data.connected !== "boolean" || typeof data.playing !== "boolean"
      || typeof data.autoplay !== "boolean") return invalidSchema()
  var track = sanitizeTrack(data.track, cacheRoot)
  if (track === undefined) return invalidSchema()
  if (track && track.durationSec > 0) track.positionSec = Math.min(track.positionSec, track.durationSec)
  return {
    ok: true,
    data: {
      connected: data.connected,
      playing: data.playing,
      track: track,
      volume: finiteNumber(data.volume, 0, 0, 1),
      shuffleMode: integerField(data.shuffleMode, 0, 0, 1),
      repeatMode: integerField(data.repeatMode, 0, 0, 2),
      autoplay: data.autoplay,
      fetchedAtMs: finiteNumber(data.fetchedAtMs, Date.now(), 0, 9007199254740991)
    }
  }
}

function sanitizeQueueItem(value, cacheRoot) {
  if (!isRecord(value)) return null
  var queueIndex = integerField(value.queueIndex, -1, -1, 1999)
  var skipCount = integerField(value.skipCount, -1, -1, 20)
  if (queueIndex < 0 || skipCount < 1) return null
  var title = stringField(value.title, trackStringLimit)
  var id = stringField(value.id, idStringLimit)
  if (title === "" && id === "") return null
  return {
    id: id,
    type: stringField(value.type, typeStringLimit) || "song",
    queueIndex: queueIndex,
    skipCount: skipCount,
    title: title || "Unknown track",
    artist: stringField(value.artist, trackStringLimit),
    album: stringField(value.album, trackStringLimit),
    artSource: artworkSource(value.artPath, cacheRoot),
    durationSec: finiteNumber(value.durationSec, 0, 0, 86400)
  }
}

function parseQueueResponse(raw, cacheRoot) {
  var result = parseResponse(raw)
  if (!result.ok) return result
  if (!Array.isArray(result.data.upNext)) return invalidSchema()
  var upNext = []
  for (var index = 0; index < result.data.upNext.length && index < maxQueueItems; index++) {
    var item = sanitizeQueueItem(result.data.upNext[index], cacheRoot)
    if (item) upNext.push(item)
  }
  return { ok: true, data: { upNext: upNext } }
}

function parseActionResponse(raw) {
  var result = parseResponse(raw)
  if (!result.ok) return result
  if (!validAction(result.data.action)) return invalidSchema()
  return { ok: true, data: { action: String(result.data.action) } }
}

function friendlyError(result) {
  var code = String(result && result.code || "")
  if (code === "missing_api_key") return "Cider API key is not configured."
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
  var source = isRecord(track) ? track : {}
  var next = {}
  for (var key in source) next[key] = source[key]
  next.positionSec = numberInRange(position, 0, 0, Number(source.durationSec || 86400))
  return next
}

function formatTime(value) {
  var seconds = Math.max(0, Math.min(86400, Math.floor(Number(value || 0))))
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
    "toggleShuffle", "toggleRepeat", "toggleAutoplay", "queueMove",
    "queueRemove", "skipTo"
  ].indexOf(String(name || "")) !== -1
}

function actionCommand(helperPath, action) {
  if (!action || !validAction(action.name)) return []
  var command = ["python3", String(helperPath || ""), "action", String(action.name)]
  var values = Array.isArray(action.value) ? action.value : [action.value]
  for (var index = 0; index < values.length && index < 2; index++) {
    var value = values[index]
    if (value !== undefined && value !== null && String(value) !== "") command.push(String(value).substring(0, 32))
  }
  return command
}

if (typeof module !== "undefined") {
  module.exports = {
    boundedText: boundedText,
    parseJson: parseJson,
    parseResponse: parseResponse,
    parseStatusResponse: parseStatusResponse,
    parseQueueResponse: parseQueueResponse,
    parseActionResponse: parseActionResponse,
    artworkSource: artworkSource,
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
