const fs = require("node:fs")
const fsp = require("node:fs/promises")
const path = require("node:path")
const os = require("node:os")
const { spawn } = require("node:child_process")

/** Every path the model touches is resolved inside this root. */
let root = process.cwd()

/** Injected by the main process so tools can stream and ask for permission. */
let sinks = { onOutput: null, onApproval: null }

/** When false the agent may change anything without asking. */
let confirming = true
/** Tools the user chose to stop approving for the rest of this run. */
const trusted = new Set()

function setSinks(next) {
  sinks = { ...sinks, ...next }
}

function setConfirming(value) {
  confirming = Boolean(value)
}

function isTrusted(name) {
  return trusted.has(name)
}

function trust(name) {
  trusted.add(name)
}

/**
 * Shell that can destroy work, exfiltrate data, or rewrite history. These are
 * asked about even when blanket confirmation is switched off.
 */
const DANGEROUS = [
  /\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]/,
  /\bsudo\b/,
  /\bgit\s+push\b[^\n]*(--force|-f)\b/,
  /\bgit\s+reset\s+--hard\b/,
  /\bgit\s+clean\b[^\n]*-[a-zA-Z]*[fd]/,
  /\bgit\s+rebase\b[^\n]*(--abort)?/,
  /\bchmod\s+(-[a-zA-Z]+\s+)*(777|a\+rwx)\b/,
  /\bchown\b/,
  /\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(ba)?sh\b/,
  /\bnpm\s+publish\b/,
  /\bbun\s+publish\b/,
  /\bbrew\s+uninstall\b/,
  /\bkillall\b|\bpkill\b/,
  /\bdiskutil\b/,
  /\bdd\s+if=/,
  /\bmkfs\b/,
  /\bmv\b[^\n]*\/(system|usr|bin|etc)\b/,
  /\bdefaults\s+delete\b/,
  /\bosascript\b/,
  /\bsecurity\s+add-generic-password\b/,
  />\s*\/dev\/(disk|sd)/,
  /\bnpx\b/,
  /\b(launchctl|crontab)\b/,
]

function isDangerous(command) {
  return DANGEROUS.some((pattern) => pattern.test(command))
}

/**
 * Resolves true to run, false to refuse. Falls through to running when there is
 * nobody to ask, so the tool still works headless.
 */
async function approved(name, summary, alwaysAllowed) {
  if (alwaysAllowed) return true
  if (!confirming) return true
  if (isTrusted(name)) return true
  if (typeof sinks.onApproval !== "function") return true

  const verdict = await sinks.onApproval({ name, summary })
  if (verdict === "always") trusted.add(name)
  return verdict === "allow" || verdict === "always"
}

/**
 * The lines around a change, so the model can see exactly what it altered in
 * context instead of trusting a bare "done".
 */
function contextWindow(content, charIndex, radius = 4) {
  const lines = content.split("\n")
  let consumed = 0
  let lineIndex = 0
  for (let i = 0; i < lines.length; i++) {
    if (consumed + lines[i].length >= charIndex) { lineIndex = i; break }
    consumed += lines[i].length + 1
  }

  const start = Math.max(0, lineIndex - radius)
  const end = Math.min(lines.length, lineIndex + radius + 1)
  const body = lines
    .slice(start, end)
    .map((line, index) => `${String(start + index + 1).padStart(5)} | ${line}`)
    .join("\n")

  return {
    text: `lines ${start + 1}-${end} of ${lines.length}, showing context around line ${lineIndex + 1}\n\n${body}`,
    startLine: start + 1,
    endLine: end,
  }
}

const REFUSED = (what) => ({
  ok: false,
  output: `The user declined ${what}. Do not retry it. Ask what they would prefer instead.`,
  meta: { declined: true },
})

function setRoot(next) {
  root = path.resolve(next)
  return root
}

function getRoot() {
  return root
}

class JailError extends Error {}

function resolveInside(relative) {
  const target = path.resolve(root, relative ?? ".")
  const rel = path.relative(root, target)
  if (rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new JailError(`${relative} is outside the workspace (${root})`)
  }
  return target
}

const MAX_READ_BYTES = 2 * 1024 * 1024
const MAX_OUTPUT = 60_000

