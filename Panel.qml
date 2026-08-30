import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "zpl.cider"
  ipcTarget: "zpl.cider"
  manageIpc: false

  property double nowMs: Date.now()
  property bool queueWatching: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property var sharedService: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor(moduleName) : null
  readonly property var service: sharedService || localService
  readonly property bool hasTrack: service && service.hasTrack
  readonly property real displayPosition: service ? service.estimatedPosition(nowMs) : 0
  readonly property string title: hasTrack ? String(service.track.title || "") : ""
  readonly property string artist: hasTrack ? String(service.track.artist || "") : ""
  readonly property string album: hasTrack ? String(service.track.album || "") : ""

  function pushSettings() {
    if (service) service.settings = settings
  }

  function setQueueWatching(enabled) {
    var next = enabled === true
    if (!service || queueWatching === next) return
    queueWatching = next
    if (next) service.beginQueueWatch()
    else service.endQueueWatch()
  }

  function openCider() {
    Quickshell.execDetached(["uwsm-app", "--", "cider"])
    close()
  }

  function barTooltip() {
    if (!service || !service.probed) return "Cider · checking playback"
    if (!service.configured) return "Cider · API key unavailable"
    if (!service.connected) return "Cider · RPC unavailable"
    if (!hasTrack) return "Cider · nothing playing"
    return title + (artist ? " · " + artist : "")
  }

  function heroMessage() {
    if (!service.probed) return "Connecting to Cider"
    if (!service.configured) return "API key unavailable"
    if (!service.connected) return "Cider is offline"
    if (!hasTrack) return "Nothing playing"
    return service.playing ? "NOW PLAYING" : "PAUSED"
  }

  function setupTitle() {
    return service.configured ? "Cider RPC is not reachable" : "CIDER_API_KEY is not available"
  }

  function setupDetail() {
    if (!service.configured)
      return "Import the exported key into the user service environment, then restart Omarchy Shell."
    return "Open Cider and enable RPC under Settings > Connectivity > Manage External Application Access."
  }

  function queueEmptyText() {
    if (service.queueRefreshing) return "Reading the Cider queue…"
    if (!hasTrack) return "Start a song to see what plays next."
    return service.autoplay ? "Autoplay will choose what comes next." : "Nothing else is queued."
  }

  onSettingsChanged: pushSettings()
  onServiceChanged: pushSettings()
  Component.onCompleted: pushSettings()
  Component.onDestruction: setQueueWatching(false)

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    setQueueWatching(opened)
    if (!opened) return
    nowMs = Date.now()
    service.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Service {
    id: localService
    active: root.sharedService === null
  }

  Timer {
    interval: 250
    repeat: true
    running: root.opened || (root.service && root.service.playing)
    onTriggered: root.nowMs = Date.now()
  }

  IpcHandler {
    target: root.ipcTarget

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.service.refresh(); return "ok" }
    function playPause(): string { return root.service.runAction("playPause") ? "queued" : "unavailable" }
    function next(): string { return root.service.runAction("next") ? "queued" : "unavailable" }
    function previous(): string { return root.service.runAction("previous") ? "queued" : "unavailable" }
    function volume(value: string): string {
      var number = Number(value)
      if (!isFinite(number) || number < 0 || number > 1) return "volume must be between 0 and 1"
      return root.service.runAction("volume", number) ? "queued" : "unavailable"
    }
    function seek(value: string): string {
      var number = Number(value)
      if (!isFinite(number) || number < 0) return "seek must be a positive number of seconds"
      return root.service.runAction("seek", number) ? "queued" : "unavailable"
    }
    function status(): string {
      return JSON.stringify({
        configured: root.service.configured,
        connected: root.service.connected,
        playing: root.service.playing,
        title: root.title,
        artist: root.artist,
        volume: root.service.volume,
        shuffleMode: root.service.shuffleMode,
        repeatMode: root.service.repeatMode,
        autoplay: root.service.autoplay,
        upNext: root.service.upNext.length,
        error: root.service.lastError || root.service.actionError
      })
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.hasTrack && root.service.playing ? "󰏤" : "󰐊"
    foreground: root.service && root.service.connected
      ? (root.service.playing ? root.barForeground : Qt.darker(root.barForeground, 1.5))
      : root.urgent
    tooltipText: root.barTooltip()

    onPressed: function(mouseButton) {
      if (mouseButton === Qt.MiddleButton) root.service.runAction("next")
      else if (mouseButton === Qt.RightButton) root.service.refresh()
      else root.toggle()
    }
    onWheelMoved: function(delta) {
      if (delta > 0) root.service.runAction("previous")
      else if (delta < 0) root.service.runAction("next")
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(410))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight, Style.space(660))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onActivateRequested: root.service.runAction("playPause")
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        var key = String(text || "").toLowerCase()
        if (key === "p" || key === " ") root.service.runAction("playPause")
        else if (key === "n") root.service.runAction("next")
        else if (key === "b") root.service.runAction("previous")
        else if (key === "s") root.service.runAction("toggleShuffle")
        else if (key === "r") root.service.runAction("toggleRepeat")
        else if (key === "a") root.service.runAction("toggleAutoplay")
        else if (key === "o") root.openCider()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: contentColumn
          width: panelFlick.width
          spacing: Style.space(12)

          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            Text {
              Layout.fillWidth: true
              text: "Cider"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Text {
              visible: root.service.refreshing
              text: "SYNCING"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.1
            }

            PanelActionButton {
              iconText: "󰑐"
              tooltipText: "Refresh Cider"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !root.service.refreshing
              onClicked: root.service.refresh()
            }

            PanelActionButton {
              iconText: "󰏌"
              tooltipText: "Open Cider"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.openCider()
            }
          }

          BorderSurface {
            visible: root.service.probed && (!root.service.configured || !root.service.connected)
            width: parent.width
            implicitHeight: setupContent.implicitHeight + Style.space(22)
            radius: Style.cornerRadius
            color: Util.alpha(root.urgent, 0.08)
            borderSpec: Border.controlSpec("normal", root.urgent, root.urgent)

            RowLayout {
              id: setupContent
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              spacing: Style.space(10)

              Text {
                text: root.service.configured ? "󰅚" : "󰌆"
                color: root.urgent
                font.family: root.fontFamily
                font.pixelSize: Style.font.heading
                Layout.alignment: Qt.AlignTop
              }

              ColumnLayout {
                Layout.fillWidth: true
                spacing: Style.space(3)

                Text {
                  Layout.fillWidth: true
                  text: root.setupTitle()
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  font.bold: true
                  wrapMode: Text.WordWrap
                }

                Text {
                  Layout.fillWidth: true
                  text: root.setupDetail()
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  wrapMode: Text.WordWrap
                }
              }

              PanelActionButton {
                visible: root.service.configured
                iconText: "󰏌"
                tooltipText: "Open Cider"
                foreground: root.foreground
                fontFamily: root.fontFamily
                Layout.alignment: Qt.AlignVCenter
                onClicked: root.openCider()
              }
            }
          }

          RowLayout {
            visible: root.service.connected
            width: parent.width
            spacing: Style.space(12)

            BorderSurface {
              Layout.preferredWidth: Style.space(92)
              Layout.preferredHeight: Style.space(92)
              Layout.alignment: Qt.AlignTop
              radius: Style.spacing.labelGap
              color: Style.normalFillFor(root.foreground, Color.accent)
              borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)
              clip: true

              Image {
                anchors.fill: parent
                anchors.margins: Style.space(2)
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                source: root.hasTrack ? String(root.service.track.artUrl || "") : ""
                visible: source !== ""
              }

              Text {
                anchors.centerIn: parent
                visible: !root.hasTrack || !root.service.track.artUrl
                text: "󰝚"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.displayLarge
              }
            }

            ColumnLayout {
              Layout.fillWidth: true
              Layout.alignment: Qt.AlignVCenter
              spacing: Style.space(3)

              Text {
                Layout.fillWidth: true
                text: root.hasTrack ? root.title : "Nothing playing"
                textFormat: Text.PlainText
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.heading
                font.bold: true
                maximumLineCount: 2
                wrapMode: Text.Wrap
                elide: Text.ElideRight
              }

              Text {
                visible: root.artist !== ""
                Layout.fillWidth: true
                text: root.artist
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }

              Text {
                visible: root.album !== ""
                Layout.fillWidth: true
                text: root.album
                textFormat: Text.PlainText
                color: Qt.darker(root.foreground, 1.7)
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
              }

              RowLayout {
                visible: root.hasTrack
                Layout.fillWidth: true
                spacing: Style.space(6)

                Text {
                  text: root.heroMessage()
                  color: root.service.playing ? Color.accent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  font.letterSpacing: 1.0
                }

                Text {
                  visible: Model.audioBadge(root.service.track) !== ""
                  text: "·  " + Model.audioBadge(root.service.track)
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
              }
            }
          }

          Column {
            visible: root.service.connected && root.hasTrack
            width: parent.width
            spacing: Style.space(5)

            PanelSlider {
              id: progressSlider
              bar: root.bar
              width: parent.width
              minimum: 0
              maximum: Math.max(1, Number(root.hasTrack ? root.service.track.durationSec || 1 : 1))
              step: 5
              value: root.displayPosition
              enabled: root.hasTrack && Number(root.service.track.durationSec || 0) > 0
              onReleased: function(value) { root.service.runAction("seek", value) }
            }

            RowLayout {
              width: parent.width

              Text {
                text: Model.formatTime(progressSlider.dragging ? progressSlider.liveValue : root.displayPosition)
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              Item { Layout.fillWidth: true }

              Text {
                text: Model.formatTime(root.hasTrack ? root.service.track.durationSec : 0)
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          Row {
            visible: root.service.connected
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(5)

            Button {
              iconText: "󰒟"
              tooltipText: root.service.shuffleMode ? "Shuffle on" : "Shuffle off"
              selected: root.service.shuffleMode !== 0
              foreground: root.foreground
              accent: Color.accent
              horizontalPadding: Style.spacing.controlPaddingX
              verticalPadding: Style.spacing.controlPaddingY
              onClicked: root.service.runAction("toggleShuffle")
            }

            Button {
              iconText: "󰒮"
              tooltipText: "Previous"
              foreground: root.foreground
              horizontalPadding: Style.spacing.controlPaddingX
              verticalPadding: Style.spacing.controlPaddingY
              onClicked: root.service.runAction("previous")
            }

            Button {
              iconText: root.service.playing ? "󰏤" : "󰐊"
              tooltipText: root.service.playing ? "Pause" : "Play"
              foreground: root.foreground
              horizontalPadding: Style.spacing.panelGap
              verticalPadding: Style.spacing.controlPaddingY
              iconSize: Style.font.iconLarge
              onClicked: root.service.runAction("playPause")
            }

            Button {
              iconText: "󰒭"
              tooltipText: "Next"
              foreground: root.foreground
              horizontalPadding: Style.spacing.controlPaddingX
              verticalPadding: Style.spacing.controlPaddingY
              onClicked: root.service.runAction("next")
            }

            Button {
              iconText: Model.repeatIcon(root.service.repeatMode)
              tooltipText: Model.repeatLabel(root.service.repeatMode)
              selected: root.service.repeatMode !== 0
              foreground: root.foreground
              accent: Color.accent
              horizontalPadding: Style.spacing.controlPaddingX
              verticalPadding: Style.spacing.controlPaddingY
              onClicked: root.service.runAction("toggleRepeat")
            }
          }

          Column {
            visible: root.service.connected
            width: parent.width
            spacing: Style.space(5)

            RowLayout {
              width: parent.width

              PanelSectionHeader {
                text: "VOLUME"
                foreground: root.foreground
                fontFamily: root.fontFamily
                Layout.fillWidth: true
              }

              Button {
                text: "AUTOPLAY"
                iconText: "󰔰"
                tooltipText: root.service.autoplay ? "Autoplay on" : "Autoplay off"
                selected: root.service.autoplay
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                iconSize: Style.font.bodySmall
                horizontalPadding: Style.space(6)
                verticalPadding: Style.space(3)
                onClicked: root.service.runAction("toggleAutoplay")
              }
            }

            RowLayout {
              width: parent.width
              spacing: Style.space(8)

              Text {
                text: Model.volumeIcon(volumeSlider.dragging ? volumeSlider.liveValue : root.service.volume)
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }

              PanelSlider {
                id: volumeSlider
                bar: root.bar
                Layout.fillWidth: true
                minimum: 0
                maximum: 1
                step: 0.05
                value: root.service.volume
                onReleased: function(value) { root.service.runAction("volume", value) }
              }

              Text {
                text: Math.round((volumeSlider.dragging ? volumeSlider.liveValue : root.service.volume) * 100) + "%"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                Layout.preferredWidth: Style.space(30)
                horizontalAlignment: Text.AlignRight
              }
            }
          }

          Text {
            visible: root.service.actionError !== ""
            width: parent.width
            text: root.service.actionError
            textFormat: Text.PlainText
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          PanelSeparator {
            visible: root.service.connected
            foreground: root.foreground
          }

          RowLayout {
            visible: root.service.connected
            width: parent.width

            PanelSectionHeader {
              text: "UP NEXT"
              foreground: root.foreground
              fontFamily: root.fontFamily
              Layout.fillWidth: true
            }

            Text {
              visible: root.service.upNext.length > 0
              text: root.service.upNext.length + " SHOWN"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
            }

            PanelActionButton {
              iconText: root.service.queueRefreshing ? "󰑓" : "󰑐"
              tooltipText: "Refresh Up Next"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !root.service.queueRefreshing
              onClicked: root.service.refreshQueue()
            }
          }

          Text {
            visible: root.service.connected && root.service.upNext.length === 0
            width: parent.width
            text: root.queueEmptyText()
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            topPadding: Style.space(5)
            bottomPadding: Style.space(5)
          }

          Column {
            id: queueColumn
            visible: root.service.connected && root.service.upNext.length > 0
            width: parent.width
            spacing: Style.space(5)

            Repeater {
              model: root.service.upNext

              BorderSurface {
                required property var modelData
                required property int index
                width: queueColumn.width
                implicitHeight: queueRow.implicitHeight + Style.space(10)
                radius: Style.spacing.labelGap
                color: index === 0 ? Util.alpha(Color.accent, 0.08) : "transparent"
                borderSpec: index === 0
                  ? Border.controlSpec("normal", root.foreground, Color.accent)
                  : Border.none()

                RowLayout {
                  id: queueRow
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.leftMargin: Style.space(7)
                  anchors.rightMargin: Style.space(7)
                  spacing: Style.space(8)

                  BorderSurface {
                    Layout.preferredWidth: Style.space(36)
                    Layout.preferredHeight: Style.space(36)
                    radius: Style.spacing.labelGap
                    color: Style.normalFillFor(root.foreground, Color.accent)
                    borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)
                    clip: true

                    Image {
                      anchors.fill: parent
                      anchors.margins: Style.space(1)
                      source: String(modelData.artUrl || "")
                      fillMode: Image.PreserveAspectCrop
                      asynchronous: true
                      visible: source !== ""
                    }

                    Text {
                      anchors.centerIn: parent
                      visible: !modelData.artUrl
                      text: "󰝚"
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }
                  }

                  ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Style.space(1)

                    Text {
                      Layout.fillWidth: true
                      text: String(modelData.title || "Unknown track")
                      textFormat: Text.PlainText
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.bold: index === 0
                      elide: Text.ElideRight
                    }

                    Text {
                      Layout.fillWidth: true
                      text: Model.queueMeta(modelData)
                      textFormat: Text.PlainText
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideRight
                    }
                  }

                  Text {
                    text: Model.formatTime(modelData.durationSec)
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    Layout.alignment: Qt.AlignVCenter
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
