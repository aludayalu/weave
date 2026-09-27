"use client";

import { useEffect, useState } from "react";
import { noticeKept, noticeRevert } from "./notices";

/**
 * The agent's uncommitted changes, mirrored from the main process. Edits land on
 * disk straight away, so this is the record of what it touched and the handle for
 * putting any of it back.
 */
let changes = []
let loading = false
let started = false
const listeners = new Set()

function publish() {
  for (const listener of listeners) listener(changes)
}

export async function refreshChanges() {
  if (typeof window === "undefined" || !window.desktop) return []
  if (loading) return changes
  loading = true
  try {
    const result = await window.desktop.changes.list()
    changes = result?.changes ?? []
    publish()
    return changes
  } finally {
    loading = false
  }
}

export function startChangesBridge() {
  if (started || typeof window === "undefined" || !window.desktop) return
  started = true
  refreshChanges()
  window.desktop.changes.onUpdate(() => refreshChanges())
  window.addEventListener("workspace-changed", () => refreshChanges())
}

export function useChanges() {
  const [value, setValue] = useState(changes)

  useEffect(() => {
    const listener = (next) => setValue(next)
    listeners.add(listener)
    setValue(changes)
    return () => listeners.delete(listener)
  }, [])

  return value
}

/** Only the entries that still differ from their snapshot. */
export function useLiveChanges() {
  const all = useChanges()
  return all.filter((change) => !change.unchanged)
}

export async function revertChange(path) {
  const result = await window.desktop.changes.revert(path)
  if (result?.ok) await noticeRevert(path)
  await refreshChanges()
  return result
}

export async function revertEverything() {
  const result = await window.desktop.changes.revertAll()
  for (const path of result?.reverted ?? []) await noticeRevert(path)
  await refreshChanges()
  return result
}

export async function keepEverything() {
  const result = await window.desktop.changes.keepAll()
  noticeKept(result?.kept ?? [])
  await refreshChanges()
  return result
}

export async function loadVersions(path) {
  return await window.desktop.changes.versions(path)
}
