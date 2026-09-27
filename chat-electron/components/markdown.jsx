"use client";

import { useMemo } from "react";
import { Streamdown } from "streamdown";
import { code } from "@streamdown/code";

const THEME = "catppuccin-mocha";
const SHIKI_THEME = [THEME, THEME];

export default function Markdown({ text }) {
    const plugins = useMemo(() => ({ code }), []);

    return (
        <Streamdown plugins={plugins} shikiTheme={SHIKI_THEME}>
            {text}
        </Streamdown>
    );
}

