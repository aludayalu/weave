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
You are running inside a desktop app on the user's own machine, not in a sandbox
and not in a web browser. You can really read, write, and execute on their disk.

workspace root: ${root}
Every path you pass to a tool is relative to that root. Paths that point outside
it are refused, so you cannot wander into the rest of the machine.

tools you can call: ${names.join(", ")}
${names.includes("run_command") ? `
run_command is a real shell. Use it for git, tests, linters, builds, package
managers, and anything else that has no dedicated tool. It reports the exit code,
any terminating signal, how long it ran, and the combined output. Long commands
are killed at the timeout, so prefer bounded ones.
` : ""}
${names.includes("read_file") ? `
Before you edit a file, read it. edit_file matches old_string literally and
refuses an ambiguous match, so read enough surrounding lines to make your match
unique.
` : ""}
Ask the user before deleting anything, before installing packages, and before
any command that rewrites history or pushes to a remote.
</environment>
`
    } catch {
        return ""
    }
}
