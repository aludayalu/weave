/**
 * Turning a turn's timeline into renderable rows. Kept free of JSX so the
 * ordering rules can be tested directly.
 */

/**
 * Tool calls arrive as one "tool_call" entry holding every call in the round,
 * followed by one "tool_response" per result in the same order.
 */
export function pairTools(entries) {
    const calls = [];
    const results = [];

    for (const entry of entries) {
        if (entry.type === "tool_call") {
            for (const call of entry.data ?? []) calls.push(call);
        } else if (entry.type === "tool_response") {
            results.push(entry.data?.content);
        }
    }

    return calls.map((call, index) => ({ call, result: results[index] }));
}

/**
 * Collapse a run of reasoning into one fold, pair each round of calls with its
 * results, and keep every segment in the order it actually happened.
 */
export function buildRows(timeline) {
    const rows = [];
    let i = 0;

    while (i < timeline.length) {
        const entry = timeline[i];

        if (entry.type === "reasoning") {
            let text = entry.content ?? "";
            while (i + 1 < timeline.length && timeline[i + 1].type === "reasoning") {
                text += timeline[i + 1].content ?? "";
                i++;
            }
            if (text.trim()) rows.push({ kind: "reasoning", text });
            i++;
            continue;
        }

        if (entry.type === "text") {
            rows.push({ kind: "text", text: entry.content ?? "" });
            i++;
            continue;
        }

        if (entry.type === "tool_call" || entry.type === "tool_response") {
            const round = [];
            while (i < timeline.length && (timeline[i].type === "tool_call" || timeline[i].type === "tool_response")) {
                round.push(timeline[i]);
                i++;
            }
            const paired = pairTools(round);
            if (paired.length) {
                // back to back rounds share one block so there is no gap
                const previous = rows[rows.length - 1];
                if (previous && previous.kind === "tools") previous.paired.push(...paired);
                else rows.push({ kind: "tools", paired });
            }
            continue;
        }

        i++;
    }

    return rows;
}
