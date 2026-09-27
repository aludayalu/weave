"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";

/**
 * One panel docked to the right of the chat. It has two faces: the agent's
 * uncommitted changes, and a file open for editing. A panel rather than a modal
 * because it stays put while the conversation keeps moving.
 */
const PanelContext = createContext({
    tab: "changes",
    setTab: () => {},
    open: () => {},
    close: () => {},
    isOpen: false,
    filePath: null,
    setFilePath: () => {},
    focusPath: null,
    setFocusPath: () => {},
    width: 520,
    setWidth: () => {},
});

export function PanelProvider({ children }) {
    const [isOpen, setOpen] = useState(false);
    const [tab, setTab] = useState("changes");
    const [filePath, setFilePath] = useState(null);
    const [focusPath, setFocusPath] = useState(null);
    const [width, setWidth] = useState(520);

    const open = useCallback((which, path = null) => {
        if (path) setFilePath(path);
        if (which) setTab(which);
        setOpen(true);
    }, []);

    const close = useCallback(() => setOpen(false), []);

    /** A path in the transcript: opens the file, and remembers it for the diff tab. */
    const openFile = useCallback((path) => {
        setFocusPath(path);
        setFilePath(path);
        setTab("file");
        setOpen(true);
    }, []);

    const showChanges = useCallback((path = null) => {
        if (path) setFocusPath(path);
        setTab("changes");
        setOpen(true);
    }, []);

    const value = useMemo(
        () => ({ tab, setTab, open, close, isOpen, openFile, showChanges, filePath, setFilePath, focusPath, setFocusPath, width, setWidth }),
        [tab, isOpen, open, close, openFile, showChanges, filePath, focusPath, width]
    );

    return <PanelContext.Provider value={value}>{children}</PanelContext.Provider>;
}

export function usePanel() {
    return useContext(PanelContext);
}
