/**
 * Talking to a weave server instead of OpenRouter.
 *
 * The harness uses the OpenRouter SDK, which is hardwired to their base URL, so
 * routing through a local server means doing the request here. The shapes on
 * both sides are the same, which is the whole point of the server speaking the
 * OpenAI chat completions API: the message format, the tool call format and the
 * streamed chunk format all arrive unchanged.
 */

export function readKeys() {
    const defaults = {
        provider: "openrouter",
        openrouter: "",
        weaveKey: "",
        weaveBase: "http://127.0.0.1:8000",
        exa: ""
    }
    const stored = localStorage.getItem("api_keys")
    if (stored == null) return defaults
    try {
        return { ...defaults, ...JSON.parse(stored) }
    } catch (error) {
        console.error("api_keys in localStorage is not valid json", error)
        return defaults
    }
}

export function usingWeave() {
    const keys = readKeys()
    return keys.provider === "weave" && (keys.weaveKey || "").length > 0
}

function baseUrl(keys) {
    return (keys.weaveBase || "http://127.0.0.1:8000").replace(/\/+$/, "")
}

/**
 * Parse a server sent events body into the chunk objects the OpenRouter SDK
 * would have yielded, so the rest of the harness does not know the difference.
 *
 * OpenAI streams `data: {json}` lines and terminates with `data: [DONE]`.
 * Anything that is not a data line is a keepalive comment and is skipped.
 */
async function* readSSE(response) {
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ""

    while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // events are separated by a blank line, and a chunk can straddle two
        // reads, so only complete frames are taken off the buffer
        let split
        while ((split = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, split)
            buffer = buffer.slice(split + 2)
            for (const line of frame.split("\n")) {
                if (!line.startsWith("data:")) continue
                const payload = line.slice(5).trim()
                if (payload === "" || payload === "[DONE]") continue
                try {
                    yield JSON.parse(payload)
                } catch (error) {
                    console.error("could not parse a streamed frame", payload.slice(0, 200), error)
                }
            }
        }
    }
}

/**
 * One streamed chat completion against the weave server.
 *
 * `request` is the same body the SDK would have been given, so callers do not
 * change: model, messages, tools, stream.
 */
export async function* weaveStream(request, { signal } = {}) {
    const keys = readKeys()
    const response = await fetch(`${baseUrl(keys)}/v1/chat/completions`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${keys.weaveKey}`
        },
        body: JSON.stringify({ ...request, stream: true }),
        signal
    })

    if (!response.ok) {
        const detail = await response.text().catch(() => "")
        throw new Error(
            `weave server returned ${response.status}: ${detail.slice(0, 400)}`)
    }
    if (!response.body) {
        throw new Error("weave server returned no body to stream")
    }

    for await (const chunk of readSSE(response)) {
        yield normalise(chunk)
    }
}

/**
 * OpenAI streams tool calls as `delta.tool_calls` with snake_case keys. The
 * OpenRouter SDK yields `delta.toolCalls`, so the harness reads that. Rather
 * than touch the chunk handling in stream.js, the difference is absorbed here,
 * which is the only place that knows which transport is in use.
 */
function normalise(chunk) {
    const choice = chunk?.choices?.[0]
    const delta = choice?.delta
    if (!delta) return chunk
    if (!delta.tool_calls) return chunk
    return {
        ...chunk,
        choices: [{
            ...choice,
            delta: {
                ...delta,
                toolCalls: delta.tool_calls.map((call) => ({
                    index: call.index,
                    id: call.id,
                    type: call.type,
                    function: {
                        name: call.function?.name,
                        arguments: call.function?.arguments
                    }
                }))
            }
        }]
    }
}

/**
 * The request body in OpenAI shape, which is what the server expects.
 *
 * `reasoning` is passed through when the caller set it, because the server
 * forwards to a provider that understands it and dropping it would silently
 * change how much the model thinks.
 */
export function weaveRequest({ model, messages, tools, reasoning }) {
    const body = { model, messages, stream: true }
    if (tools?.length) body.tools = tools
    if (reasoning) body.reasoning = reasoning
    return body
}
