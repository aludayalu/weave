"use client";

/**
 * The Weave wordmark. Lowercase, one weight, one olive. The dot is set in the
 * accent so it reads as a full stop rather than a stray speck.
 */
const OLIVE = "#7ba32c";
const OLIVE_DEEP = "#5d7a1f";

const SIZES = {
    xs: { size: 14, tracking: "0em", weight: 600, dot: false },
    sm: { size: 16, tracking: "0em", weight: 600, dot: false },
    md: { size: 22, tracking: "-0.005em", weight: 600, dot: false },
    lg: { size: 34, tracking: "-0.012em", weight: 500, dot: true },
};

export function Wordmark({ size = "sm", stacked = false, className = "" }) {
    const d = SIZES[size] ?? SIZES.sm;
    void stacked;

    return (
        <span
            className={`select-none leading-none ${className}`}
            style={{
                fontSize: d.size,
                fontWeight: d.weight,
                letterSpacing: d.tracking,
                color: size === "lg" ? OLIVE : OLIVE_DEEP,
            }}
        >
            weave
            {d.dot && (
                <span style={{ color: OLIVE_DEEP, marginLeft: "0.04em" }}>.</span>
            )}
        </span>
    );
}
