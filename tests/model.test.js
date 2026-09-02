const test = require("node:test")
const assert = require("node:assert/strict")
const Model = require("../Model.js")

test("parses successful and failed helper responses", () => {
  assert.deepEqual(Model.parseResponse(JSON.stringify({ ok: true, data: { playing: true } })), {
    ok: true,
    data: { playing: true }
  })

  assert.deepEqual(Model.parseResponse(JSON.stringify({
    ok: false,
    error: { code: "unauthorized", message: "Cider rejected the app token" }
  })), {
    ok: false,
    code: "unauthorized",
    error: "Cider rejected the app token"
  })
})

test("rejects oversized helper output and invalid success schemas", () => {
  assert.equal(Model.parseJson("x".repeat(65537)).ok, false)
  assert.equal(Model.parseResponse(JSON.stringify({ ok: true, data: [] })).ok, false)
})

test("validates and bounds status fields before exposing them", () => {
  const cacheRoot = "/home/test/.cache/omarchy-cider-widget/artwork"
  const safePath = cacheRoot + "/" + "a".repeat(64) + ".png"
  const result = Model.parseStatusResponse(JSON.stringify({
    ok: true,
    data: {
      connected: true,
      playing: true,
      track: {
        id: "i".repeat(300),
        type: "song",
        title: "t".repeat(700),
        artist: "Artist",
        album: "Album",
        artPath: safePath,
        durationSec: 100,
        positionSec: 500,
        inLibrary: false,
        inFavorites: true,
        audioTraits: Array(20).fill("lossless")
      },
      volume: 5,
      shuffleMode: 8,
      repeatMode: 9,
      autoplay: false,
      fetchedAtMs: Date.now()
    }
  }), cacheRoot)

  assert.equal(result.ok, true)
  assert.equal(result.data.track.id.length, 256)
  assert.equal(result.data.track.title.length, 512)
  assert.equal(result.data.track.audioTraits.length, 8)
  assert.equal(result.data.track.positionSec, 100)
  assert.equal(result.data.track.artSource, "file://" + safePath)
  assert.equal(result.data.volume, 1)
  assert.equal(result.data.repeatMode, 2)
})

test("filters queue schema and rejects artwork outside the private cache", () => {
  const cacheRoot = "/home/test/.cache/omarchy-cider-widget/artwork"
  const rows = [{
    id: "one",
    type: "song",
    queueIndex: 2,
    skipCount: 1,
    title: "Next",
    artist: "Artist",
    album: "Album",
    artPath: "/tmp/not-approved.png",
    durationSec: 60
  }, { id: "bad", title: "Missing indices" }]
  for (let index = 0; index < 30; index++) rows.push({
    id: String(index), queueIndex: index, skipCount: 1, title: "Track"
  })
  const result = Model.parseQueueResponse(JSON.stringify({ ok: true, data: { upNext: rows } }), cacheRoot)
  assert.equal(result.ok, true)
  assert.equal(result.data.upNext.length, 19)
  assert.equal(result.data.upNext[0].artSource, "")
  assert.equal(result.data.upNext.some(item => item.id === "bad"), false)
})

test("maps setup failures to useful panel copy", () => {
  assert.equal(
    Model.friendlyError({ code: "missing_api_key", error: "missing" }),
    "Cider API key is not configured."
  )
  assert.equal(
    Model.friendlyError({ code: "unavailable", error: "offline" }),
    "Cider RPC is not reachable on localhost:10767."
  )
})

test("formats playback values", () => {
  assert.equal(Model.formatTime(0), "0:00")
  assert.equal(Model.formatTime(201.9), "3:21")
  assert.equal(Model.repeatLabel(0), "Repeat off")
  assert.equal(Model.repeatLabel(1), "Repeat one")
  assert.equal(Model.repeatLabel(2), "Repeat all")
  assert.equal(Model.volumeIcon(0), "󰖁")
  assert.equal(Model.volumeIcon(0.25), "󰕿")
  assert.equal(Model.volumeIcon(0.8), "󰕾")
})

test("detects audio quality and builds queue metadata", () => {
  assert.equal(Model.audioBadge({ audioTraits: ["lossless", "atmos"] }), "DOLBY ATMOS")
  assert.equal(Model.audioBadge({ audioTraits: ["lossless"] }), "LOSSLESS")
  assert.equal(Model.queueMeta({ title: "Track", artist: "Artist", album: "Album" }), "Artist · Album")
})

test("updates track position without mutating the source", () => {
  const track = { title: "Song", durationSec: 100, positionSec: 5 }
  const updated = Model.trackWithPosition(track, 140)
  assert.equal(updated.positionSec, 100)
  assert.equal(track.positionSec, 5)
})

test("builds only allowlisted helper action commands", () => {
  assert.deepEqual(
    Model.actionCommand("/plugin/cider-rpc.py", { name: "volume", value: 0.65 }),
    ["python3", "/plugin/cider-rpc.py", "action", "volume", "0.65"]
  )
  assert.deepEqual(
    Model.actionCommand("/plugin/cider-rpc.py", { name: "queueMove", value: [4, 3] }),
    ["python3", "/plugin/cider-rpc.py", "action", "queueMove", "4", "3"]
  )
  assert.deepEqual(
    Model.actionCommand("/plugin/cider-rpc.py", { name: "skipTo", value: 2 }),
    ["python3", "/plugin/cider-rpc.py", "action", "skipTo", "2"]
  )
  assert.deepEqual(Model.actionCommand("/plugin/cider-rpc.py", { name: "deleteEverything" }), [])
})
