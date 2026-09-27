"use client";

import { useEffect, useRef, useState } from "react";
import { EditorState, Compartment } from "@codemirror/state";
import { EditorView, keymap, lineNumbers, highlightActiveLine, drawSelection, highlightActiveLineGutter, rectangularSelection, crosshairCursor } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { bracketMatching, indentOnInput, syntaxHighlighting, defaultHighlightStyle } from "@codemirror/language";
import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { oneDark } from "@codemirror/theme-one-dark";
import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { json } from "@codemirror/lang-json";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { markdown } from "@codemirror/lang-markdown";
import { rust } from "@codemirror/lang-rust";
import { go } from "@codemirror/lang-go";
import { RotateCcw, Check, Loader2 } from "lucide-react";
import { usePanel } from "./panelState";

/** Language by extension, so the highlighting is right without a server round trip. */
function languageFor(path) {
    const ext = String(path).split(".").pop()?.toLowerCase();
    switch (ext) {
        case "js": case "jsx": case "mjs": case "cjs": case "ts": case "tsx": return javascript()
        case "py": return python()
        case "json": return json()
        case "html": case "htm": case "jsx2": return html()
        case "css": case "scss": case "less": return css()
        case "md": case "mdx": return markdown()
        case "rs": return rust()
        case "go": return go()
        default: return []
    }
}

const theme = EditorView.theme({
    "&": { height: "100%", fontSize: "12.5px", backgroundColor: "transparent" },
    ".cm-scroller": { fontFamily: "var(--font-geist-mono), ui-monospace, monospace", lineHeight: "1.6" },
    ".cm-gutters": { backgroundColor: "transparent", borderRight: "1px solid rgba(255,255,255,0.07)", color: "rgba(255,255,255,0.25)" },
    ".cm-activeLine": { backgroundColor: "rgba(255,255,255,0.035)" },
    ".cm-activeLineGutter": { backgroundColor: "rgba(255,255,255,0.05)", color: "rgba(255,255,255,0.5)" },
    "&.cm-focused": { outline: "none" },
}, { dark: true })

/**
 * A file, open in the panel and editable. Saving writes straight to disk through
 * the main process, which snapshots the previous contents first, so a save from
 * here is revertible exactly like an edit made by the agent.
 */
export function FilePanel() {
    const { filePath, setFilePath, close } = usePanel();
    const host = useRef(null);
    const view = useRef(null);
    const [state, setState] = useState("loading");
    const [dirty, setDirty] = useState(false);
    const [savedAt, setSavedAt] = useState(null);
    const language = useRef(new Compartment());

    useEffect(() => {
        if (!filePath || !host.current) return
        let cancelled = false

        setState("loading")
        setDirty(false)
        setSavedAt(null)

        window.desktop.files.read(filePath)
            .then((result) => {
                if (cancelled) return
                if (!result?.ok) {
                    setState("error")
                    return
                }

                const state_ = EditorState.create({
                    doc: result.contents,
                    extensions: [
                        lineNumbers(),
                        highlightActiveLineGutter(),
                        highlightActiveLine(),
                        drawSelection(),
                        rectangularSelection(),
                        crosshairCursor(),
                        highlightSelectionMatches(),
                        history(),
                        bracketMatching(),
                        closeBrackets(),
                        indentOnInput(),
                        syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
                        language.current.of(languageFor(filePath)),
                        oneDark,
                        theme,
                        EditorView.lineWrapping,
                        keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
                        EditorView.updateListener.of((update) => {
                            if (!update.docChanged) return
                            setDirty(true)
                        }),
                    ],
                })

                view.current?.destroy()
                view.current = new EditorView({ state: state_, parent: host.current })
                setState("ready")
            })
            .catch(() => !cancelled && setState("error"))

        return () => {
            cancelled = true
            view.current?.destroy()
            view.current = null
        }
    }, [filePath])

    async function save() {
        if (!view.current || !filePath) return
        setState("saving")
        const contents = view.current.state.doc.toString()
        const result = await window.desktop.files.write(filePath, contents)
        if (result?.ok) {
            setDirty(false)
            setSavedAt(Date.now())
            setState("ready")
        } else {
            setState("error")
        }
    }

    // ctrl/cmd+s saves, the way any editor does
    useEffect(() => {
        function onKey(e) {
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
                e.preventDefault()
                if (dirty) save()
            }
        }
        window.addEventListener("keydown", onKey)
        return () => window.removeEventListener("keydown", onKey)
    }, [dirty, filePath])

    if (!filePath) {
        return (
            <div className="flex-1 flex flex-col items-center justify-center gap-2 px-8 text-center">
                <div className="text-[13px] opacity-50">No file open</div>
                <div className="text-[12px] opacity-30">Click a path in the transcript to open it here.</div>
            </div>
        )
    }

    return (
        <div className="flex-1 min-h-0 flex flex-col">
            <div className="shrink-0 flex items-center gap-2 px-3 h-9 border-b border-white/10">
                <span className="text-[12px] font-mono opacity-70 truncate" title={filePath}>{filePath}</span>
                {dirty && <span className="shrink-0 w-1.5 h-1.5 rounded-full bg-amber-400/80" title="unsaved" />}
                <span className="flex-1" />
                {savedAt && !dirty && (
                    <span className="shrink-0 flex items-center gap-1 text-[11px] opacity-45">
                        <Check size={11} /> saved
                    </span>
                )}
                {state === "saving" && <Loader2 size={12} className="shrink-0 opacity-50 animate-spin" />}
                <button
                    onMouseDown={(e) => { e.preventDefault(); save() }}
                    disabled={!dirty}
                    className="shrink-0 px-2 py-1 rounded text-[11px] bg-[#6b8e23] text-white disabled:opacity-25 disabled:bg-white/10"
                >
                    Save
                </button>
            </div>

            <div className="flex-1 min-h-0 overflow-hidden" ref={host}>
                {state === "loading" && <div className="p-4 text-[13px] opacity-40">opening…</div>}
                {state === "error" && <div className="p-4 text-[13px] text-red-400/80">could not open this file</div>}
            </div>
        </div>
    )
}
