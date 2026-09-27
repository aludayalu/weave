const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron")
const path = require("node:path")
const pathModule = path
const fs = require("node:fs")
const { spawn } = require("node:child_process")
const { runTool, setRoot, getRoot, setSinks, setConfirming, trusted, MODEL_TOOLS } = require("./tools")
const undo = require("./undo")

const PORT = Number(process.env.CHAT_PORT || 4311)
const isDev = !app.isPackaged

let mainWindow = null
let server = null

/** Approval requests waiting on the user, keyed by request id. */
const pendingApprovals = new Map()
let approvalCounter = 0

function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload)
  }
}

undo.onChanged(() => {
  send("changes:updated", { at: Date.now() })
})

setSinks({
  onOutput: ({ callId, chunk }) => send("command:output", { callId, chunk }),
  onApproval: (request) => new Promise((resolve) => {
    const id = ++approvalCounter
    pendingApprovals.set(id, resolve)
    send("tools:approval", { id, ...request })
  }),
})

function startServer() {
  return new Promise((resolve, reject) => {
    const args = isDev ? ["next", "dev", "-p", String(PORT)] : ["next", "start", "-p", String(PORT)]
    const command = process.platform === "win32" ? "bun.cmd" : "bun"
    server = spawn(command, args, {
      cwd: app.getAppPath(),
      env: { ...process.env, NODE_ENV: isDev ? "development" : "production" },
      stdio: ["ignore", "pipe", "pipe"],
    })
    server.stdout.on("data", (chunk) => process.stdout.write(`[next] ${chunk}`))
    server.stderr.on("data", (chunk) => process.stderr.write(`[next] ${chunk}`))
    server.on("error", reject)

    const deadline = Date.now() + 90_000
    const poll = setInterval(() => {
      fetch(`http://localhost:${PORT}`)
        .then(() => {
          clearInterval(poll)
          resolve()
        })
        .catch(() => {
          if (Date.now() > deadline) {
            clearInterval(poll)
            reject(new Error("the Next server did not start within 90s"))
          }
        })
    }, 400)
  })
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 720,
    minHeight: 480,
    titleBarStyle: "hiddenInset",
    backgroundColor: "#0b0b0d",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: false,
    },
  })

  mainWindow.loadURL(`http://localhost:${PORT}`)

  // external links belong in the browser, never inside the app
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: "deny" }
  })
}

ipcMain.handle("workspace:get", () => ({ root: getRoot() }))

ipcMain.handle("workspace:choose", async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory", "createDirectory"],
    title: "Choose the workspace the agent may work in",
  })
  if (result.canceled || result.filePaths.length === 0) return { root: getRoot(), canceled: true }
  return { root: setRoot(result.filePaths[0]), canceled: false }
})

ipcMain.handle("workspace:set", (_event, root) => {
  if (typeof root !== "string" || !root) return { root: getRoot() }
  try {
    const next = setRoot(root)
    undo.activate(next)
    return { root: next }
  } catch {
    return { root: getRoot() }
  }
})

ipcMain.handle("tools:list", () => MODEL_TOOLS)

ipcMain.handle("tools:run", async (_event, { name, args, callId }) => {
  const started = Date.now()
  // the call id lets streamed command output find its row in the UI
  const result = await runTool(name, { ...args, __callId: callId ?? null })
  // attachments are structured-cloned to the renderer, never stringified
  return { ...result, name, durationMs: Date.now() - started }
})

ipcMain.handle("tools:approval-mode", (_event, value) => {
  setConfirming(value)
  return { confirming: Boolean(value), trusted: [...trusted] }
})

ipcMain.handle("tools:approval-response", (_event, { id, verdict }) => {
  const resolve = pendingApprovals.get(id)
  if (!resolve) return { ok: false }
  pendingApprovals.delete(id)
  resolve(verdict)
  return { ok: true }
})

ipcMain.handle("files:read", async (_event, { path: relative }) => {
  const target = pathModule.join(getRoot(), relative)
  try {
    const contents = await require("node:fs/promises").readFile(target, "utf8")
    return { ok: true, contents, name: pathModule.basename(target) }
  } catch (error) {
    return { ok: false, output: String(error.message ?? error) }
  }
})

ipcMain.handle("files:write", async (_event, { path: relative, contents }) => {
  // an edit made in the panel is a real change, so snapshot it like any other
  await undo.record(relative, "modified")
  const result = await runTool("write_file", { path: relative, content: contents })
  return result
})

ipcMain.handle("changes:list", async () => {
  // never push from here: the renderer calls this in response to
  // changes:updated, so emitting would loop forever
  const changes = await undo.changesWithStats()
  return { changes }
})

ipcMain.handle("changes:versions", async (_event, { path: relative }) => {
  const result = await runTool("file_versions", { path: relative })
  return result
})

ipcMain.handle("changes:revert", async (_event, { path: relative }) => {
  const result = await undo.revert(relative)
  const changes = await undo.changesWithStats()
  send("changes:updated", { count: changes.filter((c) => !c.unchanged).length })
  return result
})

ipcMain.handle("changes:revert-all", async () => {
  const result = await undo.revertAll()
  return result
})

ipcMain.handle("changes:keep-all", async () => {
  const kept = undo.keepAll()
  return { kept }
})

app.on("before-quit", () => {
  undo.flush()
})

app.whenReady().then(async () => {
  // snapshots live in userData so they survive a crash or a quit
  undo.setStorage(path.join(app.getPath("userData"), "undo-store.json"))

  setRoot(process.env.CHAT_WORKSPACE || app.getAppPath())
  try {
    await startServer()
  } catch (error) {
    dialog.showErrorBox("Could not start the app", String(error))
    app.quit()
    return
  }
  createWindow()

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on("window-all-closed", () => {
  if (server) server.kill()
  if (process.platform !== "darwin") app.quit()
})

app.on("before-quit", () => {
  if (server) server.kill()
})
