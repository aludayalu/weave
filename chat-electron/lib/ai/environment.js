/**
 * What the model knows about the machine it is running on, injected fresh each
 * turn so it never has to guess where it is or what it is allowed to touch.
 */
export async function desktopContext() {
    if (typeof window === "undefined" || !window.desktop) return ""

    try {
        const { root } = await window.desktop.workspace.get()
        const names = await window.desktop.tools.list()

        return `
<environment>
You are running inside a desktop application on the user's own machine, not in a
sandbox and not in a web browser. You can really read, write and execute on their
disk.

workspace root: ${root}
Every path you pass to a tool is relative to that root. Paths pointing outside it
are refused, so you cannot wander into the rest of the machine.

tools you can call: ${names.join(", ")}
${names.includes("run_command") ? `
run_command is a real shell. Use it for git, tests, linters, builds and package
managers. It reports the exit code, any terminating signal, how long it ran, and
the combined output. Long commands are killed at the timeout, so keep them bounded.
` : ""}
${names.includes("read_file") ? `
Read a file before you edit it. edit_file matches old_string literally and refuses
an ambiguous match, so read enough surrounding lines to make the match unique.
` : ""}
${names.includes("list_changes") ? `
Your writes reach the disk immediately so the toolchain sees a real working tree.
The user sees every change as a diff and can revert any of it, so a mistake is
recoverable but also visible. Keep edits deliberate, and use list_changes and
revert_file if you need to inspect or undo your own work.
` : ""}
Deletions and commands that could destroy work, such as rm -rf, force pushes or
piped installers, are held for the user's approval. If one is refused, adapt rather
than retrying the same call.
</environment>
`
    } catch {
        return ""
    }
}
