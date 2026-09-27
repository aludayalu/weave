import { continue_ref } from "@/lib/helpers"
import { useRouter } from "next/navigation"

export function ChatsListsItem({title, chat_id}) {
    const router = useRouter()
    return (
        <div className="truncate pl-2 pr-2 pt-1 pb-1 hover:bg-[#333] select-none cursor-pointer opacity-70 hover:opacity-100" onMouseDown={() => {
            continue_ref.current.current = false
            router.push("/?chat_id=" + chat_id)
        }}>
            {title}
        </div>
    )
}