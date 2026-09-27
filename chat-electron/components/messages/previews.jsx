"use client";

import { useEffect, useState } from "react";
import { X, Image as ImageIcon, FileText } from "lucide-react";

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