function clip(text) {
  const value = String(text ?? "")
  if (value.length <= MAX_OUTPUT) return { text: value, truncated: false }
  return {
    text: `${value.slice(0, MAX_OUTPUT)}\n... truncated at ${MAX_OUTPUT} characters ...`,
    truncated: true,
  }
}

function humanSize(bytes) {
  const units = ["B", "KB", "MB", "GB"]
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)}${units[unit]}`
}


/* ---------- media: images and PDFs the model can actually look at ---------- */

const IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

const PDF_TYPE = "application/pdf"

const MAX_IMAGE_BYTES = 8 * 1024 * 1024
const MAX_PDF_BYTES = 20 * 1024 * 1024

/** Magic bytes beat the extension, since files lie about what they are. */
function sniff(buffer) {
    if (buffer.length > 8 &&
        buffer[0] === 0x89 && buffer[1] === 0x50 && buffer[2] === 0x4e && buffer[3] === 0x47) return "image/png"
    if (buffer.length > 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) return "image/jpeg"
    if (buffer.length > 3 && buffer.subarray(0, 3).toString("latin1") === "GIF") return "image/gif"
    if (buffer.length > 12 && buffer.subarray(0, 4).toString("latin1") === "RIFF" && buffer.subarray(8, 12).toString("latin1") === "WEBP") return "image/webp"
    if (buffer.length > 5 && buffer.subarray(0, 5).toString("latin1") === "%PDF-") return PDF_TYPE
    return null
}

/** Just enough header parsing to report dimensions without a native dependency. */
function imageSize(buffer, mime) {
    try {
        if (mime === "image/png" && buffer.length > 24) {
            return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) }
        }
        if (mime === "image/gif" && buffer.length > 10) {
            return { width: buffer.readUInt16LE(6), height: buffer.readUInt16LE(8) }
        }
        if (mime === "image/jpeg") {
            let offset = 2
            while (offset + 9 < buffer.length) {
                if (buffer[offset] !== 0xff) { offset++; continue }
                const marker = buffer[offset + 1]
                const length = buffer.readUInt16BE(offset + 2)
                if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
                    return { height: buffer.readUInt16BE(offset + 5), width: buffer.readUInt16BE(offset + 7) }
                }
                offset += 2 + length
            }
        }
        if (mime === "image/webp" && buffer.length > 30) {
            const format = buffer.subarray(12, 16).toString("latin1")
            if (format === "VP8X") {
                const width = 1 + (buffer[24] | (buffer[25] << 8) | (buffer[26] << 16))
                const height = 1 + (buffer[27] | (buffer[28] << 8) | (buffer[29] << 16))
                return { width, height }
            }
        }
    } catch {}
    return null
}

function countPdfPages(text) {
    const matches = text.match(/\/Type\s*\/Page[^s]/g)
    return matches ? matches.length : null
}


/* ---------- diffs: what changed in a file since the last commit ---------- */

const { execFile } = require("node:child_process")

function git(args, cwd) {
  return new Promise((resolve) => {
    execFile("git", args, { cwd, maxBuffer: 8 * 1024 * 1024 }, (error, stdout) => {
      resolve(error ? null : stdout)
    })
  })
}

async function isTracked(cwd, relative) {
  const listed = await git(["ls-files", "--error-unmatch", "--", relative], cwd)
  return listed !== null
}

async function hasCommits(cwd) {
  return (await git(["rev-parse", "--verify", "HEAD"], cwd)) !== null
}

/** Which line numbers moved, used for the +N/-M badge in the UI. */
function diffHunks(before, after) {
  const a = before.split("\n")
  const b = after.split("\n")
  const hunks = []
  let start = 0
  while (start < a.length && start < b.length && a[start] === b[start]) start++
  if (start === a.length && start === b.length) return hunks

  let endA = a.length - 1
  let endB = b.length - 1
  while (endA >= start && endB >= start && a[endA] === b[endB]) { endA--; endB-- }

  const removed = endA - start + 1
  const added = endB - start + 1
  if (removed > 0 || added > 0) {
    hunks.push({ startLine: start + 1, removed, added })
  }
  return hunks
}

/** Render a unified diff without shelling out, for non-git workspaces. */
function syntheticDiff(relative, before, after) {
  if (before === null) {
    const lines = after.split("\n")
    const head = [
      `--- /dev/null`,
      `+++ b/${relative}`,
      `@@ -0,0 +1,${lines.length} @@`,
    ]
    return [...head, ...lines.map((line) => `+${line}`)].join("\n")
  }

  const a = before.split("\n")
  const b = after.split("\n")
  const hunks = diffHunks(before, after)
  if (hunks.length === 0) return ""

  const hunk = hunks[0]
  const context = 3
  const start = Math.max(0, hunk.startLine - 1 - context)
  const aEnd = Math.min(a.length, hunk.startLine - 1 + hunk.removed + context)
  const bEnd = Math.min(b.length, hunk.startLine - 1 + hunk.added + context)

  const out = [
    `--- a/${relative}`,
    `+++ b/${relative}`,
    `@@ -${start + 1},${aEnd - start} +${start + 1},${bEnd - start} @@`,
  ]

  for (let i = start; i < hunk.startLine - 1; i++) out.push(` ${a[i]}`)
  for (let i = hunk.startLine - 1; i < hunk.startLine - 1 + hunk.removed; i++) {
    if (i < a.length) out.push(`-${a[i]}`)
  }
  for (let i = hunk.startLine - 1; i < hunk.startLine - 1 + hunk.added; i++) {
    if (i < b.length) out.push(`+${b[i]}`)
  }
  for (let i = hunk.startLine - 1 + Math.max(hunk.removed, hunk.added); i < bEnd; i++) {
    if (i < b.length) out.push(` ${b[i]}`)
  }

  return out.join("\n")
}

const PREVIEW_MAX_BYTES = 12 * 1024 * 1024

async function readIfPresent(target) {
  try {
    return await fsp.readFile(target, "utf8")
  } catch {
    return null
  }
}

const tools = {
  async list_dir({ dir = ".", depth = 1 }) {
    const target = resolveInside(dir)
    const stats = await fsp.stat(target)
    if (!stats.isDirectory()) throw new Error(`${dir} is not a directory`)
    const lines = []
    const walk = async (current, level) => {
      if (level < 0) return
      const entries = await fsp.readdir(current, { withFileTypes: true })
      entries.sort((a, b) => Number(b.isDirectory()) - Number(a.isDirectory()) || a.name.localeCompare(b.name))
      for (const entry of entries) {
        if (entry.name === ".git" || entry.name === "node_modules" || entry.name === ".next") continue
        const full = path.join(current, entry.name)
        const rel = path.relative(target, full)
        if (entry.isDirectory()) {
          lines.push(`${rel}/`)
          if (level > 0) await walk(full, level - 1)
        } else {
          let size = ""
          try {
            size = `  ${humanSize((await fsp.stat(full)).size)}`
          } catch {}
          lines.push(`${rel}${size}`)
        }
      }
    }
    await walk(target, Number(depth) || 1)
    const { text, truncated } = clip(lines.join("\n") || "(empty)")
    return { ok: true, output: `${target}\n${lines.length} entries\n\n${text}`, meta: { truncated, count: lines.length } }
  },

  async read_file({ path: relative, start_line = 1, end_line = 400 }) {
    const target = resolveInside(relative)
    const stats = await fsp.stat(target)

    const media = sniff(await fsp.readFile(target))
    if (media) {
      return {
        ok: false,
        output: `${relative} is binary (${media}). read_file cannot show it to you. Call read_media with this path instead, which attaches it so you can actually look at it.`,
        meta: { binary: true, mime: media },
      }
    }

    if (stats.size > MAX_READ_BYTES) {
      throw new Error(`${relative} is ${humanSize(stats.size)}, too large to read whole`)
    }
    const content = await fsp.readFile(target, "utf8")
    const lines = content.split("\n")
    const start = Math.max(1, Number(start_line) || 1)
    const end = Math.min(lines.length, Number(end_line) || 400)
    const body = lines
      .slice(start - 1, end)
      .map((line, index) => `${String(start + index).padStart(5)} | ${line}`)
      .join("\n")
    const remaining = lines.length - end
    return {
      ok: true,
      output: `${target}  (lines ${start}-${end} of ${lines.length})\n\n${body}${remaining > 0 ? `\n\n... ${remaining} more lines` : ""}`,
      meta: { lines: lines.length },
    }
  },

  async write_file({ path: relative, content = "" }) {
    const target = resolveInside(relative)
    const existed = fs.existsSync(target)
    const verb = existed ? "Overwrite" : "Create"
    const lines = String(content).split("\n").length
    await fsp.mkdir(path.dirname(target), { recursive: true })
    await fsp.writeFile(target, content, "utf8")
    const bytes = Buffer.byteLength(content, "utf8")
    const preview = String(content).split("\n").slice(0, 40)
      .map((line, index) => `${String(index + 1).padStart(5)} | ${line}`)
      .join("\n")
    const total = String(content).split("\n").length

    return {
      ok: true,
      output: `${existed ? "overwrote" : "created"} ${target}  (${humanSize(bytes)}, ${total} lines)\n\n${preview}${total > 40 ? `\n... ${total - 40} more lines` : ""}`,
      meta: { created: !existed, bytes, changed: true, lines: total, hunks: existed ? 1 : [] },
    }
  },

  async edit_file({ path: relative, old_string, new_string, replace_all = false }) {
    const target = resolveInside(relative)
    const content = await fsp.readFile(target, "utf8")
    if (!content.includes(old_string)) {
      throw new Error(`old_string not found in ${relative}. Read the file again before editing.`)
    }
    const before = content
    const occurrences = content.split(old_string).length - 1
    if (occurrences > 1 && !replace_all) {
      throw new Error(`old_string appears ${occurrences} times in ${relative}; add more context or pass replace_all`)
    }
    const updated = replace_all
      ? content.split(old_string).join(new_string)
      : content.replace(old_string, new_string)
    await fsp.writeFile(target, updated, "utf8")

    const at = updated.indexOf(new_string)
    const context = contextWindow(updated, at < 0 ? 0 : at, 4)

    return {
      ok: true,
      output: `edited ${target}  (${occurrences} replacement${occurrences === 1 ? "" : "s"})\n\n${context.text}`,
      meta: {
        occurrences,
        changed: true,
        startLine: context.startLine,
        endLine: context.endLine,
        hunks: diffHunks(before, updated),
      },
    }
  },

  async delete_path({ path: relative, recursive = false }) {
    const target = resolveInside(relative)
    if (!(await approved("delete_path", `Delete ${relative}${recursive ? " and everything inside it" : ""}`))) {
      return REFUSED(`deleting ${relative}`)
    }
    await fsp.rm(target, { recursive: Boolean(recursive), force: true })
    return { ok: true, output: `deleted ${target}`, meta: {} }
  },

  async make_dir({ path: relative }) {
    const target = resolveInside(relative)
    if (!(await approved("make_dir", `Create the directory ${relative}`))) {
      return REFUSED(`creating ${relative}`)
    }
    await fsp.mkdir(target, { recursive: true })
    return { ok: true, output: `created ${target}`, meta: {} }
  },

  async search({ query, dir = ".", glob = null, limit = 60 }) {
    const target = resolveInside(dir)
    const hits = []
    const pattern = new RegExp(String(query).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i")
    const matcher = glob ? new RegExp(`^${glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*\*/g, ".*").replace(/\*/g, "[^/]*").replace(/\?/g, ".")}$`) : null
    const walk = async (current) => {
      if (hits.length >= Number(limit) * 4) return
      let entries
      try {
        entries = await fsp.readdir(current, { withFileTypes: true })
      } catch {
        return
      }
      for (const entry of entries) {
        if (entry.name === ".git" || entry.name === "node_modules" || entry.name === ".next") continue
        const full = path.join(current, entry.name)
        if (entry.isDirectory()) {
          await walk(full)
          continue
        }
        const rel = path.relative(target, full)
        if (matcher && !matcher.test(rel)) continue
        if (entry.name.endsWith(".pdf") || entry.name.endsWith(".png") || entry.name.endsWith(".jpg")) continue
        let content
        try {
          const stats = await fsp.stat(full)
          if (stats.size > MAX_READ_BYTES) continue
          content = await fsp.readFile(full, "utf8")
        } catch {
          continue
        }
        content.split("\n").forEach((line, index) => {
          if (hits.length < Number(limit) && pattern.test(line)) {
            hits.push(`${rel}:${index + 1}: ${line.trim().slice(0, 200)}`)
          }
        })
      }
    }
    await walk(target)
    const { text, truncated } = clip(hits.join("\n") || "(no matches)")
    return {
      ok: true,
      output: `pattern: ${query}\nsearched: ${target}\nmatches: ${hits.length}${truncated ? "+" : ""}\n\n${text}`,
      meta: { matches: hits.length },
    }
  },

  async read_media({ path: relative, question = null }) {
    const target = resolveInside(relative)
    const stats = await fsp.stat(target)
    if (!stats.isFile()) throw new Error(`${relative} is not a file`)

    const buffer = await fsp.readFile(target)
    const mime = sniff(buffer)
    if (!mime) {
      throw new Error(`${relative} is not a PNG, JPEG, GIF, WebP or PDF`)
    }

    const isPdf = mime === PDF_TYPE
    const limit = isPdf ? MAX_PDF_BYTES : MAX_IMAGE_BYTES
    if (stats.size > limit) {
      throw new Error(`${relative} is ${humanSize(stats.size)}, over the ${humanSize(limit)} limit for ${isPdf ? "PDFs" : "images"}`)
    }

    const base64 = buffer.toString("base64")
    const lines = [
      `${isPdf ? "PDF" : "image"}: ${target}`,
      `type: ${mime}`,
      `size: ${humanSize(stats.size)}`,
    ]

    if (isPdf) {
      const pages = countPdfPages(buffer.toString("latin1"))
      if (pages) lines.push(`pages: ${pages}`)
    } else {
      const size = imageSize(buffer, mime)
      if (size) lines.push(`dimensions: ${size.width}x${size.height}`)
    }

    if (question) lines.push(`you were asked: ${question}`)
    lines.push("", isPdf
      ? "The document is now attached to this conversation. Read it to answer."
      : "The image is now attached to this conversation. Look at it to answer.")

    return {
      ok: true,
      output: lines.join("\n"),
      meta: { mime, bytes: stats.size },
      attachments: [{
        type: isPdf ? "file" : "image_url",
        ...(isPdf
          ? { file: { filename: path.basename(target), fileData: `data:${PDF_TYPE};base64,${base64}` } }
          : { imageUrl: { url: `data:${mime};base64,${base64}` } }),
      }],
    }
  },

  async diff({ path: relative = null, stat_only = false }) {
    const target = relative ? resolveInside(relative) : getRoot()
    const inRepo = Boolean(await git(["rev-parse", "--is-inside-work-tree"], getRoot()))

    if (!inRepo) {
      if (!relative) throw new Error("this workspace is not a git repository, so name a file to diff")
      const after = await readIfPresent(target)
      if (after === null) throw new Error(`${relative} does not exist`)
      const text = syntheticDiff(relative, null, after)
      return {
        ok: true,
        output: stat_only ? relative : text,
        meta: { relative, source: "none", lines: text.split("\n").length },
      }
    }

    const cwd = getRoot()
    const args = relative
      ? ["diff", "HEAD", "--", relative]
      : ["diff", "HEAD"]

    if (relative && !(await isTracked(cwd, relative))) {
      const after = await readIfPresent(target)
      const text = after === null ? "" : syntheticDiff(relative, null, after)
      return {
        ok: true,
        output: stat_only ? `${relative} (untracked)` : text || `${relative} is untracked and identical to empty`,
        meta: { relative, source: "untracked", untracked: true, lines: text.split("\n").length },
      }
    }

    const text = await git(args, cwd)
    if (text === null) throw new Error("git diff failed")

    const numstat = relative
      ? await git(["diff", "--numstat", "HEAD", "--", relative], cwd)
      : await git(["diff", "--numstat", "HEAD"], cwd)

    let added = 0
    let removed = 0
    for (const line of (numstat ?? "").split("\n").filter(Boolean)) {
      const [a, r] = line.split("\t")
      if (a !== "-") added += Number(a) || 0
      if (r !== "-") removed += Number(r) || 0
    }

    if (stat_only) {
      return {
        ok: true,
        output: `${numstat || "no changes"}`.trim(),
        meta: { added, removed, source: "git" },
      }
    }

    if (!text.trim()) {
      return {
        ok: true,
        output: relative
          ? `${relative} has no uncommitted changes.`
          : "no uncommitted changes in the workspace.",
        meta: { added: 0, removed: 0, source: "git", clean: true },
      }
    }

    return {
      ok: true,
      output: text,
      meta: { added, removed, source: "git", lines: text.split("\n").length, relative },
    }
  },

  /** A data URL for the UI to preview, without the model paying for it. */
  async preview_data_url({ path: relative }) {
    const target = resolveInside(relative)
    const stats = await fsp.stat(target)
    if (stats.size > PREVIEW_MAX_BYTES) {
      throw new Error(`${relative} is ${humanSize(stats.size)}, too large to preview`)
    }
    const buffer = await fsp.readFile(target)
    const mime = sniff(buffer)
    if (!mime) throw new Error(`${relative} is not an image or PDF`)
    return {
      ok: true,
      output: `${mime}  ${humanSize(stats.size)}`,
      meta: { mime, bytes: stats.size, name: path.basename(target) },
      preview: { mime, name: path.basename(target), dataUrl: `data:${mime};base64,${buffer.toString("base64")}` },
    }
  },

  async run_command({ command, cwd = ".", timeout_ms = 30000, __callId = null }) {
    const directory = resolveInside(cwd)
    await fsp.access(directory).catch(() => {
      throw new Error(`${cwd} is not a directory`)
    })

    // An agent runs hundreds of commands, so only the ones that can destroy
    // work or exfiltrate data stop for a decision. The rest just run.
    if (isDangerous(command) && !(await approved("run_command", `Run in ${cwd}: ${command}`))) {
      return REFUSED(`running: ${command}`)
    }

    const started = Date.now()
    const timeout = Math.min(Number(timeout_ms) || 30000, 600000)

    return await new Promise((resolvePromise) => {
      const child = spawn(command, { cwd: directory, shell: true, env: process.env })
      let output = ""
      let settled = false
      const emit = typeof sinks.onOutput === "function" && __callId
        ? (chunk) => sinks.onOutput({ callId: __callId, chunk })
        : () => {}
      const append = (chunk) => {
        if (output.length > MAX_OUTPUT) return
        output += chunk
        emit(chunk)
      }
      child.stdout.on("data", append)
      child.stderr.on("data", append)
      const timer = setTimeout(() => {
        if (settled) return
        child.kill("SIGTERM")
        setTimeout(() => child.kill("SIGKILL"), 2000)
      }, timeout)
      child.on("close", (code, signal) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        const duration = Date.now() - started
        const timedOut = duration >= timeout - 50
        const { text, truncated } = clip(output.trim() || "(no output)")
        resolvePromise({
          ok: code === 0 && !timedOut,
          output:
            `$ ${command}\ncwd: ${directory}\n` +
            `status: ${timedOut ? `timed out after ${timeout}ms and was killed` : `exit ${code ?? "none"}${signal ? ` signal ${signal}` : ""}`} in ${duration}ms\n\n${text}`,
          meta: { exitCode: code, signal, durationMs: duration, timedOut, truncated, cwd: directory },
        })
      })
    })
  },

  async environment({}) {
    return {
      ok: true,
      output: [
        `workspace: ${root}`,
        `platform:   ${os.platform()} ${os.release()}`,
        `node:       ${process.version}`,
        `cwd:        ${process.cwd()}`,
        `home:       ${os.homedir()}`,
      ].join("\n"),
      meta: { root },
    }
  },
}

async function runTool(name, args) {
  const tool = tools[name]
  if (!tool) return { ok: false, output: `Unknown tool: ${name}`, meta: {} }
  try {
    return await tool(args ?? {})
  } catch (error) {
    if (error instanceof JailError) {
      return { ok: false, output: `Refused: ${error.message}`, meta: { jailed: true } }
    }
    return { ok: false, output: `${name} failed: ${error.message}`, meta: {} }
  }
}

/** What the model is allowed to call. preview_data_url is for the UI only. */
const MODEL_TOOLS = [
  "list_dir", "read_file", "write_file", "edit_file", "delete_path", "make_dir",
  "search", "read_media", "diff", "run_command", "environment",
]

module.exports = {
  tools,
  MODEL_TOOLS,
  runTool,
  setRoot,
  getRoot,
  setSinks,
  setConfirming,
  isDangerous,
  trusted,
}
