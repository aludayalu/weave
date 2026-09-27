"use client";

import { ChatsListsItem } from "@/components/chatsListItem";
import { ApprovalToggle, WorkspacePicker } from "@/components/workspacePicker";
import { Wordmark } from "@/components/wordmark";
import ChatInput from "@/components/ChatInput";
import { StoreLoad } from "@/lib/syncStore";
import { Download, Plus } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { sendMessage } from "@/lib/messages";
import { startCommandStream } from "@/lib/ai/commandStream";
import { startApprovalBridge } from "@/lib/ai/approvals";
import { startChangesBridge } from "@/lib/ai/pendingChanges";
import { PanelProvider, usePanel } from "@/components/panels/panelState";
import { SidePanel } from "@/components/panels/SidePanel";
import { ReadFiles } from "@/lib/helpers";

function BaseApp({children}) {
    const chat_id = useSearchParams().get("chat_id")
    const router = useRouter()
    const chats = useChats()
    const [fileDragging, setFileDragging] = useState(false)
    const [api_keys, setApiKeys] = useState(undefined)

    useEffect(() => {
        startCommandStream()
        startApprovalBridge()
        startChangesBridge()
    }, [])

    useEffect(() => {
        const keys = JSON.parse(localStorage.getItem("api_keys"))
        setApiKeys(keys)

        if (keys == null) {
            router.push("/manageKeys")
        }
    }, [])

    if (api_keys === undefined || api_keys == null) {
        return children
    }

    async function onDrop(e) {
        e.preventDefault();
    
        const files = [...e.dataTransfer.files];

        setFileDragging(false)
    
        sendMessage("input", {"event": "addFiles", "files": await ReadFiles(files)})
    }

    return (
        <>
            <div id="root" className="flex h-[100svh] overflow-hidden">
                <div className="w-[248px] shrink-0 flex flex-col" style={{borderRight: "1px solid rgba(255, 255, 255, 0.14)"}}>
                    {/* top strip is draggable and deliberately empty on the left so the
                        macOS traffic lights have room; the logo sits on its own row below */}
                    <div className="titlebar-drag h-[38px] shrink-0 flex justify-end items-center pr-3 select-none">
                        <div className="titlebar-no-drag hover:bg-[#333] h-[26px] w-[26px] shrink-0 flex items-center justify-center cursor-pointer opacity-60 hover:opacity-100" style={{borderRadius: "8px"}} onMouseDown={() => location.href = "/"} aria-label="New Chat">
                            <Plus size={16} strokeWidth={3}></Plus>
                        </div>
                    </div>

                    <div className="titlebar-drag flex items-center px-3 pb-2.5 select-none">
                        <Wordmark size="xs" />
                    </div>

                    <div id="chats" className="flex-1 min-h-0 flex flex-col">
                        <div className="px-3 pt-1 pb-0.5 opacity-50 select-none text-[11px] uppercase tracking-wider">
                            Chats
                        </div>
                        <div className="pt-1 px-2 flex-1 min-h-0 overflow-y-auto">
                            {chats.sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at)).map((x) => {
                                return <ChatsListsItem key={x.chat_id} title={x.title} chat_id={x.chat_id}></ChatsListsItem>
                            })}
                        </div>
                    </div>

                    <div className="shrink-0 border-t border-white/10 py-1.5">
                        <WorkspacePicker />
                        <ApprovalToggle />
                    </div>
                </div>

                <div
                    className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden"
                    style={{ position: "relative" }}
                    onDragOver={(e) => {
                        e.preventDefault();
                    }}
                    onDrop={onDrop}
                    onDragEnter={(e) => {
                        if (e.dataTransfer.types.includes("Files")) setFileDragging(true);
                    }}
                    onDragLeave={(e) => {
                        if (e.currentTarget.contains(e.relatedTarget)) return;
                        setFileDragging(false);
                    }}
                >
                    {children}
                    <ChatInput />

                    <div style={{position: "absolute", height: "100%", zIndex: 100, width: "100%", display: fileDragging ? undefined : "none"}} className="bg-black/50 backdrop-blur-md flex flex-col justify-center items-center pointer-events-none">
                        <div><Download size={100}></Download></div>
                        <div className="mt-2">Drop Files here</div>
                    </div>
                </div>

                <DockedPanel />
            </div>
        </>
    )
}

function useChats() {
    const [chats, setChats] = useState(StoreLoad("chat_headers", []))

    function Loader() {
        var chat_headers = StoreLoad("chat_headers", [])
        setChats(chat_headers)
    }

    useEffect(() => {
        const interval = setInterval(Loader, 1000)
        return () => clearInterval(interval)
    }, [])

    return chats
}

/** Reads the panel state, so it only mounts when something is open. */
function DockedPanel() {
    const { isOpen } = usePanel()
    if (!isOpen) return null
    return <SidePanel />
}

export default function Base({children}) {
    const [mounted, setMounted] = useState(false);
    useEffect(() => {
        setMounted(true);
    }, [])
    if (!mounted) return null;
    return (
        <PanelProvider>
            <BaseApp children={children} />
        </PanelProvider>
    );
}