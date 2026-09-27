"use client"

import { FilePreview } from "./FilePreview"
import { X } from "lucide-react"

export function FileItem({file, onDelete}) {
    return (
        <div className="group/file relative h-16 w-16 shrink-0">
            <FilePreview file={file} />

            {onDelete && (
                <button
                    onMouseDown={(e) => {
                        e.preventDefault()
                        e.stopPropagation()
                        onDelete()
                    }}
                    aria-label={`remove ${file?.name}`}
                    title="Remove"
                    className="absolute -top-1.5 -right-1.5 w-[18px] h-[18px] rounded-full bg-[#2a2a2e] border border-white/25 flex items-center justify-center hover:bg-[#3a3a40] opacity-0 group-hover/file:opacity-100 focus:opacity-100 transition-opacity z-10"
                >
                    <X size={10} strokeWidth={3} />
                </button>
            )}
        </div>
    )
}
