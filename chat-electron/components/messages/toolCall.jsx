import { useEffect, useState } from "react"
import {
    ChevronRight, Terminal, FileText, FolderSearch,
    Search, PenLine, Globe, Check, X, Wrench, FolderPlus, Trash2, Monitor, Image, FileDiff, FileCode2
} from "lucide-react"
import { useCommandOutput } from "@/lib/ai/commandStream"
import { respond, useApprovals } from "@/lib/ai/approvals"
import { MediaPreview } from "./previews"
import { usePanel } from "@/components/panels/panelState"

const ICONS = {
    web_search: Globe,
    list_dir: FolderSearch,
    read_file: FileText,
    write_file: PenLine,
    edit_file: PenLine,
    search: Search,
    run_command: Terminal,
    make_dir: FolderPlus,
    delete_path: Trash2,
    environment: Monitor,
    read_media: Image,
    diff: FileDiff,
}

const LABELS = {
    web_search: "web",
    list_dir: "ls",
    read_file: "read",
    write_file: "write",
    edit_file: "edit",
    search: "grep",
    run_command: "sh",
    make_dir: "mkdir",
    delete_path: "rm",
    environment: "env",
    read_media: "media",
    diff: "diff",
}

function parse(raw, fallback) {
    try {
        return typeof raw === "string" ? JSON.parse(raw || "{}") : (raw ?? fallback)
    } catch {
        return fallback
    }
}

function subject(name, args) {
    switch (name) {
        case "web_search": return args.query
        case "run_command": return args.command
        case "read_media":
        case "read_file":
        case "write_file":
        case "edit_file":
        case "make_dir":
        case "delete_path": return args.path
        case "list_dir": return args.dir || "."
        case "search": return args.query
        case "environment": return "machine"
        default: return ""
    }
}

