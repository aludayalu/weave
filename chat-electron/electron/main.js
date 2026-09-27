const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron")
const path = require("node:path")
const fs = require("node:fs")
const { spawn } = require("node:child_process")
const { runTool, setRoot, getRoot, setSinks, setConfirming, trusted, MODEL_TOOLS } = require("./tools")

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
    return { root: setRoot(root) }
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

app.whenReady().then(async () => {
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
