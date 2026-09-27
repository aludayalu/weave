"use client";

import { useEffect, useState } from "react";
import { X, FileDiff, Image as ImageIcon, FileText } from "lucide-react";

/**
 * Click a diff badge to see everything that has changed in that file since the
 * last commit, not just the edit you happen to be looking at.
 */
export function DiffButton({ path }) {
    const [open, setOpen] = useState(false);

    if (!path) return null;

    return (
        <>
            <button
                onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); setOpen(true) }}
                title={`See every change to ${path} since the last commit`}
                className="shrink-0 opacity-0 group-hover:opacity-60 hover:!opacity-100 transition-opacity flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded bg-white/10 hover:bg-white/20"
            >
                <FileDiff size={11} />
                diff
            </button>

            {open && <DiffModal path={path} onClose={() => setOpen(false)} />}
        </>
    );
}

function DiffModal({ path, onClose }) {
    const [state, setState] = useState({ loading: true, diff: "", error: null, meta: null });

    useEffect(() => {
        let alive = true;

        (async () => {
            try {
                const result = await window.desktop.tools.run("diff", { path });
                if (!alive) return;
                setState({
                    loading: false,
                    diff: String(result.output ?? ""),
                    error: result.ok === false ? result.output : null,
                    meta: result.meta ?? null,
                });
            } catch (error) {
                if (alive) setState({ loading: false, diff: "", error: String(error), meta: null });
            }
        })();

        return () => { alive = false };
    }, [path]);

    useEffect(() => {
        const onKey = (e) => { if (e.key === "Escape") onClose() };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
    }, [onClose]);

    return (
        <div
            className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-8"
            onMouseDown={onClose}
        >
            <div
                className="bg-[#0d0d0f] rounded-lg border border-white/15 w-full max-w-4xl max-h-full flex flex-col overflow-hidden"
                onMouseDown={(e) => e.stopPropagation()}
            >
                <div className="flex items-center gap-3 px-4 h-11 shrink-0 border-b border-white/10">
                    <FileDiff size={14} className="opacity-50 shrink-0" />
                    <span className="text-sm opacity-80 truncate min-w-0">{path}</span>
                    {state.meta && (state.meta.added > 0 || state.meta.removed > 0) && (
                        <span className="shrink-0 font-mono text-[11px]">
                            <span className="text-green-500">+{state.meta.added}</span>
                            <span className="text-red-500 ml-2">-{state.meta.removed}</span>
                        </span>
                    )}
                    <span className="flex-1" />
                    <span className="text-[11px] opacity-35 shrink-0 hidden sm:block">esc to close</span>
                    <button onMouseDown={onClose} className="shrink-0 opacity-50 hover:opacity-100 p-1">
                        <X size={15} />
                    </button>
                </div>

                <div className="flex-1 min-h-0 overflow-auto p-4">
                    {state.loading && <div className="text-sm opacity-40">loading diff…</div>}
                    {state.error && <div className="text-sm text-red-400/80 whitespace-pre-wrap">{state.error}</div>}
                    {!state.loading && !state.error && !state.diff.trim() && (
                        <div className="text-sm opacity-40">no changes since the last commit</div>
                    )}
                    {state.diff && <DiffBody diff={state.diff} />}
                </div>
            </div>
        </div>
    );
}

function DiffBody({ diff }) {
    return (
        <pre className="font-mono text-[12px] leading-relaxed whitespace-pre-wrap break-words">
            {diff.split("\n").map((line, index) => {
                let className = "opacity-55";
                if (line.startsWith("+++") || line.startsWith("---")) className = "opacity-40";
                else if (line.startsWith("@@")) className = "text-cyan-400/60";
                else if (line.startsWith("+")) className = "text-green-400/85 bg-green-500/5";
                else if (line.startsWith("-")) className = "text-red-400/85 bg-red-500/5";
                return (
                    <div key={index} className={className}>
                        {line || " "}
                    </div>
                );
            })}
        </pre>
    );
}

/**
 * Images get an inline thumbnail, everything else a tappable tile. Either way
 * clicking opens the real thing.
 */
export function MediaPreview({ path, mime, size }) {
    const [url, setUrl] = useState(null);
    const [open, setOpen] = useState(false);
    const [failed, setFailed] = useState(false);

    useEffect(() => {
        if (!path) return;
        let alive = true;

        (async () => {
            try {
                const result = await window.desktop.tools.run("preview_data_url", { path });
                if (!alive || !result.preview) return setFailed(true);
                setUrl(result.preview.dataUrl);
                if (!mime) setMime(result.preview.mime);
            } catch {
                if (alive) setFailed(true)
            }
        })();

        return () => { alive = false };
    }, [path]);

    const isImage = mime?.startsWith("image/");

    if (failed) return null;

    if (isImage && url) {
        return (
            <>
                <button
                    onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); setOpen(true) }}
                    className="ml-[20px] mt-1.5 mb-0.5 self-start rounded-md overflow-hidden border border-white/10 hover:border-white/25 transition-colors"
                    title="Open full size"
                >
                    <img src={url} alt={path} className="max-h-40 max-w-[260px] object-contain block" />
                </button>

                {open && (
                    <div
                        className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center p-10"
                        onMouseDown={() => setOpen(false)}
                    >
                        <img
                            src={url}
                            alt={path}
                            className="max-w-full max-h-full object-contain"
                            onMouseDown={(e) => e.stopPropagation()}
                        />
                        <button
                            onMouseDown={() => setOpen(false)}
                            className="absolute top-5 right-5 p-2 rounded-full bg-white/10 hover:bg-white/20"
                        >
                            <X size={18} />
                        </button>
                    </div>
                )}
            </>
        );
    }

    return (
        <>
            <button
                onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); setOpen(true) }}
                className="ml-[20px] mt-1.5 mb-1 self-start flex items-center gap-2 px-2.5 py-1.5 rounded-md bg-white/5 hover:bg-white/10 border border-white/10 max-w-[280px]"
            >
                {mime === "application/pdf" || path?.endsWith(".pdf")
                    ? <FileText size={13} className="shrink-0 opacity-50" />
                    : <ImageIcon size={13} className="shrink-0 opacity-50" />}
                <span className="text-[12px] opacity-70 truncate">{path?.split("/").pop()}</span>
                <span className="text-[11px] opacity-35 shrink-0">{size}</span>
            </button>

            {open && url && (
                <div
                    className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center p-8"
                    onMouseDown={() => setOpen(false)}
                >
                    <iframe
                        title={path}
                        src={url}
                        className="w-full h-full max-w-5xl bg-white rounded-lg"
                        onMouseDown={(e) => e.stopPropagation()}
                    />
                    <button
                        onMouseDown={() => setOpen(false)}
                        className="absolute top-5 right-5 p-2 rounded-full bg-white/10 hover:bg-white/20"
                    >
                        <X size={18} />
                    </button>
                </div>
            )}
        </>
    );
}
