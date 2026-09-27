"use client";

import { useEffect } from "react";
import { X, GitCompare, FileCode2, RotateCcw, Check } from "lucide-react";
import { ChangesPanel } from "./ChangesPanel";
import { FilePanel } from "./FilePanel";
import { usePanel } from "./panelState";
import { useLiveChanges, keepEverything, revertEverything } from "@/lib/ai/pendingChanges";

/** The docked right hand panel: changes on one tab, an open file on the other. */
export function SidePanel() {
    const { tab, setTab, close, width, setWidth, isOpen } = usePanel();
    const changes = useLiveChanges();

    useEffect(() => {
        function onKey(e) {
            if (e.key === "Escape" && isOpen) close();
        }
        window.addEventListener("keydown", onKey)
        return () => window.removeEventListener("keydown", onKey)
    }, [close, isOpen])

    if (!isOpen) return null

    function startResize(e) {
        e.preventDefault()
        const startX = e.clientX
        const startWidth = width

        function onMove(move) {
            const next = Math.min(Math.max(startWidth - (move.clientX - startX), 320), window.innerWidth - 420)
            setWidth(next)
        }
        function onUp() {
            window.removeEventListener("mousemove", onMove)
            window.removeEventListener("mouseup", onUp)
            document.body.style.cursor = ""
        }

        document.body.style.cursor = "col-resize"
        window.addEventListener("mousemove", onMove)
        window.addEventListener("mouseup", onUp)
    }

    return (
        <div className="relative shrink-0 h-full flex flex-col min-h-0" style={{ width }}>
            {/* the divider: a real grabbable edge between chat and panel */}
            <div
                onMouseDown={startResize}
                className="absolute top-0 bottom-0 z-30 cursor-col-resize"
                style={{ left: -3, width: 7 }}
            >
                <div className="absolute top-0 bottom-0 left-1/2 w-px -translate-x-1/2 bg-white/15" />
                <div className="absolute inset-y-0 left-0 right-0 opacity-0 hover:opacity-100 transition-opacity bg-[#6b8e23]/25" />
            </div>
            <div className="flex-1 min-h-0 flex flex-col border-l border-white/12">

            <div className="shrink-0 h-11 flex items-center gap-1 px-2 border-b border-white/10">
                <button
                    onMouseDown={() => setTab("changes")}
                    className={`flex items-center gap-1.5 h-7 px-2.5 rounded text-[12px] ${
                        tab === "changes" ? "bg-white/12 text-white/90" : "text-white/50 hover:bg-white/5"
                    }`}
                >
                    <GitCompare size={12} />
                    Changes
                    {changes.length > 0 && (
                        <span className="ml-0.5 px-1.5 rounded-full bg-[#6b8e23] text-white text-[10px] leading-4">
                            {changes.length}
                        </span>
                    )}
                </button>

                <button
                    onMouseDown={() => setTab("file")}
                    className={`flex items-center gap-1.5 h-7 px-2.5 rounded text-[12px] ${
                        tab === "file" ? "bg-white/12 text-white/90" : "text-white/50 hover:bg-white/5"
                    }`}
                >
                    <FileCode2 size={12} />
                    File
                </button>

                <span className="flex-1" />

                {tab === "changes" && changes.length > 0 && (
                    <>
                        <button
                            onMouseDown={() => revertEverything()}
                            title="Put every change back"
                            className="flex items-center gap-1 h-7 px-2 rounded text-[11px] text-white/55 hover:bg-white/8"
                        >
                            <RotateCcw size={11} />
                            Revert all
                        </button>
                        <button
                            onMouseDown={() => keepEverything()}
                            title="Accept the changes; they can no longer be reverted"
                            className="flex items-center gap-1 h-7 px-2 rounded text-[11px] text-white/55 hover:bg-white/8"
                        >
                            <Check size={11} />
                            Keep
                        </button>
                    </>
                )}

                <button onMouseDown={close} className="p-1.5 rounded hover:bg-white/8 opacity-50 hover:opacity-100">
                    <X size={14} />
                </button>
            </div>

            {tab === "changes" ? <ChangesPanel /> : <FilePanel />}
            </div>
        </div>
    )
}
