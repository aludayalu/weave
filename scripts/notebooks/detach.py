"""Start a command fully detached, so it survives an aborted shell.

nohup and disown were not enough: the tool kills the whole process group, and
the child goes with it. A double fork puts the real work in a session of its own
with no controlling terminal, which no group kill reaches.

    python3 detach.py <logfile> <command> [args...]
"""

import os
import sys


def main() -> int:
    log_path, command = sys.argv[1], sys.argv[2:]
    if not command:
        print("usage: detach.py <logfile> <command> [args...]", file=sys.stderr)
        return 2

    if os.fork():                      # parent: hand the child on and leave
        return 0
    os.setsid()                        # new session, no controlling terminal
    if os.fork():                      # first child exits, so we are orphaned
        os._exit(0)

    handle = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(handle, 1)
    os.dup2(handle, 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(handle)
    os.close(devnull)

    # unbuffered, or a python child writes nothing until it exits and the log
    # looks empty for the whole run
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.execvp(command[0], command)
    return 1


if __name__ == "__main__":
    sys.exit(main())
