"use client";

import { Button, Input, Label, Switch } from "@heroui/react";
import { useEffect, useState } from "react"

/**
 * Key manager for the harness.
 *
 * Two providers, and they are genuinely different rather than two spellings of
 * the same thing:
 *
 *   OpenRouter  talks to openrouter.ai directly with their key
 *   weave       talks to a weave server, which forwards upstream
 *
 * The weave provider is the one to use when the server is running locally: the
 * hex key is the shared secret the server compares on the backend, and every
 * request made through it is logged against the shared account, so the usage
 * panel on the server shows the traffic instead of it disappearing.
 */
function ManageKeys() {
    const [unsavedChanges, setUnsavedChanges] = useState(false)
    const [keys, setKeys] = useAPIKeys()

    function OnSave() {
        saveKeys(keys)
        setUnsavedChanges(false)
    }

    function update(patch) {
        setUnsavedChanges(true)
        setKeys({ ...keys, ...patch })
    }

    return (
        <>
        <div>
            <img src="/logo_tm_with_text.svg" style={{width: 160}}></img>
            <div className="flex justify-center">
                <div className="w-[50vw] max-w-[500px]">
                    <div className="text-xl">
                        Key Manager
                    </div>

                    <div className="flex flex-col gap-1 mt-6">
                        <Label className="ml-1">Provider</Label>
                        <Switch
                            isSelected={keys.provider === "weave"}
                            onValueChange={(selected) =>
                                update({ provider: selected ? "weave" : "openrouter" })}
                        >
                            {keys.provider === "weave" ? "weave server" : "OpenRouter"}
                        </Switch>
                    </div>

                    {keys.provider === "weave" ? <>
                        <div className="flex flex-col gap-1 mt-4">
                            <Label className="ml-1">weave key</Label>
                            <Input
                                placeholder="the hex key from the weave server"
                                value={keys.weaveKey}
                                onChange={(event) => update({ weaveKey: event.target.value })}
                            />
                        </div>
                        <div className="flex flex-col gap-1 mt-4">
                            <Label className="ml-1">Server</Label>
                            <Input
                                placeholder="http://127.0.0.1:8000"
                                value={keys.weaveBase}
                                onChange={(event) => update({ weaveBase: event.target.value })}
                            />
                        </div>
                        <div className="text-xs opacity-60 mt-2">
                            Requests go to {'{'}server{'}'}/v1/chat/completions and are forwarded
                            upstream by the server, which records them against the
                            shared account.
                        </div>
                    </> : <>
                        <div className="flex flex-col gap-1 mt-4">
                            <Label className="ml-1">OpenRouter</Label>
                            <Input
                                value={keys.openrouter}
                                onChange={(event) => update({ openrouter: event.target.value })}
                                placeholder="sk-or-v1-..."
                            />
                        </div>
                    </>}

                    <div className="flex flex-col gap-1 mt-4">
                        <Label className="ml-1">Exa</Label>
                        <Input
                            value={keys.exa}
                            onChange={(event) => update({ exa: event.target.value })}
                        />
                    </div>

                    {unsavedChanges && <Button className="mt-5" onPress={OnSave}>Save Changes</Button>}
                </div>
            </div>
        </div>
        </>
    )
}

const DEFAULTS = {
    provider: "openrouter",
    openrouter: "",
    weaveKey: "",
    weaveBase: "http://127.0.0.1:8000",
    exa: ""
}

function useAPIKeys() {
    var stored = localStorage.getItem("api_keys")
    var local_keys = { ...DEFAULTS }
    if (stored != null) {
        try {
            // merged over the defaults so a key store written by an older build
            // does not come back undefined and silently disable the new fields
            local_keys = { ...DEFAULTS, ...JSON.parse(stored) }
        } catch (error) {
            console.error("api_keys in localStorage is not valid json, using defaults", error)
        }
    }
    return useState(local_keys)
}

function saveKeys(keys) {
    localStorage.setItem("api_keys", JSON.stringify(keys))
}

export default function Home() {
    const [mounted, setMounted] = useState(false);
    useEffect(() => {
        setMounted(true);
    }, [])
    if (!mounted) return null;
    return <ManageKeys/>;
}
