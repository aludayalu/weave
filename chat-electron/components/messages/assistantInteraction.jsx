import equal from "fast-deep-equal";
import { memo, useState } from "react";
import Markdown from "../markdown";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ToolActivity } from "./toolCall";
import { buildRows, pairTools } from "@/lib/ai/timeline.mjs";

function Reasoning({ text, defaultOpen }) {
    const [open, setOpen] = useState(defaultOpen);
    const [touched, setTouched] = useState(false);

    // while the answer is still streaming, keep it folded unless asked
    if (!touched && defaultOpen === false && open) {
        setTimeout(() => setOpen(false), 0);
    }

    return (
        <>
            <div
                className="opacity-50 font-bold flex gap-2 select-none text-sm items-center cursor-pointer w-fit"
                onMouseDown={() => { setTouched(true); setOpen(!open) }}
            >
                {open ? <ChevronDown /> : <ChevronRight />}
                Thought for a few seconds
            </div>
            {open && (
                <div className="flex flex-col gap-5 opacity-50 text-sm w-full min-w-0 break-words">
                    <Markdown text={text} />
                </div>
            )}
        </>
    );
}

export function AssistantInteractionInner({interaction, showSpinner}) {
    const answer = interaction.output[0]?.content ?? "";
    const hasNoOutputContent = !answer.trim().length;

    // Old messages have no timeline; fall back to the old two-bucket layout.
    const timeline = interaction.timeline && interaction.timeline.length > 0
        ? interaction.timeline
        : null;

    if (!timeline) {
        const reasoning = (interaction.thinking ?? []).filter((x) => x.type === "text");
        const tools = pairTools(interaction.thinking ?? []);

        return (
            <>
                {reasoning.length > 0 && <Reasoning text={reasoning.map((x) => x.content).join("")} defaultOpen={hasNoOutputContent} />}
                {tools.length > 0 && (
                    <div className="flex flex-col gap-0.5 py-1 pl-3.5 ml-0.5 border-l border-white/10 min-w-0 w-full">
                        {tools.map(({ call, result }, i) => <ToolActivity key={i} call={call} result={result} />)}
                    </div>
                )}
                <div className="min-w-0 w-full max-w-full overflow-x-hidden">
                    <Markdown text={answer} />
                </div>
            </>
        );
    }

    const rows = buildRows(timeline)

    return (
        <>
            {rows.map((row, index) => {
                if (row.kind === "reasoning") {
                    return <Reasoning key={index} text={row.text} defaultOpen={hasNoOutputContent && showSpinner} />
                }
                if (row.kind === "tools") {
                    return (
                        <div key={index} className="flex flex-col gap-0.5 py-1 pl-3.5 ml-0.5 border-l border-white/10 min-w-0 w-full">
                            {row.paired.map(({ call, result }, i) => (
                                <ToolActivity key={i} call={call} result={result} />
                            ))}
                        </div>
                    );
                }
                return (
                    <div key={index} className="min-w-0 w-full max-w-full overflow-x-hidden">
                        <Markdown text={row.text} />
                    </div>
                );
            })}
        </>
    );
}

export const AssistantInteraction = memo(
    AssistantInteractionInner,
    (prev, next) => equal(prev, next)
)
