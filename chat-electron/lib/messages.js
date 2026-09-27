import { useEffect, useRef } from "react"

const registry = new Map()

export function useOnMessage(id, func) {
    registry.set(id, func)

    const last_fn = useRef(null)

    last_fn.current = func

    useEffect(() => {
        return () => {
            for (const [key, value] of registry) {
                if (value === last_fn.current) {
                    registry.delete(key);
                    break;
                }
            }
        }
    }, [])
}

export function sendMessage(id, data) {
    var func = registry.get(id)

    if (!func) {
        throw new Error("sendMessage: no entry with id " + id + " was found")
    }

    return func(data)
}