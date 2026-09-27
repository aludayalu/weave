"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Live stdout and stderr for in-flight shell commands, keyed by tool call id.
 * The main process pushes chunks as they arrive; the tool row subscribes to the
 * one it cares about and shows output while the command is still running.
 */
const chunks = new Map()
const listeners = new Set()

let started = false

function publish(callId) {
  for (const listener of listeners) listener(callId, chunks.get(callId) ?? "")
}

export function startCommandStream() {
  if (started || typeof window === "undefined" || !window.desktop) return
  started = true

  window.desktop.onCommandOutput(({ callId, chunk }) => {
    const existing = chunks.get(callId) ?? ""
    // keep the tail only; these blobs never need to grow without bound
    const next = (existing + chunk).slice(-20000)
    chunks.set(callId, next)
    publish(callId)
  })
}

export function useCommandOutput(callId) {
  const [text, setText] = useState(() => chunks.get(callId) ?? "")

  useEffect(() => {
    if (!callId) return
    setText(chunks.get(callId) ?? "")

    const listener = (id, value) => {
      if (id === callId) setText(value)
    }
    listeners.add(listener)
    return () => listeners.delete(listener)
  }, [callId])

  return text
}

/** Forget a finished call so long sessions do not hold on to every output. */
export function releaseCommandOutput(callId) {
  chunks.delete(callId)
}
