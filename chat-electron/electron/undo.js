const fsp = require("node:fs/promises")
const path = require("node:path")
const os = require("node:os")

/**
 * Undo for the agent.
 *
 * Edits land on disk straight away, so builds, tests and package managers see a
 * real working tree. Before anything is touched, the previous state is written
 * to a snapshot, which is what lets the user read the diff and put any of it
 * back.
 */

let root = process.cwd()

/** relPath -> { before, kind, at } */
const snapshots = new Map()

let onChange = () => {}

/** Snapshots older than this are dropped when the user accepts the changes. */
let notify = () => {}

function setUndoRoot(next) {
  activate(next)
}

function onChanged(fn) {
  notify = fn
}

function key(rel) {
  return path.normalize(rel)
}

async function diskRead(rel) {
  try {
    return await fsp.readFile(path.join(root, rel), "utf8")
  } catch {
    return null
  }
}

/**
 * Remember what a path looked like before the first change of this session.
 * Called before every mutation, so it is safe to call repeatedly.
 */
async function record(rel, kindHint = null) {
  const k = key(rel)
  if (snapshots.has(k)) return snapshots.get(k)

  const before = await diskRead(rel)
  const entry = { before, kind: kindHint ?? (before === null ? "created" : "modified"), at: Date.now() }
  snapshots.set(k, entry)
  bucket()[k] = entry
  notify()
  persist()
  return entry
}

/** Snapshot every file under a directory before it is deleted. */
async function recordTree(rel) {
  const base = path.join(root, rel)
  const found = []
  try {
    const entries = await fsp.readdir(base, { withFileTypes: true, recursive: true })
    for (const entry of entries) {
      if (!entry.isFile()) continue
      const full = entry.parentPath ? path.join(entry.parentPath, entry.name) : path.join(base, entry.name)
      found.push(key(path.relative(root, full)))
    }
  } catch {
    return []
  }
  for (const child of found) await record(child, "modified")
  return found
}

function listChanges() {
  return [...snapshots.entries()].map(([rel, entry]) => {
    const current = entry.before
    return {
      path: rel,
      kind: entry.kind,
      at: entry.at,
    }
  })
}

async function changesWithStats() {
  const out = []
  for (const [rel, entry] of snapshots.entries()) {
    const now = await diskRead(rel)
    out.push({
      path: rel,
      kind: entry.kind,
      at: entry.at,
      deleted: now === null,
      additions: countChanged(entry.before, now, "after"),
      deletions: countChanged(entry.before, now, "before"),
      unchanged: entry.before === now,
    })
  }
  return out.sort((a, b) => b.at - a.at)
}

function lineCount(text) {
  if (text === null) return 0
  if (text === "") return 0
  return text.split("\n").length
}

function countChanged(before, now, side) {
  if (before === null) return side === "after" ? lineCount(now) : 0
  if (now === null) return side === "before" ? lineCount(before) : 0
  if (before === now) return 0
  const delta = lineCount(now) - lineCount(before)
  if (delta !== 0) return side === "after" ? Math.max(0, delta) : Math.max(0, -delta)
  return 1
}

/** The two versions the diff viewer renders. */
async function versions(rel) {
  const entry = snapshots.get(key(rel))
  const now = await diskRead(rel)
  return {
    before: entry ? entry.before : now,
    after: now,
    tracked: Boolean(entry),
  }
}

/** Put one path back the way it was before the agent touched it. */
async function revert(rel) {
  const k = key(rel)
  const entry = snapshots.get(k)
  if (!entry) return { ok: false, output: `${rel} has not been changed, so there is nothing to revert` }

  const target = path.join(root, k)
  try {
    if (entry.before === null) {
      await fsp.rm(target, { recursive: true, force: true })
    } else {
      await fsp.mkdir(path.dirname(target), { recursive: true })
      await fsp.writeFile(target, entry.before, "utf8")
    }
  } catch (error) {
    return { ok: false, output: `could not revert ${rel}: ${error.message ?? error}` }
  }

  snapshots.delete(k)
  delete bucket()[k]
  notify()
  persist()
  return { ok: true, output: entry.before === null ? `removed ${rel}, it did not exist before` : `restored ${rel} to its previous contents` }
}

async function revertAll() {
  const rels = [...snapshots.keys()]
  const done = []
  const failed = []
  for (const rel of rels) {
    const result = await revert(rel)
    if (result.ok) done.push(rel)
    else failed.push({ path: rel, error: result.output })
  }
  return { reverted: done, failed }
}

/** Accept everything: the snapshots are dropped and nothing is revertible. */
function keepAll() {
  const kept = [...snapshots.keys()]
  snapshots.clear()
  delete store[root]
  notify()
  persist()
  return kept
}

function pendingCount() {
  return snapshots.size
}

function has(rel) {
  return snapshots.has(key(rel))
}

/* ---------- durability: snapshots outlive the window ---------- */

let storagePath = null
/** root -> { rel -> entry } so switching workspaces keeps each set intact */
let store = {}

/** Called once at startup with a path inside the app's userData directory. */
function setStorage(file) {
  storagePath = file
  load()
}

function load() {
  if (!storagePath) return
  try {
    const raw = require("node:fs").readFileSync(storagePath, "utf8")
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === "object" && parsed.workspaces) store = parsed.workspaces
  } catch {
    store = {}
  }
}

let persistTimer = null

function persist() {
  if (!storagePath) return
  if (persistTimer) return
  // batch bursts, but always flush before the process can die
  persistTimer = setTimeout(() => {
    persistTimer = null
    writeNow()
  }, 150)
  if (typeof persistTimer.unref === "function") persistTimer.unref()
}

function writeNow() {
  if (!storagePath) return
  if (persistTimer) {
    clearTimeout(persistTimer)
    persistTimer = null
  }
  try {
    const fs = require("node:fs")
    fs.mkdirSync(path.dirname(storagePath), { recursive: true })
    const tmp = storagePath + ".tmp"
    // write then rename, so a crash mid write cannot corrupt the store
    fs.writeFileSync(tmp, JSON.stringify({ version: 1, workspaces: store }), "utf8")
    fs.renameSync(tmp, storagePath)
  } catch {}
}

/** The snapshot set belonging to a given root. */
function bucket(forRoot = root) {
  if (!store[forRoot]) store[forRoot] = {}
  return store[forRoot]
}

/** Rehydrate the in-memory view for whichever workspace is now active. */
function activate(forRoot) {
  root = path.resolve(forRoot)
  snapshots.clear()
  for (const [rel, entry] of Object.entries(bucket())) snapshots.set(rel, entry)
  notify()
}

function flush() {
  writeNow()
}

module.exports = {
  setStorage,
  activate,
  flush,
  setUndoRoot,
  onChanged,
  record,
  recordTree,
  listChanges,
  changesWithStats,
  versions,
  revert,
  revertAll,
  keepAll,
  pendingCount,
  has,
  snapshotDir: () => storagePath,
  get root() { return root },
}
