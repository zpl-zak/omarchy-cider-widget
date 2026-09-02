pragma Singleton
import QtQml

QtObject {
  function execDetached(command) {}
  function env(name) {
    if (name === "HOME") return "/home/test"
    return ""
  }
}
