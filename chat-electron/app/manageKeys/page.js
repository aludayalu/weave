"use client";

import { Button, Input, Label } from "@heroui/react";
import { useEffect, useState } from "react"

function ManageKeys() {
    const [unsavedChanges, setUnsavedChanges] = useState(false)
    const [keys, setKeys] = useAPIKeys()

    function OnSave() {
        saveKeys(keys)
        setUnsavedChanges(false)
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
                    <div className="flex flex-col gap-1 mt-4">
                        <Label className="ml-1">OpenRouter</Label>
                        <Input id="input-type-email" value={keys.openrouter} onChange={(event) => {
                            setUnsavedChanges(true)
                            setKeys({...keys, "openrouter": event.target.value})
                        }} type="email" />
                    </div>
                    <div className="flex flex-col gap-1 mt-4">
                        <Label className="ml-1">Exa</Label>
                        <Input id="input-type-email" value={keys.exa} type="email" onChange={(event) => {
                            setKeys({...keys, "exa": event.target.value})
                            setUnsavedChanges(true)
                        }} />
                    </div>
                    {unsavedChanges && <Button className="mt-5" onPress={OnSave}>Save Changes</Button>}
                </div>
            </div>
        </div>
        </>
    )
}

function useAPIKeys() {
    var local_keys = JSON.parse(localStorage.getItem("api_keys"))

    if (local_keys == null) {
        local_keys = {"openrouter": "", "exa": ""}
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