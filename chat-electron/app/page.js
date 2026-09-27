"use client";

import { Interactions } from "@/components/messages/showInteractions";
import NewChat from "@/components/newChat";
import { generate_stream } from "@/lib/ai/stream";
import { continue_ref, getMessagesFromInteractions, useInteractions } from "@/lib/helpers";
import { writeItem } from "@/lib/indexedDB";
import { useOnMessage } from "@/lib/messages";
import { useSearchParams } from "next/navigation"
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

export default function RightSide() {
    const chat_id = useSearchParams().get("chat_id")
    const [interactions_ref, setInteractions] = useInteractions()
    const [waiting, setWaiting] = useState(false)
    const [response_started, setResponseStarted] = useState(false)
    const interactions_container_ref = useRef(null)
    const scroll_ref = useRef(null)

    // Follow the stream only while the reader is already at the bottom. Any
    // upward movement releases it immediately, so scrolling back through the
    // transcript is never fought.
    const stick = useRef(true)

    const scrollToBottom = useCallback(() => {
        const el = scroll_ref.current
        if (!el) return
        el.scrollTop = el.scrollHeight
    }, [])

    useEffect(() => {
        const el = scroll_ref.current
        if (!el) return

        const release = () => { stick.current = false }

        const onScroll = () => {
            const gap = el.scrollHeight - el.scrollTop - el.clientHeight
            // only a real return to the bottom re-arms following
            stick.current = gap <= 24
        }

        // catching the gesture is what makes it feel instant
        const onWheel = (event) => { if (event.deltaY < 0) release() }
        const onTouchMove = () => release()
        const onKey = (event) => {
            if (event.key === "ArrowUp" || event.key === "PageUp" || event.key === "Home") release()
        }

        el.addEventListener("scroll", onScroll, { passive: true })
        el.addEventListener("wheel", onWheel, { passive: true })
        el.addEventListener("touchmove", onTouchMove, { passive: true })
        window.addEventListener("keydown", onKey)

        // one rAF-batched follow per frame, however fast the tokens arrive
        let frame = 0
        const follow = () => {
            if (frame) return
            frame = requestAnimationFrame(() => {
                frame = 0
                if (stick.current) el.scrollTop = el.scrollHeight
            })
        }

        const observer = new ResizeObserver(follow)
        if (interactions_container_ref.current) observer.observe(interactions_container_ref.current)
        observer.observe(el)

        stick.current = true
        follow()

        return () => {
            el.removeEventListener("scroll", onScroll)
            el.removeEventListener("wheel", onWheel)
            el.removeEventListener("touchmove", onTouchMove)
            window.removeEventListener("keydown", onKey)
            observer.disconnect()
            if (frame) cancelAnimationFrame(frame)
        }
    }, [chat_id])

    useEffect(() => {
        return () => continue_ref.current.current = false
    }, [])

    async function ProcessMessage({text, files}) {
        continue_ref.current.current = false

        if (waiting) {
            await new Promise((resolve) => setTimeout(resolve, 500))
        }
        
        continue_ref.current = {current: true}

        setWaiting(true)
        setResponseStarted(false)

        const file_descriptors = []
        
        for (var i = 0; i < files.length; i++) {
            var f = files[i];

            var result = f.type?.startsWith("image/") ? { type: "image_url", imageUrl: { url: bytesToBase64DataUrl(f.data, f.type) } } : { type: "file", file: { filename: f.name, fileData: bytesToBase64DataUrl(f.data, f.type || "application/octet-stream") } }
            var id = Math.random()

            await writeItem(id, result)

            file_descriptors.push({"type": "indexed_db_item", "id": id})
        }

        const raw_message = {
            role: "user",
            content: [
                { type: "text", text },
                ...file_descriptors
            ]
        }

        files.forEach((x) => {
            x.data = []
        })

        const user_interaction = {
            side: "user",
            id: Math.random(),
            thinking: [],
            text, files,
            output: [
                { type: "mixed", raw_message } // mixed just for our reference
            ],
        }

        await setInteractions((interactions) => [...interactions, user_interaction])

        setTimeout(() => {
            document.getElementById(String(user_interaction.id))?.scrollIntoView({block: "start", behavior: "instant"})
        }, 100)

        var created_assistant_interaction = false

        const assistant_interaction = {
            side: "assistant",
            id: Math.random(),
            // timeline is the truth about what happened and in what order:
            // text, tool calls and results interleave exactly as they occurred
            timeline: [],
            thinking: [],
            output: [],
        }

        function pushTimeline(entry) {
            assistant_interaction.timeline.push(entry)
        }

        async function save_assistant_interaction() {
            await setInteractions((data) => {
                var current_interactions = data

                if (created_assistant_interaction) {
                    current_interactions = current_interactions.slice(0, -1)
                } else {
                    created_assistant_interaction = true
                }

                return [...current_interactions, structuredClone(assistant_interaction)]
            })
        }

        function onStartStream() {
            setResponseStarted(true)
        }

        function onEndStream() {
            setWaiting(false)
        }

        async function onOutputThinkingChunk(chunk) {
            var last_element = assistant_interaction.thinking.at(-1)

            if (!last_element || last_element.type != "text") {
                assistant_interaction.thinking.push({"type": "text", "content": chunk})
                pushTimeline({ "type": "reasoning", "content": chunk })
            }

            if (last_element && last_element.type == "text") {
                last_element.content += chunk
            }

            var last_timeline = assistant_interaction.timeline.at(-1)
            if (last_timeline && last_timeline.type == "reasoning") {
                last_timeline.content += chunk
            }

            await save_assistant_interaction()
        }

        async function onOutputChunk(chunk) {
            var last_element = assistant_interaction.output.at(-1)

            if (!last_element || last_element.type != "text") {
                assistant_interaction.output.push({"type": "text", "content": chunk, "raw_message": { role: "assistant", content: chunk }})
                last_element = assistant_interaction.output.at(-1)
                pushTimeline({ "type": "text", "content": chunk, "raw_message": { role: "assistant", content: chunk } })
            } else {
                last_element.content += chunk
                last_element.raw_message.content = last_element.content
            }

            // keep the running answer and the live segment in step
            var last_segment = assistant_interaction.timeline.at(-1)
            if (last_segment && last_segment.type == "text") {
                last_segment.content = last_element.content
                last_segment.raw_message = last_element.raw_message
            }

            await save_assistant_interaction()
        }

        async function onTools(tool_calls, raw_message) {
            pushTimeline({"type": "tool_call", "raw_message": raw_message, "data": tool_calls})

            await save_assistant_interaction()
        }

        async function onProcessedTools(tool_results, raw_message_array) {
            raw_message_array.forEach((x, i) => {
                pushTimeline({"type": "tool_response", "raw_message": x, "data": tool_results[i]})
            })

            await save_assistant_interaction()
        }

        await generate_stream(getMessagesFromInteractions(interactions_ref.current), chat_id, continue_ref.current, onStartStream, onEndStream, onOutputChunk, onOutputThinkingChunk, onTools, onProcessedTools)
    }

    useOnMessage("rightSide", (msg) => {
        if (msg.event == "send_message") {
            stick.current = true
            scrollToBottom()

            ProcessMessage(msg.data)
        }
    })
    
    if (chat_id === null) {
        return <NewChat />;
    }
    
    return (
        <div
            key={chat_id}
            className="flex-1 min-w-0 overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable] [overflow-anchor:none]"
            ref={scroll_ref}
        >
            <div className="w-full flex justify-center px-5">
                <div className="w-full max-w-[720px] min-w-0 overflow-x-hidden outline-none" ref={interactions_container_ref} id="interactions" tabIndex="-1">
                    <Interactions interactions={interactions_ref.current} showSpinner={waiting} container_ref={interactions_container_ref} />
                </div>
            </div>
        </div>
    );
}

function bytesToBase64DataUrl(bytes, mimeType) {
    let binary = ''
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
    return `data:${mimeType};base64,${btoa(binary)}`
}