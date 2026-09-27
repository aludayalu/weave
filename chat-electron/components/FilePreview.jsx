"use client";

import { useEffect, useState } from "react";
import { X, FileText } from "lucide-react";

/** Chunked so a large file does not blow the argument limit. */
function toDataUrl(bytes, mime) {
    if (!bytes || !bytes.length) return null;
    if (typeof bytes === "string") return bytes.startsWith("data:") ? bytes : null;

    let binary = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return `data:${mime || "application/octet-stream"};base64,${btoa(binary)}`;
}

export function useFileUrl(file) {
    const [url, setUrl] = useState(() => file?.dataUrl ?? null);

    useEffect(() => {
        if (file?.dataUrl) { setUrl(file.dataUrl); return }
        if (file?.url) { setUrl(file.url); return }
        setUrl(toDataUrl(file?.data, file?.type));
    }, [file]);

    return url;
}

export function isImage(file) {
    return Boolean(file?.type?.startsWith("image/") || file?.mime?.startsWith("image/"))
}

export function isPdf(file) {
    return file?.type === "application/pdf" || file?.mime === "application/pdf"
        || String(file?.name || "").toLowerCase().endsWith(".pdf")
}

function humanSize(bytes) {
    if (!bytes) return ""
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++ }
    return `${value.toFixed(unit === 0 ? 0 : 1)}${units[unit]}`
}

/**
 * The one preview surface: an inline thumbnail for images, a tappable tile for
 * everything else, and a full screen modal for either.
 */
export function FilePreview({ file, size = "sm", onClick, children }) {
    const url = useFileUrl(file)
    const [open, setOpen] = useState(false)

    const image = isImage(file) && url
    const pdf = !image && isPdf(file) && url

    function show() {
        if (url) setOpen(true)
        onClick?.()
    }

    const TILE = "h-16 w-16"

    return (
        <>
            {children ? (
                <div onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); show() }} className="cursor-pointer">
                    {children}
                </div>
            ) : image ? (
                <button
                    onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); show() }}
                    title={`Open ${file.name}`}
                    className={`${TILE} shrink-0 rounded-lg overflow-hidden border border-white/15 hover:border-white/40 transition-colors`}
                >
                    <img src={url} alt={file.name} className="w-full h-full object-cover" />
                </button>
            ) : (
                <button
                    onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); show() }}
                    title={`${file.name}${file.size ? ` · ${humanSize(file.size)}` : ""}`}
                    className="h-16 w-16 shrink-0 flex flex-col items-center justify-center gap-1 rounded-lg border border-white/15 hover:border-white/35 transition-colors overflow-hidden"
                >
                    <FileText size={18} className="shrink-0 opacity-40" />
                    <span className="text-[9px] uppercase tracking-wide opacity-55 leading-none">
                        {(file.name?.split(".").pop() || "file").slice(0, 4)}
                    </span>
                    {file.size ? (
                        <span className="text-[9px] opacity-30 leading-none">{humanSize(file.size)}</span>
                    ) : null}
                </button>
            )}

            {open && url && (
                <PreviewModal url={url} name={file.name} pdf={pdf} onClose={() => setOpen(false)} />
            )}
        </>
    )
}

export function PreviewModal({ url, name, pdf, onClose }) {
    const [fit, setFit] = useState(null)

    useEffect(() => {
        const onKey = (e) => { if (e.key === "Escape") onClose() }
        window.addEventListener("keydown", onKey)
        return () => window.removeEventListener("keydown", onKey)
    }, [onClose])

    return (
        <div
            className="fixed inset-0 z-[100] bg-black/85 backdrop-blur-sm flex items-center justify-center"
            onMouseDown={onClose}
        >
            <button
                onMouseDown={onClose}
                className="absolute top-5 right-5 p-2 rounded-full bg-white/10 hover:bg-white/25 z-10"
                aria-label="close"
            >
                <X size={18} />
            </button>

            {pdf ? (
                <iframe
                    title={name}
                    src={url}
                    className="w-[92vw] h-[88vh] bg-white rounded-lg"
                    onMouseDown={(e) => e.stopPropagation()}
                />
            ) : (
                <img
                    src={url}
                    alt={name}
                    style={fit}
                    className="max-w-[92vw] max-h-[88vh] object-contain rounded-lg"
                    onLoad={(e) => {
                        const img = e.currentTarget
                        const scale = Math.min(
                            (window.innerHeight * 0.86) / img.naturalHeight,
                            (window.innerWidth * 0.9) / img.naturalWidth,
                            4
                        )
                        if (scale > 1) setFit({ width: img.naturalWidth * scale, height: img.naturalHeight * scale })
                    }}
                    onMouseDown={(e) => e.stopPropagation()}
                />
            )}
        </div>
    )
}
