import { useEffect, useRef, useState } from "react";
import { StoreLoad, StoreSave } from "./syncStore";
import { useSearchParams } from "next/navigation";
import { readItem, writeItem } from "./indexedDB";

export var continue_ref = { current: { current: true } }

function toDataUrl(bytes, mime) {
    let binary = ""
    const chunk = 0x8000
    for (let i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk))
    }
    return `data:${mime || "application/octet-stream"};base64,${btoa(binary)}`
}

export async function ReadFiles(files) {
    return Promise.all(
        files.map(async (file) => {
            const data = new Uint8Array(await file.arrayBuffer())
            return {
                name: file.name,
                type: file.type,
                size: file.size,
                lastModified: file.lastModified,
                data,
                // kept so the thumbnail still renders after data is released
                dataUrl: toDataUrl(data, file.type),
                id: Math.random()
            }
        })
    );
}

export function create_new_chat(title = "") {
    title = title.replace("\n", " ").replace("\t", "    ").replace("\r", " ")
    title = title.trim()
    
    if (title.length == 0) {
        title = "untitled chat"
    }

    var chat_id = random_id()
    StoreSave("chat-" + chat_id, {"title": title, "interaction_ids": [], "chat_id": chat_id})
    StoreSave("chat_headers", [...StoreLoad("chat_headers", []), {"title": title, "chat_id": chat_id, "updated_at": String(new Date())}])
    return chat_id
}

export function random_id() {
    return Array.from({ length: 16 }, () => Math.floor(Math.random() * 16).toString(16)).join("")
}

export async function getInteractions(chat_id) {
    if (!chat_id) return []
    var out = []

    var chat = StoreLoad("chat-" + chat_id)

    var interaction_ids = chat.interaction_ids

    for (let index = 0; index < interaction_ids.length; index++) {
        const interaction_id = interaction_ids[index];
        out.push(await readItem("interaction-" + interaction_id))
    }

    return out
}

export function useInteractions() {
    const chat_id = useSearchParams().get("chat_id")
    const [inters, setInters] = useState([])
    const inters_ref = useRef(inters)

    useEffect(() => {
        (async () => {
            const fresh = await getInteractions(chat_id)
            setInters(fresh)
            inters_ref.current = fresh
        })()
    }, [chat_id])

    async function saveInteractions(newValue) {
        if (typeof newValue == "function") {
            newValue = newValue(inters_ref.current)
        }

        const new_ids = []

        for (let index = 0; index < newValue.length; index++) {
            const inter = newValue[index];
            inter.chat_id = chat_id
            await writeItem("interaction-" + inter.id, inter)
            new_ids.push(inter.id)
        }

        { // Just saving other stuff
            var current_data = StoreLoad("chat-" + chat_id)
            current_data.interaction_ids = new_ids
            StoreSave("chat-" + chat_id, current_data)
            
            var current_headers = StoreLoad("chat_headers", [])

            for (let index = 0; index < current_headers.length; index++) {
                var header = current_headers[index];

                if (header.chat_id == chat_id) {
                    current_headers[index].updated_at = String(new Date())
                }
            }

            StoreSave("chat_headers", current_headers)
        }

        setInters(newValue)
        inters_ref.current = newValue
    }

    return [inters_ref, saveInteractions]
}

export function getMessagesFromInteractions(interactions) {
    var out = []

    interactions.forEach((x) => {
        // the timeline preserves the real order of text, tool calls and results
        if (x.timeline && x.timeline.length > 0) {
            x.timeline.forEach((entry) => {
                if (entry.raw_message) {
                    out.push(entry.raw_message)
                }
            })
            return
        }

        // messages saved before the timeline existed
        if (x.thinking) {
            x.thinking.forEach((y) => {
                if (y.raw_message) {
                    out.push(y.raw_message)
                }
            })
        }

        x.output.forEach((y) => {
            if (y.raw_message) {
                out.push(y.raw_message)
            }
        })
    })

    return out
}