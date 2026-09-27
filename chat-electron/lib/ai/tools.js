export var tools = [
    {
        type: "function",
        function: {
            name: "web_search",
            description: "Search the web for current information, articles, news, or documentation. Returns a list of relevant results with titles, URLs, and text content.",
            parameters: {
                type: "object",
                properties: {
                    query: {
                        type: "string",
                        description: "The search query"
                    },
                    num_results: {
                        type: "number",
                        description: "Number of results to return (default 5)"
                    }
                },
                required: ["query"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "list_dir",
            description: "List the contents of a directory inside the workspace. Use this to orient yourself before reading or editing files. Returns paths, entry counts, and sizes.",
            parameters: {
                type: "object",
                properties: {
                    dir: {
                        type: "string",
                        description: "Directory relative to the workspace root, e.g. 'src/components'. Defaults to the root."
                    },
                    depth: {
                        type: "number",
                        description: "How many levels to descend (default 1)"
                    }
                }
            }
        }
    },
    {
        type: "function",
        function: {
            name: "read_file",
            description: "Read a text file from the workspace with line numbers. Always read a file before editing it so your edits match the real content.",
            parameters: {
                type: "object",
                properties: {
                    path: {
                        type: "string",
                        description: "File path relative to the workspace root"
                    },
                    start_line: {
                        type: "number",
                        description: "First line to read (default 1)"
                    },
                    end_line: {
                        type: "number",
                        description: "Last line to read (default 400)"
                    }
                },
                required: ["path"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "write_file",
            description: "Create a new file or replace a file's entire contents. The parent directory is created if needed. Prefer edit_file for changes to an existing file.",
            parameters: {
                type: "object",
                properties: {
                    path: {
                        type: "string",
                        description: "File path relative to the workspace root"
                    },
                    content: {
                        type: "string",
                        description: "The full contents to write"
                    }
                },
                required: ["path", "content"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "edit_file",
            description: "Replace an exact snippet in an existing file. old_string must appear exactly once unless replace_all is true, and must include enough surrounding context to be unique.",
            parameters: {
                type: "object",
                properties: {
                    path: {
                        type: "string",
                        description: "File path relative to the workspace root"
                    },
                    old_string: {
                        type: "string",
                        description: "The exact text to replace, copied from the file"
                    },
                    new_string: {
                        type: "string",
                        description: "The text to put in its place"
                    },
                    replace_all: {
                        type: "boolean",
                        description: "Replace every occurrence instead of requiring a unique match"
                    }
                },
                required: ["path", "old_string", "new_string"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "search",
            description: "Search file contents across the workspace for a literal string, like grep. Returns file:line matches. Optionally limit to paths matching a glob.",
            parameters: {
                type: "object",
                properties: {
                    query: {
                        type: "string",
                        description: "The text to search for, treated as a literal string"
                    },
                    dir: {
                        type: "string",
                        description: "Directory to search under, relative to the workspace root"
                    },
                    glob: {
                        type: "string",
                        description: "Optional filename pattern such as '**/*.js'"
                    },
                    limit: {
                        type: "number",
                        description: "Maximum matches to return (default 60)"
                    }
                },
                required: ["query"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "list_changes",
            description: "List every file you have changed this session, with how many lines were added and removed. Use it to review your own work, or to find something to revert.",
            parameters: {
                type: "object",
                properties: {}
            }
        }
    },
    {
        type: "function",
        function: {
            name: "revert_file",
            description: "Put one file back exactly as it was before you changed it this session. Use it when you made a change that turned out to be wrong.",
            parameters: {
                type: "object",
                properties: {
                    path: {
                        type: "string",
                        description: "File to revert, relative to the workspace root"
                    }
                },
                required: ["path"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "diff",
            description: "Show what changed in a file, or the whole workspace, compared with the last git commit. Returns a unified diff plus added and removed line counts. Use it to review your own work, or to see what someone else changed before you touch a file.",
            parameters: {
                type: "object",
                properties: {
                    path: {
                        type: "string",
                        description: "File to diff, relative to the workspace root. Omit for every changed file."
                    },
                    stat_only: {
                        type: "boolean",
                        description: "Return only the added/removed counts instead of the full diff"
                    }
                }
            }
        }
    },
    {
        type: "function",
        function: {
            name: "read_media",
            description: "Look at an image or a PDF in the workspace. The file is attached to the conversation so you can actually see and read it, which is the only way to answer questions about its visual or textual content. Supports PNG, JPEG, GIF, WebP and PDF. Use this instead of read_file for those formats, since read_file returns raw bytes as text.",
            parameters: {
                type: "object",
                properties: {
                    path: {
                        type: "string",
                        description: "Path to the image or PDF, relative to the workspace root"
                    },
                    question: {
                        type: "string",
                        description: "What you need to get out of it, so the right pages or regions are attended to"
                    }
                },
                required: ["path"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "run_command",
            description: "Run a shell command in the workspace. Returns the command, the working directory, the exit code or terminating signal, how long it took, and the combined output. Use it to run tests, linters, builds, and git. Long commands are killed at the timeout.",
            parameters: {
                type: "object",
                properties: {
                    command: {
                        type: "string",
                        description: "The shell command to run"
                    },
                    cwd: {
                        type: "string",
                        description: "Working directory relative to the workspace root"
                    },
                    timeout_ms: {
                        type: "number",
                        description: "Kill the command after this many milliseconds (default 30000, max 600000)"
                    }
                },
                required: ["command"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "make_dir",
            description: "Create a directory (and any missing parents) inside the workspace.",
            parameters: {
                type: "object",
                properties: {
                    path: {
                        type: "string",
                        description: "Directory path relative to the workspace root"
                    }
                },
                required: ["path"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "delete_path",
            description: "Delete a file or directory inside the workspace. Pass recursive true for directories.",
            parameters: {
                type: "object",
                properties: {
                    path: {
                        type: "string",
                        description: "Path relative to the workspace root"
                    },
                    recursive: {
                        type: "boolean",
                        description: "Delete directories and their contents"
                    }
                },
                required: ["path"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "environment",
            description: "Report the workspace root, platform, node version, and home directory. Call this once at the start of a task if you need to know where you are.",
            parameters: {
                type: "object",
                properties: {}
            }
        }
    }
]

var LOCAL_TOOLS = new Set([
    "list_dir", "read_file", "write_file", "edit_file",
    "search", "run_command", "read_media", "diff",
    "list_changes", "revert_file",
    "make_dir", "delete_path", "environment"
])

export function isLocalTool(name) {
    return LOCAL_TOOLS.has(name)
}

async function runWebSearch(parsed) {
    const api_keys = JSON.parse(localStorage.getItem("api_keys"))
    const result = await fetch("/api/exa", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            apiKey: api_keys.exa,
            query: parsed.query,
            numResults: parsed.num_results ?? 5,
        }),
    }).then(r => r.json());

    const formatted = result.results.map(r => ({
        title: r.title,
        url: r.url,
        text: r.text?.slice(0, 2000)
    }))

    return JSON.stringify(formatted)
}

async function runLocalTool(name, parsed, callId) {
    if (typeof window === "undefined" || !window.desktop) {
        return JSON.stringify({
            error: `${name} needs the desktop app. Run it with 'bun run electron' rather than in a plain browser.`
        })
    }

    // the call id lets live command output find its way back to the right row
    const result = await window.desktop.tools.run(name, parsed, callId)

    return {
        text: JSON.stringify({
            ok: result.ok,
            output: result.output,
            ...(result.meta && Object.keys(result.meta).length ? { meta: result.meta } : {}),
        }),
        // images and PDFs ride along as real content parts, not as text
        attachments: result.attachments ?? [],
    }
}

async function runToolCall(tool_call) {
    const { name, arguments: args } = tool_call
    const parsed = typeof args === "string" ? JSON.parse(args || "{}") : (args ?? {})

    if (name === "web_search") return await runWebSearch(parsed)
    if (isLocalTool(name)) return await runLocalTool(name, parsed, tool_call.id)

    return JSON.stringify({ error: `Unknown tool: ${name}` })
}

export async function processTools(tool_calls) {
    return Promise.all(tool_calls.map(async (tc) => {
        let content
        let attachments = []

        try {
            const outcome = await runToolCall(tc)
            if (typeof outcome === "string") {
                content = outcome
            } else {
                content = outcome.text
                attachments = outcome.attachments ?? []
            }
        } catch (err) {
            content = JSON.stringify({ error: String(err) })
        }

        return { tool_call_id: tc.id, content, attachments }
    }))
}
