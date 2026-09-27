"use client";

import { useEffect, useState } from "react";

/**
 * Collects approval requests coming from the main process so tool rows can
 * render them inline, and forwards the user's verdict back.
 */
const requests = new Map()
const listeners = new Set()

let started = false

function publish() {
  for (const listener of listeners) listener()
}

export function startApprovalBridge() {
  if (started || typeof window === "undefined" || !window.desktop) return
  started = true

  window.desktop.approvals.onRequest((request) => {
    requests.set(request.id, { ...request, pending: true })
    publish()
  })

  const saved = localStorage.getItem("confirm_changes")
  window.desktop.approvals.setMode(saved === null ? true : saved === "true")

  window.addEventListener("approval-mode-changed", (event) => {
    window.desktop.approvals.setMode(event.detail)
  })
}

export function respond(id, verdict) {
  requests.delete(id)
  window.desktop.approvals.respond(id, verdict)
  publish()
}

export function useApprovals() {
  const [, force] = useState(0)

  useEffect(() => {
    const listener = () => force((n) => n + 1)
    listeners.add(listener)
    return () => listeners.delete(listener)
  }, [])

  return [...requests.values()]
}
