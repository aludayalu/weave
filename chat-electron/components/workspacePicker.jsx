"use client";

import { FolderOpen, ShieldCheck, ShieldOff } from "lucide-react";
import { useEffect, useState } from "react";

const STORAGE_KEY = "workspace_root";

/**
 * The folder the agent is allowed to touch. Lives in the sidebar footer so it is
 * always visible, and persists across restarts.
 */
export function WorkspacePicker() {
    const [root, setRoot] = useState(null);
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        if (typeof window === "undefined" || !window.desktop) return;

        const saved = localStorage.getItem(STORAGE_KEY);
        const restore = saved
            ? window.desktop.workspace.set(saved)
            : window.desktop.workspace.get();

        restore.then((result) => setRoot(result.root)).catch(() => {});
    }, []);

    async function choose() {
        if (!window.desktop) return;
        setBusy(true);
        try {
            const result = await window.desktop.workspace.choose();
            if (result.canceled) return;
            localStorage.setItem(STORAGE_KEY, result.root);
            setRoot(result.root);
            window.dispatchEvent(new CustomEvent("workspace-changed", { detail: result.root }));
        } finally {
            setBusy(false);
        }
    }

    if (!root) return null;

    return (
        <button
            onMouseDown={choose}
            className="titlebar-no-drag flex items-center gap-2 w-full px-3 py-2 text-left hover:bg-white/5 cursor-pointer group"
            title={root}
        >
            <FolderOpen size={13} className="shrink-0 opacity-40 group-hover:opacity-70" />
            <div className="min-w-0 flex-1">
                <div className="text-[10px] uppercase tracking-wider opacity-35 leading-none mb-1">Workspace</div>
                <div className="text-[12px] opacity-70 truncate leading-none">
                    {busy ? "choosing…" : root.split("/").filter(Boolean).pop() || root}
                </div>
            </div>
        </button>
    );
}

/**
 * When on, anything that changes the machine or deletes data stops and waits
 * for a human. Reading and running safe commands are never interrupted.
 */
export function ApprovalToggle() {
    const [confirming, setConfirming] = useState(true);

    useEffect(() => {
        const saved = localStorage.getItem("confirm_changes")
        setConfirming(saved === null ? true : saved === "true")
    }, [])

    function toggle() {
        const next = !confirming
        setConfirming(next)
        localStorage.setItem("confirm_changes", String(next))
        window.dispatchEvent(new CustomEvent("approval-mode-changed", { detail: next }))
    }

    return (
        <button
            onMouseDown={toggle}
            className="titlebar-no-drag flex items-center gap-2 w-full px-3 py-2 text-left hover:bg-white/5 cursor-pointer group"
            title={confirming ? "The agent asks before changing anything" : "The agent may change anything without asking"}
        >
            {confirming
                ? <ShieldCheck size={13} className="shrink-0 text-green-500/70" />
                : <ShieldOff size={13} className="shrink-0 text-amber-500/70" />}
            <div className="min-w-0 flex-1">
                <div className="text-[10px] uppercase tracking-wider opacity-35 leading-none mb-1">Confirm changes</div>
                <div className="text-[12px] opacity-70 leading-none">{confirming ? "on" : "off"}</div>
            </div>
        </button>
    )
}
