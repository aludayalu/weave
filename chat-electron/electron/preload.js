const { contextBridge, ipcRenderer } = require("electron")

/**
 * The renderer gets no Node access. Everything it can do to the machine goes
 * through these four calls, all of which land in the main process.
 */
contextBridge.exposeInMainWorld("desktop", {
  isDesktop: true,

  workspace: {
    get: () => ipcRenderer.invoke("workspace:get"),
    choose: () => ipcRenderer.invoke("workspace:choose"),
    set: (root) => ipcRenderer.invoke("workspace:set", root),
  },

  tools: {
    list: () => ipcRenderer.invoke("tools:list"),
    run: (name, args) => ipcRenderer.invoke("tools:run", { name, args }),
  },

  /** Live stdout/stderr for a running command, keyed by tool call id. */
  onCommandOutput: (callback) => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on("command:output", listener)
    return () => ipcRenderer.removeListener("command:output", listener)
  },

  /**
   * Called when the agent wants to change something. The returned promise settles
   * with "allow", "deny" or "always".
   */
  approvals: {
    setMode: (confirming) => ipcRenderer.invoke("tools:approval-mode", confirming),
    onRequest: (callback) => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on("tools:approval", listener)
      return () => ipcRenderer.removeListener("tools:approval", listener)
    },
    respond: (id, verdict) => ipcRenderer.invoke("tools:approval-response", { id, verdict }),
  },
})
