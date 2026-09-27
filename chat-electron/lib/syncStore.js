function getCurrentChatId() {
    if (typeof window === "undefined") return null
    return new URLSearchParams(window.location.search).get("chat_id")
}

function pruneOldChats() {
    const current_chat_id = getCurrentChatId()
    const headers = JSON.parse(localStorage.getItem("chat_headers") || "[]")

    const deletable = headers.filter(h => h.chat_id !== current_chat_id)
    const keep_count = Math.ceil(deletable.length * 0.1)

    deletable.sort((a, b) => new Date(a.updated_at) - new Date(b.updated_at))
    const to_delete = deletable.slice(0, deletable.length - keep_count)
    const to_delete_ids = new Set(to_delete.map(h => h.chat_id))

    to_delete.forEach(h => {
        const chat = JSON.parse(localStorage.getItem("chat-" + h.chat_id) || "null")

        if (chat?.interaction_ids) {
            chat.interaction_ids.forEach(id => {
                localStorage.removeItem("interaction-" + id)
            })
        }

        localStorage.removeItem("chat-" + h.chat_id)
    })

    const remaining_headers = headers.filter(h => !to_delete_ids.has(h.chat_id))
    localStorage.setItem("chat_headers", JSON.stringify(remaining_headers))
}

export function StoreSave(key, data) {
    try {
        localStorage.setItem(key, JSON.stringify(data))
    } catch (err) {
        if (err.name === "QuotaExceededError" || err.code === 22 || err.code === 1014) {
            pruneOldChats()

            try {
                localStorage.setItem(key, JSON.stringify(data))
            } catch (err2) {
                console.error("StoreSave still failing after pruning:", key, err2)
                console.log(data)
                throw err2
            }
        } else {
            throw err
        }
    }
}

export function StoreLoad(key, default_value) {
    var savedValue = JSON.parse(localStorage.getItem(key))

    return savedValue ?? default_value
}