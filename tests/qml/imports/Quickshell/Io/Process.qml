import QtQml

QtObject {
  id: root

  property bool running: false
  property var command: []
  property var stdout: null
  property var stderr: null
  property bool _completing: false
  property var signalsSent: []

  signal exited(int exitCode, int exitStatus)

  onRunningChanged: if (!running && !_completing) exited(143, 1)

  function complete(exitCode, stdoutText, stderrText) {
    feed(stdout, stdoutText)
    feed(stderr, stderrText)
    _completing = true
    running = false
    _completing = false
    exited(exitCode, 0)
  }

  function feed(stream, value) {
    if (!stream) return
    if (typeof stream.read === "function") stream.read(String(value || ""))
    else {
      stream.text = String(value || "")
      stream.streamFinished()
    }
  }

  function signal(signalNumber) {
    var next = signalsSent.slice()
    next.push(signalNumber)
    signalsSent = next
  }

  Component.onCompleted: ProcessRegistry.add(root)
  Component.onDestruction: ProcessRegistry.remove(root)
}
