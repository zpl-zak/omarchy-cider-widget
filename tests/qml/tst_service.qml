import QtQuick
import QtTest
import Quickshell
import Quickshell.Io
import "../.."

TestCase {
  name: "CiderService"

  property var service: null

  Component {
    id: serviceComponent
    Service {}
  }

  function init() {
    service = serviceComponent.createObject(this)
    verify(service !== null)
    wait(1)
    verify(statusProcess() !== null)
    verify(statusProcess().running)
  }

  function cleanup() {
    service.destroy()
    service = null
    wait(1)
  }

  function processFor(command) {
    for (var i = ProcessRegistry.processes.length - 1; i >= 0; i--) {
      var process = ProcessRegistry.processes[i]
      if (process.command.length > 2 && process.command[0] === "python3"
          && process.command[2] === command) return process
    }
    return null
  }

  function statusProcess() { return processFor("status") }
  function queueProcess() { return processFor("queue") }
  function actionProcess() { return processFor("action") }

  function envelope(data) { return JSON.stringify({ ok: true, data: data }) }

  function connectedStatus() {
    return envelope({
      connected: true,
      playing: true,
      track: {
        id: "song-1",
        type: "song",
        title: "Ego Brain",
        artist: "System Of A Down",
        album: "Steal This Album!",
        artPath: "",
        durationSec: 201,
        positionSec: 50,
        inLibrary: false,
        inFavorites: false,
        audioTraits: ["lossless"]
      },
      volume: 0.8,
      shuffleMode: 1,
      repeatMode: 0,
      autoplay: true,
      fetchedAtMs: Date.now()
    })
  }

  function connectService() {
    statusProcess().complete(0, connectedStatus(), "")
    compare(service.probed, true)
    compare(service.connected, true)
  }

  function test_connected_status_updates_playback() {
    connectService()
    compare(service.playing, true)
    compare(service.track.title, "Ego Brain")
    compare(service.volume, 0.8)
    compare(service.shuffleMode, 1)
    compare(service.autoplay, true)
  }

  function test_missing_key_is_a_setup_state() {
    statusProcess().complete(3, JSON.stringify({
      ok: false,
      error: { code: "missing_api_key", message: "missing" }
    }), "")
    compare(service.probed, true)
    compare(service.configured, false)
    compare(service.connected, false)
    compare(service.lastError, "Cider API key is not configured.")
  }

  function test_queue_watch_loads_up_next() {
    connectService()
    service.beginQueueWatch()
    verify(queueProcess() !== null)
    verify(queueProcess().running)
    queueProcess().complete(0, envelope({
      upNext: [
        { id: "song-2", queueIndex: 2, skipCount: 1, title: "Next One" },
        { id: "song-3", queueIndex: 3, skipCount: 2, title: "Next Two" }
      ]
    }), "")
    compare(service.upNext.length, 2)
    compare(service.upNext[0].title, "Next One")
    service.endQueueWatch()
  }

  function test_action_is_allowlisted_and_refreshes() {
    connectService()
    verify(service.runAction("volume", 0.65))
    verify(actionProcess() !== null)
    compare(actionProcess().command.slice(-3), ["action", "volume", "0.65"])
    compare(service.volume, 0.65)
    actionProcess().complete(0, envelope({ action: "volume" }), "")
    wait(220)
    verify(statusProcess().running)
  }

  function test_queue_action_uses_absolute_indices_and_refreshes_queue() {
    connectService()
    service.beginQueueWatch()
    queueProcess().complete(0, envelope({
      upNext: [
        { id: "song-2", queueIndex: 2, title: "Next One" },
        { id: "song-3", queueIndex: 3, title: "Next Two" }
      ]
    }), "")

    verify(service.runAction("queueMove", [3, 2]))
    compare(actionProcess().command.slice(-4), ["action", "queueMove", "3", "2"])
    actionProcess().complete(0, envelope({ action: "queueMove" }), "")
    verify(queueProcess().running)
    service.endQueueWatch()
  }

  function test_unknown_action_is_rejected() {
    connectService()
    verify(!service.runAction("clearQueue"))
  }

  function test_invalid_status_schema_is_not_assigned() {
    statusProcess().complete(0, envelope({
      connected: "yes",
      playing: true,
      autoplay: false,
      track: null
    }), "")
    compare(service.connected, false)
    compare(service.track, null)
    compare(service.lastError, "Cider returned an invalid response schema")
  }

  function test_output_limit_terminates_helper() {
    statusProcess().stdout.read(new Array(service.helperOutputLimit + 2).join("x"))
    compare(statusProcess().signalsSent.length, 1)
    compare(statusProcess().signalsSent[0], 15)
    statusProcess().complete(143, "", "")
    compare(service.connected, false)
    compare(service.lastError, "Cider status helper exceeded its output limit")
  }

  function test_watchdog_requests_term_before_kill_grace() {
    service.abortHelper("status", "Cider status helper timed out")
    compare(statusProcess().signalsSent.length, 1)
    compare(statusProcess().signalsSent[0], 15)
    statusProcess().complete(143, "", "")
    compare(service.lastError, "Cider status helper timed out")
  }
}
