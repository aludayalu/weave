import { create_new_chat, ReadFiles } from "@/lib/helpers";
import { sendMessage, useOnMessage } from "@/lib/messages"
import { ChevronUp, Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react"
import { FileItem } from "./FileItem";
import { PendingBar } from "./PendingBar";
import { useRouter, useSearchParams } from "next/navigation";

export default function ChatInput() {
    const [text, setText] = useState("")
    const textAreaRef = useRef(null)
    const [files, setFiles] = useState([])
    const chat_id = useSearchParams().get("chat_id")
    const router = useRouter()
    const sendRightSideMessageRef = useRef(null)
    const sendMessageRef = useRef(false)

    useEffect(() => {
        const textarea = textAreaRef.current;
        if (!textarea) return;

        textarea.rows = 3;

        while (textarea.scrollHeight > textarea.clientHeight && textarea.rows < 10) {
            textarea.rows++;
        }
    }, [text]);

    useOnMessage("input", (msg) => {
        if (msg.event == "addFiles") {
            setFiles((files) => [...files, ...msg.files])
        }
    })

    function UserSendMessage() {
        if (text.trim().length == 0 && files.length == 0) {
            return
        }

        sendMessage("rightSide", {"event": "send_message", "data": {
            "text": text.trim(),
            "files": files,
        }})

        setText("")
        setFiles([])

        document.getElementById("interactions")?.focus()
    }

    sendRightSideMessageRef.current = UserSendMessage

    useEffect(() => {
        if (sendMessageRef.current) {
            sendMessageRef.current = false
            sendRightSideMessageRef.current()
        }
    }, [chat_id])

    useEffect(() => {
        function OnKeyDown(e) {
            if (e.key == "Escape") {
                document.getElementById("interactions")?.focus()
                return
            }

            if (document.getElementById("userText") != document.activeElement && e.key.length === 1 && !e.metaKey && !e.altKey) {
                if (e.shiftKey && e.key == "c") {
                    return // don't interrupt a copy
                }

                document.getElementById("userText")?.focus()
            }

            if (e.key != "Enter") return
            if (e.shiftKey) return
            if (document.activeElement.id != "userText") return

            e.preventDefault();

            if (chat_id === null) {
                var new_chat_id = create_new_chat(document.activeElement.value.slice(0, 50))
                router.push("/?chat_id=" + new_chat_id)
                sendMessageRef.current = true
                return
            } else {
                sendRightSideMessageRef.current()
            }
        }

        document.addEventListener("paste", OnPaste);
        window.addEventListener("keydown", OnKeyDown)

        return () => {
            window.removeEventListener("keydown", OnKeyDown)
            document.removeEventListener("paste", OnPaste)
        }

    }, [chat_id])

    async function OnPaste(e) {
        if (e.clipboardData.types.includes("text/plain")) {
            document.getElementById("userText")?.focus()
        }

        const items = Array.from(e.clipboardData.items).filter((item) => item.kind == "file")

        if (items.length == 0) return
        
        e.preventDefault()
        
        const pastedFiles = await ReadFiles(items.map((item) => item.getAsFile()).filter(Boolean))

        setFiles((files) => [...files, ...pastedFiles])
    }

    useEffect(() => {
        function FocusUserText() {
            document.getElementById("userText")?.focus()
        }
    
        function OnVisibilityChange() {
            if (document.visibilityState === "visible") {
                FocusUserText()
            }
        }
    
        FocusUserText()
    
        window.addEventListener("focus", FocusUserText)
        document.addEventListener("visibilitychange", OnVisibilityChange)
    
        return () => {
            window.removeEventListener("focus", FocusUserText)
            document.removeEventListener("visibilitychange", OnVisibilityChange)
        }
    }, [chat_id])

    return (
        <div style={{position: "absolute", bottom: 20}} className="flex flex-col justify-center items-center w-full px-5 pointer-events-none">
            <div className="w-full max-w-[720px] flex flex-col gap-2">
            <PendingBar />
            <div className="w-full bg-[#111] p-2 pointer-events-auto" style={{border: "1px solid rgba(255, 255, 255, 0.14)", borderRadius: "8px"}}>
                <div className={`flex flex-wrap gap-2 items-end ${files.length > 0 && "mb-3"}`}>
                    {files.map((x) => <FileItem file={x} key={x.id} onDelete={() => {
                        setFiles((files) => files.filter((y) => y.id != x.id))
                    }} />)}
                </div>

                <textarea autoFocus id="userText" className="w-full scrollbar-gutter-stable" rows={3} value={text} onChange={(x) => setText(x.target.value)} ref={textAreaRef} />

                <div className="w-full flex justify-between p-1">
                    <div style={{border: "1px solid rgba(255, 255, 255, 0.14)", borderRadius: "8px"}} className="p-1 hover:bg-[#333] flex justify-center items-center cursor-pointer select-none" onMouseDown={FilesPicker}>
                        <Plus />
                    </div>

                    <div style={{border: "1px solid #6b8e23", borderRadius: "50%"}} className="p-1 bg-gradient-to-b from-[#7ba32c] to-[#3f5418] flex justify-center items-center cursor-pointer select-none">
                        <ChevronUp></ChevronUp>
                    </div>
                </div>
            </div>
            </div>
        </div>
    )
}

async function FilesPicker() {
    try {
        var handles = await window.showOpenFilePicker({
            multiple: true,
        })
    } catch {
        return
    }

    const files = await Promise.all(
        handles.map((h) => h.getFile())
    );

    sendMessage("input", {"event": "addFiles", "files": await ReadFiles(files)})
}

