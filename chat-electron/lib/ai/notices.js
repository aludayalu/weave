"use client";

/**
 * Things that happened outside a turn which the model must know about.
 *
 * If the user reverts one of the agent's edits, the agent's picture of the file
 * is now wrong. Rather than let it carry on against a stale assumption, the
 * revert is queued here and injected into the next turn, with the file's current
 * contents so the model can see the new state for itself.
 */
let queue = []

const MAX_PREVIEW_LINES = 200

async function describe(path) {
    const result = await window.desktop.files.read(path)
    if (!result?.ok) return `${path} no longer exists.`

    const lines = result.contents.split("\n")
    const shown = lines.slice(0, MAX_PREVIEW_LINES).map((line, index) => `${String(index + 1).padStart(5)} | ${line}`).join("\n")

    return [
        `${path} now contains ${lines.length} lines:`,
        "",
        shown,
        lines.length > MAX_PREVIEW_LINES ? `\n... ${lines.length - MAX_PREVIEW_LINES} more lines` : "",
    ].filter((part) => part !== "").join("\n")
}

/** Call after a revert succeeds. `what` describes what the user did. */
export async function noticeRevert(path, { revertedTo } = {}) {
    const state = await describe(path)
    queue.push(
        `The user reverted your change to ${path}. ` +
        (revertedTo ? `It was restored to ${revertedTo}. ` : "") +
        `Your copy of this file is stale. This is how it looks now:\n\n${state}`
    )
    return queue.length
}

/** Call when the user accepts the changes, so the model knows they are final. */
export function noticeKept(paths) {
    if (!paths?.length) return
    queue.push(
        `The user accepted your changes to ${paths.join(", ")}. They are now the intended state, ` +
        `so do not try to redo or revert them.`
    )
}

export function takeNotices() {
    if (queue.length === 0) return []
    const out = queue
    queue = []
    return out
}

export function peekNotices() {
    return [...queue]
}
