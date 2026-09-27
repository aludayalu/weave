"use client";

import { useState } from "react";
import { GitCompare, RotateCcw, Check, ChevronRight } from "lucide-react";
import { usePanel } from "./panels/panelState";
import { keepEverything, revertEverything, useLiveChanges } from "@/lib/ai/pendingChanges";

/**
 * Sits directly over the composer. Edits are already on disk, so this is not a
 * blocking gate; it is the standing offer to look at what changed, keep it, or
 * put it back.
 */
export function PendingBar() {
    const changes = useLiveChanges();
    const { showChanges } = usePanel();
    const [busy, setBusy] = useState(null);

    if (changes.length === 0) return null;

    const additions = changes.reduce((sum, c) => sum + c.additions, 0);
    const deletions = changes.reduce((sum, c) => sum + c.deletions, 0);

    return (
        <div className="shrink-0 w-full pointer-events-auto">
            <div className="mx-auto max-w-[720px]">
                <div
                    onMouseDown={() => showChanges()}
                    title="Open the diff"
                    className="flex items-center gap-2 px-3 h-9 rounded-lg bg-[#161618] border border-white/12 text-[12px] shadow-lg cursor-pointer hover:border-white/25 hover:bg-[#1b1b1e] transition-colors"
                >
                    <div className="flex items-center gap-2 min-w-0">
                        <GitCompare size={12} className="shrink-0 opacity-60" />
                        <span className="shrink-0 opacity-75">
                            {changes.length} file{changes.length === 1 ? "" : "s"} changed
                        </span>
                        <span className="shrink-0 font-mono">
                            {additions > 0 && <span className="text-green-500/85">+{additions}</span>}
                            {deletions > 0 && <span className="text-red-400/85 ml-1.5">-{deletions}</span>}
                        </span>
                        <ChevronRight size={12} className="shrink-0 opacity-35" />
                    </div>

                    <span className="flex-1" />

                    <button
                        onMouseDown={async (e) => { e.stopPropagation(); setBusy("revert"); await revertEverything(); setBusy(null) }}
                        disabled={busy !== null}
                        className="shrink-0 flex items-center gap-1 px-2 h-6 rounded text-[11px] text-white/55 hover:bg-white/10 disabled:opacity-30"
                    >
                        <RotateCcw size={11} />
                        Revert all
                    </button>
                    <button
                        onMouseDown={async (e) => { e.stopPropagation(); setBusy("keep"); await keepEverything(); setBusy(null) }}
                        disabled={busy !== null}
                        className="shrink-0 flex items-center gap-1 px-2 h-6 rounded text-[11px] text-white/55 hover:bg-white/10 disabled:opacity-30"
                    >
                        <Check size={11} />
                        Keep
                    </button>
                </div>
            </div>
        </div>
    )
}
