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

  readonly property string helperPath: decodeURIComponent(
    Qt.resolvedUrl("cider-rpc.py").toString().replace(/^file:\/\//, ""))
  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 2, 1, 30)
  readonly property int queueLimit: intSetting("queueLimit", 8, 3, 20)
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
    statusProcess.command = ["python3", helperPath, "status"]
    statusProcess.running = true
  }

  function applyStatus(raw) {
    var result = Model.parseResponse(raw)
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
    queueProcess.command = ["python3", helperPath, "queue", String(queueLimit)]
    queueProcess.running = true
  }

  function applyQueue(raw) {
    var result = Model.parseResponse(raw)
    queueRefreshing = false
    if (!result.ok) {
      actionError = Model.friendlyError(result)
      finishQueueRefresh()
      return
    }
    upNext = Array.isArray(result.data.upNext) ? result.data.upNext : []
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
    applyOptimisticAction(name, value)

    var next = []
    for (var i = 0; i < _actions.length; i++) {
      var queued = _actions[i]
      if ((name === "volume" || name === "seek") && queued.name === name) continue
      next.push(queued)
    }
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
    actionProcess.command = Model.actionCommand(helperPath, _currentAction)
    actionProcess.running = true
  }

  function finishAction(raw) {
    var action = _currentAction
    _currentAction = null
    var result = Model.parseResponse(raw)
    actionError = result.ok ? "" : Model.friendlyError(result)
    if (!result.ok) lastError = actionError
    refreshSoon.restart()
    if (action && (action.name === "queueMove" || action.name === "queueRemove")) refreshQueue()
    else if (action && ["next", "previous", "skipTo"].indexOf(action.name) !== -1) queueSoon.restart()
    Qt.callLater(root.startNextAction)
  }

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

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector {
      id: statusStdout
      waitForEnd: true
      onStreamFinished: root._statusOutput = text
    }
    stderr: StdioCollector {
      id: statusStderr
      waitForEnd: true
      onStreamFinished: root._statusError = text
    }
    onExited: function(exitCode) {
      var raw = String(statusStdout.text || root._statusOutput || "")
      if (raw === "") raw = JSON.stringify({
        ok: false,
        error: { code: "request_failed", message: String(statusStderr.text || root._statusError || "Cider status failed") }
      })
      root.applyStatus(raw)
    }
  }

  Process {
    id: queueProcess
    running: false
    command: []
    stdout: StdioCollector {
      id: queueStdout
      waitForEnd: true
      onStreamFinished: root._queueOutput = text
    }
    stderr: StdioCollector {
      id: queueStderr
      waitForEnd: true
      onStreamFinished: root._queueError = text
    }
    onExited: function(exitCode) {
      var raw = String(queueStdout.text || root._queueOutput || "")
      if (raw === "") raw = JSON.stringify({
        ok: false,
        error: { code: "request_failed", message: String(queueStderr.text || root._queueError || "Cider queue failed") }
      })
      root.applyQueue(raw)
    }
  }

  Process {
    id: actionProcess
    running: false
    command: []
    stdout: StdioCollector {
      id: actionStdout
      waitForEnd: true
      onStreamFinished: root._actionOutput = text
    }
    stderr: StdioCollector {
      id: actionStderr
      waitForEnd: true
      onStreamFinished: root._actionErrorOutput = text
    }
    onExited: function(exitCode) {
      var raw = String(actionStdout.text || root._actionOutput || "")
      if (raw === "") raw = JSON.stringify({
        ok: false,
        error: { code: "request_failed", message: String(actionStderr.text || root._actionErrorOutput || "Cider action failed") }
      })
      root.finishAction(raw)
    }
  }
}
