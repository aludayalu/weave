import equal from "fast-deep-equal";
import { FileItem } from "../FileItem"
import { memo } from "react";

function UserInteractionInner({interaction}) {
    var text = interaction.text
    var files = interaction.files

    return (
        <>
        <div className="flex justify-end mt-4" id={interaction.id} style={{scrollMarginTop: "40px", position: "relative"}}>
            <div className="bg-[#222] p-2 max-w-[70%]" style={{borderRadius: "8px"}}>
                <div className={`flex flex-wrap gap-2 items-end ${files.length > 0 && "mb-3"}`}>
                    {files.map((x) => <FileItem file={x} key={x.id} onDelete={() => {}} />)}
                </div>

                <div className="text-left whitespace-pre-wrap max-h-[50svh] p-2 overflow-y-auto">
                    {text}
                </div>
            </div>
        </div>
        </>
    )
}

export const UserInteraction = memo(
    UserInteractionInner,
    (prev, next) => equal(prev.interaction, next.interaction)
);