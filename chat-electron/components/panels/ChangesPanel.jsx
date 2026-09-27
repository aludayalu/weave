"use client";

import { useEffect, useState } from "react";
import { MultiFileDiff } from "@pierre/diffs/react";
import { RotateCcw, Check, X, FileCode2 } from "lucide-react";
import { loadVersions, revertChange, useLiveChanges } from "@/lib/ai/pendingChanges";
import { usePanel } from "./panelState";

/**
 * The agent's changes since it started, with a real diff per file. Kept
 * read only on purpose: this is the review surface, and reverting is an
 * explicit act rather than something a stray keystroke can do.
 */
export function ChangesPanel() {
    const changes = useLiveChanges();
    const { focusPath, setFocusPath, close } = usePanel();
    const [selected, setSelected] = useState(null);
    const [versions, setVersions] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);

    const active = selected ?? focusPath ?? changes[0]?.path ?? null;

    useEffect(() => {
        if (!active) {
            setVersions(null);
            return;
        }

        let alive = true;
        setLoading(true);
        setError(null);

        loadVersions(active)
            .then((result) => {
                if (!alive) return;
                if (!result?.ok) {
                    setError(result?.output ?? "could not read that file");
                    setVersions(null);
                    return;
                }
                setVersions(result.versions);
            })
            .catch((e) => alive && setError(String(e)))
            .finally(() => alive && setLoading(false));

        return () => { alive = false };
    }, [active, changes.length]);

    if (changes.length === 0) {
        return (
            <div className="flex-1 flex flex-col items-center justify-center gap-2 px-8 text-center">
                <Check size={20} className="opacity-30" />
                <div className="text-[13px] opacity-50">No changes to review</div>
                <div className="text-[12px] opacity-30">
                    Anything the agent writes shows up here with a diff, and can be put back.
                </div>
            </div>
        );
    }

    return (
        <div className="flex-1 min-h-0 flex flex-col">
            <div className="shrink-0 border-b border-white/10 flex items-center gap-1 px-2 py-1.5 overflow-x-auto">
                {changes.map((change) => (
                    <button
                        key={change.path}
                        onMouseDown={() => setSelected(change.path)}
                        className={`shrink-0 flex items-center gap-1.5 px-2 py-1 rounded text-[11px] max-w-[190px] ${
                            active === change.path
                                ? "bg-white/12 text-white/90"
                                : "text-white/50 hover:bg-white/5"
                        }`}
                        title={change.path}
                    >
                        <FileCode2 size={11} className="shrink-0 opacity-60" />
                        <span className="truncate">{change.path.split("/").pop()}</span>
                        {change.additions > 0 && <span className="text-green-500/80 shrink-0">+{change.additions}</span>}
                        {change.deletions > 0 && <span className="text-red-400/80 shrink-0">-{change.deletions}</span>}
                    </button>
                ))}
            </div>

            <div className="flex-1 min-h-0 overflow-auto">
                {loading && <div className="p-4 text-[13px] opacity-40">loading diff…</div>}
                {error && <div className="p-4 text-[13px] text-red-400/80">{error}</div>}

                {versions && !loading && (
                    <div className="p-3">
                        <div className="flex items-center gap-2 mb-2 px-1">
                            <span className="text-[12px] opacity-60 font-mono truncate">{active}</span>
                            <span className="flex-1" />
                            <button
                                onMouseDown={async (e) => {
                                    e.preventDefault();
                                    await revertChange(active);
                                    setSelected(null);
                                }}
                                className="shrink-0 flex items-center gap-1.5 px-2 py-1 rounded text-[11px] bg-white/8 hover:bg-white/15"
                            >
                                <RotateCcw size={11} />
                                Revert this file
                            </button>
                        </div>

                        {versions.before === versions.after ? (
                            <div className="text-[12px] opacity-40 px-1">no differences</div>
                        ) : (
                            <div className="rounded-lg overflow-hidden border border-white/10">
                                <MultiFileDiff
                                    oldFile={{ name: versions.name, contents: versions.before }}
                                    newFile={{ name: versions.name, contents: versions.after }}
                                    options={{
                                        theme: "pierre-dark",
                                        diffStyle: "unified",
                                        diffIndicators: "bar",
                                        lineDiffType: "word",
                                        hunkSeparators: "metadata",
                                        overflow: "scroll",
                                        themeType: "dark",
                                    }}
                                />
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