/** One line that says what came back, so most rows never need expanding. */
function outcome(result) {
    if (!result) return ""
    if (result.error) return result.error
    const output = String(result.output ?? "")
    if (!output) return result.ok === false ? "failed" : "ok"

    if (output.startsWith("$ ")) {
        const status = output.split("\n").find((line) => line.startsWith("status:")) || ""
        return status.replace("status: ", "")
    }

    const lines = output.split("\n").filter((line) => line.trim().length)
    if (result.ok === false) return lines[lines.length - 1] || "failed"
    if (/^\//.test(lines[0] || "")) return lines[1] || lines[0]
    return lines[0] || "ok"
}

function ApprovalCard({ request }) {
    return (
        <div className="mt-1 mb-0.5 ml-[20px] rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 min-w-0">
            <div className="text-[11px] uppercase tracking-wider text-amber-500/70 mb-1">Needs your approval</div>
            <div className="font-mono text-[12px] opacity-85 break-words mb-2">{request.summary}</div>
            <div className="flex items-center gap-2">
                <button
                    onMouseDown={(e) => { e.preventDefault(); respond(request.id, "allow") }}
                    className="px-2.5 py-1 rounded text-[12px] bg-green-600/80 hover:bg-green-600 text-white"
                >
                    Allow
                </button>
                <button
                    onMouseDown={(e) => { e.preventDefault(); respond(request.id, "always") }}
                    className="px-2.5 py-1 rounded text-[12px] bg-white/10 hover:bg-white/20"
                    title="Stop asking for this tool for the rest of this run"
                >
                    Always allow
                </button>
                <button
                    onMouseDown={(e) => { e.preventDefault(); respond(request.id, "deny") }}
                    className="px-2.5 py-1 rounded text-[12px] bg-white/5 hover:bg-white/15 opacity-70"
                >
                    Deny
                </button>
            </div>
        </div>
    )
}

/**
 * One tool call on one line: what it did, and what came back. Shell output
 * streams in while the command runs, and anything destructive pauses here for a
 * decision.
 */
export function ToolActivity({ call, result }) {
    const [open, setOpen] = useState(false)
    const panel = usePanel()
    const Icon = ICONS[call.name] || Wrench
    const args = parse(call.arguments, {})
    const parsedResult = result ? parse(result, null) : null

    const running = call.name === "run_command" && parsedResult === null
    const live = useCommandOutput(running ? call.id : null)
    const approvals = useApprovals()
    const pending = approvals.find((request) => request.name === call.name)

    const failed = parsedResult && (parsedResult.ok === false || parsedResult.error)
    const StatusIcon = failed ? X : Check

    const finished = String(parsedResult?.output ?? parsedResult?.error ?? "")
    const detail = live || finished
    const canExpand = detail.trim().length > 0

    return (
        <div className="flex flex-col min-w-0">
            <div
                className="group flex items-center gap-2 min-w-0 h-6 cursor-pointer select-none"
                onMouseDown={() => canExpand && setOpen(!open)}
            >
                <Icon size={12} className="shrink-0 opacity-40" />
                <span className="shrink-0 opacity-40 font-mono text-[11px]">{LABELS[call.name] || call.name}</span>

                <button
                    onMouseDown={(e) => {
                        e.preventDefault()
                        e.stopPropagation()
                        if (args.path) panel.openFile(args.path)
                    }}
                    disabled={!args.path}
                    title={args.path ? `Open ${args.path}` : undefined}
                    className={`font-mono text-[12px] truncate min-w-0 text-left ${
                        args.path ? "hover:underline underline-offset-2 cursor-pointer" : "opacity-75 cursor-default"
                    }`}
                >
                    {subject(call.name, args)}
                </button>

                {running && <span className="shrink-0 text-[11px] opacity-40 animate-pulse">running…</span>}

                {parsedResult && (
                    <>
                        <span className="shrink-0 opacity-25">·</span>
                        <StatusIcon size={11} className={`shrink-0 ${failed ? "text-red-400/80" : "opacity-35"}`} />
                        <span className={`font-mono text-[12px] truncate min-w-0 ${failed ? "text-red-400/70" : "opacity-45"}`}>
                            {outcome(parsedResult)}
                        </span>
                    </>
                )}

                <span className="flex-1" />

                {!failed && args.path && ["write_file", "edit_file", "delete_path"].includes(call.name) && (
                    <button
                        onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); panel.showChanges(args.path) }}
                        title={`Open the diff for ${args.path}`}
                        className="shrink-0 flex items-center gap-1.5 text-[12px] font-medium px-2.5 h-7 rounded-md bg-[#6b8e23] text-white hover:bg-[#7ba32c] transition-colors"
                    >
                        <FileDiff size={12} />
                        View diff
                    </button>
                )}

                {!failed && args.path && call.name === "read_file" && (
                    <button
                        onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); panel.openFile(args.path) }}
                        title={`Open ${args.path} in the editor`}
                        className="shrink-0 flex items-center gap-1.5 text-[12px] px-2.5 h-7 rounded-md bg-white/8 hover:bg-white/16 text-white/70 transition-colors"
                    >
                        <FileCode2 size={12} />
                        Open
                    </button>
                )}

                {canExpand && (
                    <ChevronRight
                        size={12}
                        className={`shrink-0 opacity-0 group-hover:opacity-40 transition-transform ${open ? "rotate-90 opacity-40" : ""}`}
                    />
                )}
            </div>

            {open && canExpand && (
                <pre className="mt-1 mb-0.5 ml-[20px] bg-black/40 rounded-md px-3 py-2 text-[11px] leading-relaxed whitespace-pre-wrap break-words overflow-y-auto max-h-64 min-w-0 select-text opacity-70">
                    {detail.trim()}
                </pre>
            )}

            {call.name === "read_media" && parsedResult?.ok !== false && (
                <MediaPreview
                    path={args.path}
                    mime={parsedResult?.meta?.mime}
                    size={parsedResult?.meta?.bytes ? `${(parsedResult.meta.bytes / 1024).toFixed(0)}KB` : null}
                />
            )}

            {pending && <ApprovalCard request={pending} />}
        </div>
    )
}
