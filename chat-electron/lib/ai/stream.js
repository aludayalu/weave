import { OpenRouter } from '@openrouter/sdk';
import { usingWeave, weaveRequest, weaveStream } from './weave';
import { SYSTEM_PROMPT } from './prompts';
import { tools, processTools } from './tools';
import { desktopContext } from './environment';
import { readItem } from '../indexedDB';

// const MODEL = "z-ai/glm-5.3-flash";
// const PROVIDERS = ["baseten/fp8", "together", "coreweave/nvfp4"]

// const MODEL = "deepseek/deepseek-v4.1-flash";
// const PROVIDERS = ["baseten/fp8", "modal", "phala", "deepseek"]

const MODEL = "deepseek/deepseek-v4-flash-0731";
const PROVIDERS = ["baseten/fp8", "cohere"]

async function generate_once(
    client, messages, chat_id,
    onOutputChunk = () => {}, onThinkingOutputChunk = () => {},
    continue_ref, viaWeave = false
) {

    const toolCallBuffers = {}

    const reasoning = {
        "effort": "max",
        "enabled": true,
    }

    const stream = viaWeave
        ? weaveStream(weaveRequest({ model: MODEL, messages, tools, reasoning }))
        : client.chat.send({
            chatRequest: {
                model: MODEL,
                messages,
                tools,
                provider: {
                    order: PROVIDERS,
                    allow_fallbacks: true,
                    sort: "latency"
                },
                reasoning,
                stream: true,
            }
        }, { headers: {"X-Session-ID": String(chat_id)} });

    for await (const chunk of stream) {
        if (!continue_ref.current) {
            return { aborted: true }
        }

        const content = chunk.choices?.[0]?.delta?.content;

        if (content) {
            await onOutputChunk(content)
        }

        const reasoning = chunk.choices?.[0]?.delta?.reasoning;

        if (reasoning) {
            await onThinkingOutputChunk(reasoning)
        }

        const toolCalls = chunk.choices?.[0]?.delta?.toolCalls;

        if (toolCalls) {
            for (const tc of toolCalls) {
                const idx = tc.index ?? 0

                if (!toolCallBuffers[idx]) toolCallBuffers[idx] = { id: '', name: '', arguments: '' }
                if (tc.id) toolCallBuffers[idx].id = tc.id
                if (tc.function?.name) toolCallBuffers[idx].name += tc.function.name
                if (tc.function?.arguments) toolCallBuffers[idx].arguments += tc.function.arguments
            }
        }

        if (chunk.usage) {
            const completedToolCalls = Object.values(toolCallBuffers).filter(b => b.name)

            return { toolCalls: completedToolCalls, usage: chunk.usage, aborted: false }
        }
    }

    const completedToolCalls = Object.values(toolCallBuffers).filter(b => b.name)

    return { toolCalls: completedToolCalls, usage: null, aborted: false }
}

export async function generate_stream(user_given_messages, chat_id, continue_ref, onStartStream, onEndStream, onOutputChunk, onOutputThinkingChunk, onTools, onProcessedTools) {
    user_given_messages = await extract_indexed_db_items(user_given_messages)
    // the SDK is hardwired to openrouter.ai, so when the harness is pointed at a
    // weave server the request is made there instead. Both carry the same shapes,
    // which is why nothing below this line has to know the difference.
    var viaWeave = usingWeave()
    var client = viaWeave
        ? null
        : new OpenRouter({ apiKey: JSON.parse(localStorage.getItem("api_keys")).openrouter })
    var system = SYSTEM_PROMPT + await desktopContext()
    var messages = [{ role: "system", content: system }, ...user_given_messages]

    // anything the user did outside a turn, such as reverting one of our edits,
    // is stated plainly so the model is never working from a stale file
    const notices = takeNotices()
    if (notices.length > 0) {
        messages.push({
            role: "user",
            content: notices.map((n) => ({ type: "text", text: n }))
        })
    }
    var started = false
    var toEndTotal = false

    async function onLocalOutput(chunk) {
        if (!started) {
            started = true
            await onStartStream()
        }

        toEndTotal = true

        await onOutputChunk(chunk)
    }

    async function onLocalThinking(chunk) {
        if (!started) {
            started = true
            await onStartStream()
        }

        await onOutputThinkingChunk(chunk)
    }

    var rounds = 0
    const MAX_ROUNDS = 32

    while (true) {
        rounds++
        if (rounds > MAX_ROUNDS) {
            console.error("generate_stream: exceeded max rounds without a final answer or tool call")
            break
        }

        const result = await generate_once(client, messages, chat_id, onLocalOutput, onLocalThinking, continue_ref, viaWeave)

        if (!result || result.aborted || !continue_ref.current) {
            await onEndStream()
            return { aborted: true }
        }

        if (result.toolCalls.length > 0) {
            const raw_message = {
                role: "assistant",
                content: null,
                toolCalls: result.toolCalls.map(tc => ({
                    id: tc.id,
                    type: "function",
                    function: { name: tc.name, arguments: tc.arguments }
                }))
            }

            await onTools(result.toolCalls, raw_message)

            messages.push(raw_message)

            const toolResults = await processTools(result.toolCalls)

            var result_messages = []

            for (const tr of toolResults) {
                var new_message = { role: "tool", toolCallId: tr.tool_call_id, content: tr.content }
                result_messages.push(new_message)
                messages.push(new_message)
            }

            // A tool result can only be a string, so images and PDFs are handed
            // over as a user turn instead. OpenRouter asks for the text first and
            // the attachments after it.
            // The SDK validates content parts strictly, so only well formed ones
            // are allowed through. A bad part must not sink the whole request.
            const raw_attachments = toolResults.flatMap((tr) => tr.attachments ?? [])

            const attachments = raw_attachments.filter((part) => {
                if (part?.type === "file") {
                    return typeof part.file?.filename === "string" && typeof part.file?.fileData === "string"
                }
                if (part?.type === "image_url") {
                    return typeof part.imageUrl?.url === "string"
                }
                return false
            })

            if (raw_attachments.length !== attachments.length) {
                console.warn("generate_stream: dropped malformed attachment parts", raw_attachments.length - attachments.length)
            }

            if (attachments.length > 0) {
                const attachment_message = {
                    role: "user",
                    content: [
                        {
                            type: "text",
                            text: `Attached from the tool results above (${attachments.length} file${attachments.length === 1 ? "" : "s"}). Look at ${attachments.length === 1 ? "it" : "them"} before answering.`
                        },
                        ...attachments
                    ]
                }

                messages.push(attachment_message)
                result_messages.push(attachment_message)
            }

            await onProcessedTools(toolResults, result_messages)

            continue
        }

        if (toEndTotal) {
            break
        }

        console.warn("Model round produced no output and no tool calls; stopping to avoid infinite loop")
        break
    }

    await onEndStream()
}

async function extract_indexed_db_items(user_given_messages) {
    var user_given_messages = JSON.parse(JSON.stringify(user_given_messages))

    for (var i = 0; i < user_given_messages.length; i++) {
        var message = user_given_messages[i]

        if (!Array.isArray(message.content)) {
            continue
        }

        for (var j = 0; j < message.content.length; j++) {
            if (message.content[j]?.type != "indexed_db_item") {
                continue
            }

            var item = await readItem(message.content[j].id)

            if (item?.type == "file" && item.file) {
                if (!item.file.fileData && item.file.file_data) {
                    item.file.fileData = item.file.file_data
                }

                delete item.file.file_data
            }

            message.content[j] = item
        }
    }

    return user_given_messages
}
