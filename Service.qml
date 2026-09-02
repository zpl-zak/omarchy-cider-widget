import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

Item {
  id: root

  property var shell: null
  property var settings: ({})
  property bool active: true
  property bool probed: false
  property bool configured: true
  property bool connected: false
  property bool refreshing: false
  property bool refreshPending: false
  property bool playing: false
  property var track: null
  property real volume: 0
  property int shuffleMode: 0
  property int repeatMode: 0
  property bool autoplay: false
  property double fetchedAtMs: 0
  property date lastUpdated: new Date(0)
  property var upNext: []
  property bool queueRefreshing: false
  property bool queuePending: false
  property int queueWatchers: 0
  property string lastError: ""
  property string actionError: ""

  property var _actions: []
  property var _currentAction: null
  property string _statusOutput: ""
  property string _statusError: ""
  property string _queueOutput: ""
  property string _queueError: ""
  property string _actionOutput: ""
  property string _actionErrorOutput: ""
  property string _statusFailure: ""
  property string _queueFailure: ""
  property string _actionFailure: ""

  readonly property string helperPath: decodeURIComponent(
    Qt.resolvedUrl("cider-rpc.py").toString().replace(/^file:\/\//, ""))
  readonly property string artworkCacheRoot: {
    var configured = String(Quickshell.env("XDG_CACHE_HOME") || "")
    var home = String(Quickshell.env("HOME") || "")
    var base = configured.length > 0 && configured.charAt(0) === "/" ? configured : home + "/.cache"
    return base + "/omarchy-cider-widget/artwork"
  }
  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 2, 1, 30)
  readonly property int queueLimit: intSetting("queueLimit", 8, 3, 20)
  readonly property int helperOutputLimit: 65536
  readonly property int helperErrorLimit: 4096
  readonly property int helperTimeoutMs: 9000
  readonly property int helperKillGraceMs: 750
  readonly property int pendingActionLimit: 32
  readonly property bool hasTrack: track !== null && (track.title || track.artist)
  readonly property bool actionRunning: actionProcess.running || _actions.length > 0

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, minimum, maximum) {
    var value = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(value)) value = fallback
    return Math.max(minimum, Math.min(maximum, value))
  }

  function estimatedPosition(nowMs) {
    if (!track) return 0
    var position = Number(track.positionSec || 0)
    if (playing && fetchedAtMs > 0) position += Math.max(0, Number(nowMs || Date.now()) - fetchedAtMs) / 1000
    var duration = Number(track.durationSec || 0)
    return duration > 0 ? Math.min(duration, Math.max(0, position)) : Math.max(0, position)
  }

  function clearPlayback() {
    playing = false
    track = null
    upNext = []
    fetchedAtMs = 0
  }

  function refresh() {
    if (!active) return
    if (statusProcess.running) {
      refreshPending = true
      return
    }
    refreshing = true
    _statusOutput = ""
    _statusError = ""
    _statusFailure = ""
    statusProcess.command = ["python3", helperPath, "status"]
    statusWatchdog.restart()
    statusProcess.running = true
  }

  function applyStatus(raw) {
    var result = Model.parseStatusResponse(raw, artworkCacheRoot)
    probed = true
    refreshing = false
    if (!result.ok) {
      configured = result.code !== "missing_api_key"
      connected = false
      clearPlayback()
      lastError = Model.friendlyError(result)
      finishRefresh()
      return
    }

    var data = result.data || {}
    configured = true
    connected = data.connected === true
    playing = data.playing === true
    track = data.track || null
    volume = Model.numberInRange(data.volume, 0, 0, 1)
    shuffleMode = Math.round(Model.numberInRange(data.shuffleMode, 0, 0, 1))
    repeatMode = Math.round(Model.numberInRange(data.repeatMode, 0, 0, 2))
    autoplay = data.autoplay === true
    fetchedAtMs = Number(data.fetchedAtMs || Date.now())
    lastUpdated = new Date()
    lastError = ""
    finishRefresh()
  }

  function finishRefresh() {
    if (!refreshPending) return
    refreshPending = false
    Qt.callLater(root.refresh)
  }

  function refreshQueue() {
    if (!active || !configured || !connected) return
    if (queueProcess.running) {
      queuePending = true
      return
    }
    queueRefreshing = true
    _queueOutput = ""
    _queueError = ""
    _queueFailure = ""
    queueProcess.command = ["python3", helperPath, "queue", String(queueLimit)]
    queueWatchdog.restart()
    queueProcess.running = true
  }

  function applyQueue(raw) {
    var result = Model.parseQueueResponse(raw, artworkCacheRoot)
    queueRefreshing = false
    if (!result.ok) {
      actionError = Model.friendlyError(result)
      finishQueueRefresh()
      return
    }
    upNext = result.data.upNext
    actionError = ""
    finishQueueRefresh()
  }

  function finishQueueRefresh() {
    if (!queuePending) return
    queuePending = false
    Qt.callLater(root.refreshQueue)
  }

  function beginQueueWatch() {
    queueWatchers += 1
    refreshQueue()
  }

  function endQueueWatch() {
    queueWatchers = Math.max(0, queueWatchers - 1)
  }

  function applyOptimisticAction(name, value) {
    if (name === "playPause") playing = !playing
    else if (name === "play") playing = true
    else if (name === "pause") playing = false
    else if (name === "volume") volume = Model.numberInRange(value, volume, 0, 1)
    else if (name === "seek" && track) {
      track = Model.trackWithPosition(track, value)
      fetchedAtMs = Date.now()
    } else if (name === "toggleShuffle") shuffleMode = shuffleMode === 0 ? 1 : 0
    else if (name === "toggleRepeat") repeatMode = (repeatMode + 1) % 3
    else if (name === "toggleAutoplay") autoplay = !autoplay
    else if (name === "skipTo") playing = true
  }

  function runAction(name, value) {
    if (!active || !configured || !connected || !Model.validAction(name)) return false

    var next = []
    for (var i = 0; i < _actions.length; i++) {
      var queued = _actions[i]
      if ((name === "volume" || name === "seek") && queued.name === name) continue
      next.push(queued)
    }
    if (next.length >= pendingActionLimit) {
      actionError = "Too many Cider actions are already queued"
      return false
    }
    applyOptimisticAction(name, value)
    next.push({ name: name, value: value })
    _actions = next
    startNextAction()
    return true
  }

  function startNextAction() {
    if (actionProcess.running || _actions.length === 0) return
    var next = _actions.slice()
    _currentAction = next.shift()
    _actions = next
    _actionOutput = ""
    _actionErrorOutput = ""
    _actionFailure = ""
    actionProcess.command = Model.actionCommand(helperPath, _currentAction)
    actionWatchdog.restart()
    actionProcess.running = true
  }

  function finishAction(raw) {
    var action = _currentAction
    _currentAction = null
    var result = Model.parseActionResponse(raw)
    actionError = result.ok ? "" : Model.friendlyError(result)
    if (!result.ok) lastError = actionError
    refreshSoon.restart()
    if (action && (action.name === "queueMove" || action.name === "queueRemove")) refreshQueue()
    else if (action && ["next", "previous", "skipTo"].indexOf(action.name) !== -1) queueSoon.restart()
    Qt.callLater(root.startNextAction)
  }

  function helperProcess(kind) {
    if (kind === "status") return statusProcess
    if (kind === "queue") return queueProcess
    return actionProcess
  }

  function helperWatchdog(kind) {
    if (kind === "status") return statusWatchdog
    if (kind === "queue") return queueWatchdog
    return actionWatchdog
  }

  function helperKillTimer(kind) {
    if (kind === "status") return statusKillTimer
    if (kind === "queue") return queueKillTimer
    return actionKillTimer
  }

  function helperOutput(kind, isError) {
    if (kind === "status") return isError ? _statusError : _statusOutput
    if (kind === "queue") return isError ? _queueError : _queueOutput
    return isError ? _actionErrorOutput : _actionOutput
  }

  function setHelperOutput(kind, isError, value) {
    if (kind === "status") {
      if (isError) _statusError = value
      else _statusOutput = value
    } else if (kind === "queue") {
      if (isError) _queueError = value
      else _queueOutput = value
    } else {
      if (isError) _actionErrorOutput = value
      else _actionOutput = value
    }
  }

  function helperFailure(kind) {
    if (kind === "status") return _statusFailure
    if (kind === "queue") return _queueFailure
    return _actionFailure
  }

  function setHelperFailure(kind, value) {
    if (kind === "status") _statusFailure = value
    else if (kind === "queue") _queueFailure = value
    else _actionFailure = value
  }

  function appendHelperOutput(kind, isError, data) {
    if (helperFailure(kind) !== "") return
    var current = helperOutput(kind, isError)
    var limit = isError ? helperErrorLimit : helperOutputLimit
    var chunk = String(data || "")
    var room = Math.max(0, limit - current.length)
    if (chunk.length <= room) {
      setHelperOutput(kind, isError, current + chunk)
      return
    }
    setHelperOutput(kind, isError, current + chunk.substring(0, room))
    abortHelper(kind, "Cider " + kind + " helper exceeded its output limit")
  }

  function abortHelper(kind, message) {
    if (helperFailure(kind) !== "") return
    setHelperFailure(kind, message)
    helperWatchdog(kind).stop()
    var process = helperProcess(kind)
    if (process.running) {
      process.signal(15)
      helperKillTimer(kind).restart()
    }
  }

  function helperExited(kind, fallback) {
    helperWatchdog(kind).stop()
    helperKillTimer(kind).stop()
    var failure = helperFailure(kind)
    var raw = helperOutput(kind, false)
    if (failure !== "") {
      raw = JSON.stringify({ ok: false, error: { code: "deadline_exceeded", message: failure } })
    } else if (raw === "") {
      raw = JSON.stringify({
        ok: false,
        error: { code: "request_failed", message: helperOutput(kind, true) || fallback }
      })
    }
    if (kind === "status") applyStatus(raw)
    else if (kind === "queue") applyQueue(raw)
    else finishAction(raw)
  }

  function terminateAllHelpers() {
    var kinds = ["status", "queue", "action"]
    for (var index = 0; index < kinds.length; index++) {
      var process = helperProcess(kinds[index])
      if (process.running) process.signal(15)
    }
  }

  Component.onDestruction: terminateAllHelpers()

  Timer {
    id: statusTimer
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: root.active
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    interval: 5000
    repeat: true
    running: root.active && root.queueWatchers > 0
    onTriggered: root.refreshQueue()
  }

  Timer {
    id: refreshSoon
    interval: 180
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    id: queueSoon
    interval: 260
    repeat: false
    onTriggered: root.refreshQueue()
  }

  Timer {
    id: statusWatchdog
    interval: root.helperTimeoutMs
    repeat: false
    onTriggered: root.abortHelper("status", "Cider status helper timed out")
  }

  Timer {
    id: queueWatchdog
    interval: root.helperTimeoutMs
    repeat: false
    onTriggered: root.abortHelper("queue", "Cider queue helper timed out")
  }

  Timer {
    id: actionWatchdog
    interval: root.helperTimeoutMs
    repeat: false
    onTriggered: root.abortHelper("action", "Cider action helper timed out")
  }

  Timer {
    id: statusKillTimer
    interval: root.helperKillGraceMs
    repeat: false
    onTriggered: if (statusProcess.running) statusProcess.signal(9)
  }

  Timer {
    id: queueKillTimer
    interval: root.helperKillGraceMs
    repeat: false
    onTriggered: if (queueProcess.running) queueProcess.signal(9)
  }

  Timer {
    id: actionKillTimer
    interval: root.helperKillGraceMs
    repeat: false
    onTriggered: if (actionProcess.running) actionProcess.signal(9)
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.appendHelperOutput("status", false, data) }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.appendHelperOutput("status", true, data) }
    }
    onExited: function(exitCode) {
      root.helperExited("status", "Cider status failed")
    }
  }

  Process {
    id: queueProcess
    running: false
    command: []
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.appendHelperOutput("queue", false, data) }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.appendHelperOutput("queue", true, data) }
    }
    onExited: function(exitCode) {
      root.helperExited("queue", "Cider queue failed")
    }
  }

  Process {
    id: actionProcess
    running: false
    command: []
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.appendHelperOutput("action", false, data) }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.appendHelperOutput("action", true, data) }
    }
    onExited: function(exitCode) {
      root.helperExited("action", "Cider action failed")
    }
  }
}
