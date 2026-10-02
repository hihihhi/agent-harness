#!/usr/bin/env python3
"""agent-harness guard: blocks a small set of catastrophic shell commands.

Two ways to use it:

  * As a PreToolUse hook (Claude Code, and any tool with the same contract): the hook JSON
    arrives on stdin ({"tool_name": ..., "tool_input": {"command": ...}}). Exit 0 lets the
    call through; exit 2 blocks it and the reason goes to stderr, where the agent reads it.
  * As a plain checker:  guard.py --check "<command>"   (same exit codes).

Standard library only and no subprocesses, so it adds only interpreter start-up time to each call.
It is a seat belt, not a sandbox: it stops the common catastrophic mistakes an agent makes,
not a determined attacker. Anything it cannot parse as a hook payload is blocked.

Extra trusted installer hosts for `curl ... | sh`: env HARNESS_GUARD_TRUSTED_HOSTS
(comma-separated host names).

Protected data: every absolute path listed in HARNESS_GUARD_PROTECTED_FILE (default
/etc/agent-harness/protected-paths, one per line, # comments) is a tree nothing may delete, move away,
truncate or overwrite: rm/rmdir/unlink/shred/truncate/mv on it, find -delete in it, rsync --delete into it,
dd of= into it, a > redirect onto a file in it, zfs destroy/rollback/rename/set, and an interpreter whose
code names it together with a delete or write call. Reading it is untouched. No file: no protected paths.
"""
import json
import os
import re
import shlex
import sys

MAX_DEPTH = 5

SEPARATORS = {";", ";;", "&", "&&", "||", "(", ")", "<(", ">(", "$(", "\n"}
PIPES = {"|", "|&"}
REDIRECTS = {">", ">>", "<", "&>", "&>>", ">&", ">|", "<>", "<<", "<<<"}
KEYWORDS = {"!", "{", "}", "then", "do", "else", "elif", "if", "while", "until", "time",
            "exec", "command", "builtin", "nohup", "noglob", "fi", "done"}
ELEVATE = {"sudo", "doas", "pkexec", "run0", "su"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
INTERPRETERS = {"python", "python3", "perl", "ruby", "node", "php"}
FETCHERS = {"curl", "wget"}
DEFAULT_TRUSTED = {"sh.rustup.rs", "astral.sh", "bun.sh", "deno.land", "get.pnpm.io",
                   "install.python-poetry.org", "claude.ai"}
SSH_TOOLS = {"ssh", "ssh-add", "ssh-keygen", "ssh-copy-id", "chmod", "ls", "test", "[",
             "stat", "mkdir"}
READERS = {"cat", "less", "more", "head", "tail", "bat", "strings", "xxd", "od", "hexdump",
           "base64", "cp", "mv", "scp", "rsync", "curl", "wget", "nc", "ncat", "grep", "rg",
           "awk", "sed", "sort", "cut", "tee", "source", ".", "open", "pbcopy", "xclip"}

DEVICE = re.compile(r"^/dev/(r?disk\d|sd[a-z]|hd[a-z]|vd[a-z]|xvd[a-z]|nvme\d|mmcblk\d|md\d|dm-\d|loop\d)")
SAFE_DEV = re.compile(r"^/dev/(null|zero|u?random|stdout|stderr|stdin|tty|fd/\d+)$")
URL_HOST = re.compile(r"https?://([^/\s:'\"?#]+)", re.I)
SECRET = re.compile(
    r"(^|/)\.ssh(/|$)"
    r"|(^|/)id_(rsa|dsa|ecdsa|ed25519)$"
    r"|(^|/)\.aws/(credentials|config)$"
    r"|(^|/)\.(netrc|pgpass|git-credentials|npmrc|pypirc)$"
    r"|(^|/)\.docker/config\.json$|(^|/)\.kube/config$"
    r"|(^|/)\.(gnupg|azure)(/|$)|(^|/)\.config/(gcloud|gh)(/|$)"
    r"|(^|/)\.claude/\.credentials\.json$|(^|/)\.codex/auth\.json$"
    r"|(^|/)Keychains(/|$)|^/etc/(shadow|sudoers)"
    r"|\.(p12|pfx|key)$")
ENV_FILE = re.compile(r"(^|/)\.env(\.[A-Za-z0-9_-]+)?$")
ENV_OK = re.compile(r"\.(example|sample|template|dist)$")
FORK_BOMB = [
    re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:"),
    re.compile(r"\b(\w+)\s*\(\s*\)\s*\{[^}]*\b\1\s*\|\s*\1\b[^}]*&"),
    re.compile(r"\bfork\s+while\s+fork\b"),
]
SUBST_FEEDS_SHELL = re.compile(
    r"(\b(" + "|".join(sorted(SHELLS | INTERPRETERS | {"eval", "source"})) + r")\b|(^|[;&|]\s*)\.\s)"
    r"[^;&|]*(<\(|\$\(|`)\s*(curl|wget)\b")


PROTECTED_FILE = "/etc/agent-harness/protected-paths"
DESTROYERS = {"rm", "rmdir", "unlink", "shred", "truncate", "mv"}
PY_DESTROY = re.compile(r"rmtree|remove|unlink|rmdir|truncate|rename|replace|os\.system|subprocess|"
                        r"write_bytes|write_text|open\([^)]*['\"][wa+]")


def protected_roots():
    path = os.environ.get("HARNESS_GUARD_PROTECTED_FILE") or PROTECTED_FILE
    try:
        with open(path) as f:
            lines = [ln.split("#", 1)[0].strip() for ln in f]
    except OSError:
        return []
    return [os.path.normpath(ln) for ln in lines if ln.startswith("/")]


_CWD = [None]   # the directory a `cd` earlier in the same command moved to (None: the hook's own cwd)


def protected(p, roots):
    """The protected root `p` lies in (or contains), or None. A relative path is taken from the cwd; a
    path that starts with a variable or a substitution cannot be resolved here and counts if a root is
    named anywhere in it."""
    if not roots or not p or p.startswith("-"):
        return None
    if p.startswith(("$", "`")) or "$(" in p:
        return next((r for r in roots if r in p), None)
    base = _CWD[0] or os.getcwd()
    if base == "?" and not p.startswith(("/", "~")):
        return None                                   # after `cd $VAR`: a relative path cannot be resolved
    t = os.path.normpath(os.path.join(base, os.path.expanduser(p)))
    for r in roots:
        if t == r or t.startswith(r + "/") or r.startswith(t.rstrip("/") + "/"):
            return r
    return None


class Blocked(Exception):
    pass


def block(reason):
    raise Blocked(reason)


# ---------------------------------------------------------------- tokenising

HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
FEEDS_BODY = re.compile(r"\b(" + "|".join(sorted(SHELLS | INTERPRETERS | {"ssh", "eval"})) + r")\b[^|;&]*<<")


def strip_heredocs(cmd):
    """Drop here-document bodies (data), unless the heredoc feeds a shell or interpreter."""
    out, end, keep = [], None, True
    for line in cmd.split("\n"):
        if end is not None:
            if line.strip() == end:
                end = None
            if keep:
                out.append(line)
            continue
        out.append(line)
        m = HEREDOC.search(line.replace("<<<", "   "))
        if m:
            end, keep = m.group(1), bool(FEEDS_BODY.search(line))
    return "\n".join(out)


def tokenize(cmd):
    """Shell-aware tokens; quotes are data. Newlines become separators."""
    cmd = strip_heredocs(cmd).replace("\\\n", " ")
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes: the shell would refuse too; be conservative
        return re.findall(r"[;&|()<>]+|[^\s;&|()<>]+", cmd.replace("'", " ").replace('"', " "))


def pipelines(tokens):
    """[[segment, segment...], ...] where a segment is (words, redirect_targets)."""
    out, pipe, words, redirs = [], [], [], []
    punct = set(";&|()<>")
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t and set(t) <= punct:
            if "(" not in t and ("<" in t or ">" in t):
                # redirect: the next word is its target, not an operand
                if i + 1 < len(tokens):
                    redirs.append(tokens[i + 1])
                    i += 1
                i += 1
                continue
            pipe.append((words, redirs))
            words, redirs = [], []
            if t not in PIPES:
                out.append(pipe)
                pipe = []
        else:
            words.append(t)
        i += 1
    pipe.append((words, redirs))
    out.append(pipe)
    return [[s for s in p if s[0] or s[1]] for p in out if any(s[0] or s[1] for s in p)]


def unwrap(words):
    """Skip assignments, keywords and wrappers (env, nice, timeout, xargs ...)."""
    i = 0
    while i < len(words):
        w = words[i]
        if w in KEYWORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
            continue
        base = os.path.basename(w)
        if base in ("env", "nice", "ionice", "stdbuf", "timeout", "xargs", "caffeinate"):
            i += 1
            takes_value = {"-u", "-n", "-I", "-P", "-L", "-d", "-s", "-E", "-a", "-c", "-k",
                           "-s", "--signal", "--kill-after"}
            while i < len(words) and (words[i].startswith("-") or "=" in words[i]):
                i += 2 if words[i] in takes_value else 1
            if base == "timeout" and i < len(words):
                i += 1  # the duration
            continue
        return base, words[i + 1:]
    return "", []


def flags_and_operands(args):
    flags, ops, done = [], [], False
    for a in args:
        if not done and a == "--":
            done = True
        elif not done and a.startswith("-") and a != "-":
            flags.append(a)
        else:
            ops.append(a)
    return flags, ops


def has_short(flags, letters):
    return any(not f.startswith("--") and set(letters) & set(f[1:]) for f in flags)


# ---------------------------------------------------------------- path classes

def critical_path(p):
    """Why deleting / chmod-ing `p` recursively would be catastrophic, or None."""
    if p in ("", "-"):
        return None
    if "$(" in p or "`" in p or p == "$":
        return "a command substitution whose result is unknown"
    q = p
    while len(q) > 1 and q.endswith("/"):
        q = q[:-1]
    for suffix in ("/*", "/.*"):
        if q.endswith(suffix):
            q = q[: -len(suffix)] or "/"
    parts = [x for x in q.split("/") if x not in ("", ".")]
    if ".." in q.split("/") and not any(x not in ("..",) for x in parts):
        return "a parent directory"
    if q in (".", "./"):
        return "the current directory"
    if re.match(r"^~[A-Za-z0-9._-]*$", q) or q in ("$HOME", "${HOME}"):
        return "the home directory"
    m = re.match(r"^(~|\$HOME|\$\{HOME\})/(.*)$", q)
    if m:
        rest = [x for x in m.group(2).split("/") if x not in ("", ".")]
        if ".." in rest:
            return "a path that climbs out of the home directory"
        if len(rest) <= 1:
            return "a top-level folder of the home directory"
        return None
    if q.startswith("$"):
        if re.match(r"^\$\{[A-Za-z_][A-Za-z0-9_]*:\?", q) or re.match(r"^\$\{?PWD\}?/.", q):
            return None
        return "a variable that may be empty or unset (then it becomes /...); use ${VAR:?}"
    if q.startswith("/"):
        if ".." in parts:
            return "a path containing .."
        if len(parts) <= 1:
            return "the filesystem root or a top-level system directory"
        if parts[0] in ("home", "Users") and len(parts) == 2:
            return "a user's home directory"
        if any(c in parts[0] for c in "*?["):
            return "a glob over top-level system directories"
    return None


def secret_path(tok, cmd_name):
    t = tok.split("=", 1)[1] if tok.startswith("-") and "=" in tok else tok
    if t.endswith(".pub"):
        return False
    if SECRET.search(t) and cmd_name not in SSH_TOOLS:
        return True
    return bool(ENV_FILE.search(t) and not ENV_OK.search(t) and cmd_name in READERS)


# ---------------------------------------------------------------- rules

def check_segment(name, args, redirs, depth):
    flags, ops = flags_and_operands(args)
    risky_name = name.startswith("$") or "$(" in name

    if name in ELEVATE:
        block("%s: privilege escalation is not allowed; ask the human to run it" % name)

    for r in redirs:
        if DEVICE.match(r) and not SAFE_DEV.match(r):
            block("writing directly to a disk device (%s) wipes it" % r)

    if name == "rm" or risky_name:
        if "--no-preserve-root" in args:
            block("rm --no-preserve-root")
        if "--recursive" in flags or has_short(flags, "rR"):
            for op in ops:
                why = critical_path(op)
                if why:
                    block("rm -r on %s (%s)" % (op, why))

    if name in ("chmod", "chown", "chgrp") and ("--recursive" in flags or has_short(flags, "R")):
        for op in ops[1:]:
            why = critical_path(op)
            if why:
                block("%s -R on %s (%s)" % (name, op, why))

    if name == "find" and ("-delete" in args or any(a in ("rm", "/bin/rm") for a in args)):
        for a in args:
            if a.startswith("-") or a in ("(", "!"):
                break
            why = critical_path(a) if a not in (".", "./") else None
            if why:
                block("find ... -delete on %s (%s)" % (a, why))

    if name == "dd":
        for a in args:
            if a.startswith("of=") and a[3:].startswith("/dev/") and not SAFE_DEV.match(a[3:]):
                block("dd onto a device (%s) wipes it" % a[3:])
    if re.match(r"^(mkfs(\..+)?|mke2fs|mkswap|mkdosfs|mkntfs|newfs(_.+)?|wipefs|blkdiscard"
                r"|fdisk|sfdisk|cfdisk|parted|sgdisk|shred|cp|tee|pv)$", name):
        for a in ops:
            if DEVICE.match(a):
                block("%s on a block device (%s)" % (name, a))
        if name == "blkdiscard":
            block("blkdiscard erases a device")
    if name == "diskutil" and ops and re.match(
            r"^(erase|zero|random|secureErase|partitionDisk|reformat|apfs)", ops[0], re.I):
        block("diskutil %s erases a disk" % ops[0])

    if name == "git":
        check_git(args)

    check_protected(name, args, flags, ops, redirs)

    if name in SHELLS and "-c" in flags:
        i = args.index("-c")
        if i + 1 < len(args):
            check(args[i + 1], depth + 1)
    if name == "eval" and args:
        check(" ".join(args), depth + 1)

    for tok in args + redirs:
        if secret_path(tok, name):
            block("%s touches a credential file (%s); secrets never go into the agent's context"
                  % (name, tok))


def check_protected(name, args, flags, ops, redirs):
    roots = protected_roots()
    if not roots:
        return
    if name in ("cd", "pushd"):
        d = ops[0] if ops else "~"
        if d.startswith(("$", "`")) or "$(" in d or d == "-":
            _CWD[0] = "?"
        elif _CWD[0] != "?" or d.startswith(("/", "~")):
            _CWD[0] = os.path.normpath(os.path.join(_CWD[0] or os.getcwd(), os.path.expanduser(d)))
        return
    why = "is protected data: nothing may delete, move, truncate or overwrite it"
    if name in DESTROYERS:
        for op in ops:
            r = protected(op, roots)
            if r:
                block("%s on %s: %s %s" % (name, op, r, why))
    if name == "find" and ("-delete" in args or "-exec" in args or "-execdir" in args):
        for a in args:
            if a.startswith("-") or a in ("(", "!"):
                break
            r = protected(a, roots)
            if r:
                block("find -delete/-exec in %s: %s %s" % (a, r, why))
    if name == "rsync" and any(a.startswith(("--delete", "--remove-source")) for a in args):
        for op in ops:
            r = protected(op.split(":", 1)[-1], roots)
            if r:
                block("rsync --delete with %s: %s %s" % (op, r, why))
    if name == "dd":
        for a in args:
            r = protected(a[3:], roots) if a.startswith("of=") else None
            if r:
                block("dd onto %s: %s %s" % (a[3:], r, why))
    if name == "zfs" and ops and ops[0] in ("destroy", "rollback", "rename", "set"):
        block("zfs %s: datasets and snapshots are changed by the admin by hand" % ops[0])
    if name in INTERPRETERS or re.match(r"^python3(\.\d+)?$", name):
        text = " ".join(args)
        hit = next((r for r in roots if r in text), None)
        if hit and PY_DESTROY.search(text):
            block("%s code that names %s and deletes or writes: %s %s" % (name, hit, hit, why))
    for rd in redirs:
        r = protected(rd, roots)
        if r:
            block("a redirect onto %s: %s %s" % (rd, r, why))


def check_git(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    if i >= len(args) or args[i] != "push":
        return
    rest = args[i + 1:]
    flags, pos = [], []
    j = 0
    while j < len(rest):
        a = rest[j]
        if a in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            j += 2
            continue
        (flags if a.startswith("-") else pos).append(a)
        j += 1
    force = any(f == "--force" or f.startswith("--force-with-lease") for f in flags) \
        or has_short(flags, "f")
    delete = "--delete" in flags or has_short(flags, "d")
    refspecs = pos[1:]
    targets = []
    for r in refspecs:
        if r.startswith("+"):
            force = True
        dst = r.lstrip("+").split(":")[-1]
        if r.startswith(":"):
            delete = True
        targets.append(dst.replace("refs/heads/", ""))
    protected = [t for t in targets if t in ("main", "master")]
    if (force or delete) and protected:
        block("git push %s to %s rewrites shared history"
              % ("--delete" if delete and not force else "--force", protected[0]))
    if force and ("--all" in flags or "--mirror" in flags):
        block("force push of all branches")
    if force and not refspecs:
        block("git push --force without naming a branch; name it (git push --force origin my-branch)")


def substitutions(tok):
    out = []
    for m in re.finditer(r"`([^`]*)`", tok):
        out.append(m.group(1))
    start = tok.find("$(")
    while start != -1:
        level, k = 0, start + 1
        while k < len(tok):
            if tok[k] == "(":
                level += 1
            elif tok[k] == ")":
                level -= 1
                if level == 0:
                    break
            k += 1
        out.append(tok[start + 2:k])
        start = tok.find("$(", k)
    return out


def trusted_hosts():
    extra = os.environ.get("HARNESS_GUARD_TRUSTED_HOSTS", "")
    return DEFAULT_TRUSTED | {h.strip().lower() for h in extra.split(",") if h.strip()}


def check_remote_exec(cmd, pipes):
    feeds = bool(SUBST_FEEDS_SHELL.search(cmd))
    for pipe in pipes:
        seen_fetch = False
        for words, _ in pipe:
            name, args = unwrap(words)
            if name in FETCHERS:
                seen_fetch = True
            elif seen_fetch and name in SHELLS:
                feeds = True
            elif seen_fetch and name in INTERPRETERS and all(a == "-" or a.startswith("-") and
                                                            a not in ("-m", "-c", "-e") for a in args):
                feeds = True
    if not feeds:
        return
    trusted = trusted_hosts()
    hosts = [h.lower() for h in URL_HOST.findall(cmd)]
    bad = [h for h in hosts if not any(h == t or h.endswith("." + t) for t in trusted)]
    if bad or not hosts:
        block("piping a download from %s straight into a shell; download it, read it, then run it"
              % (", ".join(sorted(set(bad))) or "an unknown host"))


def check(cmd, depth=0):
    if depth > MAX_DEPTH or not cmd.strip():
        return
    for rx in FORK_BOMB:
        if rx.search(cmd):
            block("fork bomb")
    for inner in substitutions(strip_heredocs(cmd)):  # $(...) and `...` run even inside "..."
        check(inner, depth + 1)
    pipes = pipelines(tokenize(cmd))
    check_remote_exec(cmd, pipes)
    for pipe in pipes:
        for words, redirs in pipe:
            name, args = unwrap(words)
            check_segment(name, args, redirs, depth)


def verdict(cmd):
    """Return None if allowed, else the reason."""
    _CWD[0] = None
    try:
        check(cmd)
    except Blocked as e:
        return str(e)
    return None


def verdict_for_payload(payload):
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        return "tool_input is not an object"
    cmd = ti.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(shlex.quote(str(c)) for c in cmd)
    if isinstance(cmd, str):
        return verdict(cmd)
    for key in ("file_path", "path", "notebook_path"):
        p = ti.get(key)
        if isinstance(p, str) and (SECRET.search(p) and not p.endswith(".pub")
                                   or ENV_FILE.search(p) and not ENV_OK.search(p)):
            return "%s on a credential file (%s)" % (tool or "tool", p)
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--check":
        reason = verdict(" ".join(argv[1:]))
    else:
        try:
            payload = json.loads(sys.stdin.read())
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            reason = verdict_for_payload(payload)
        except (ValueError, UnicodeDecodeError) as e:
            reason = "hook payload could not be parsed (%s); failing closed" % e
    if reason:
        sys.stderr.write("agent-harness guard blocked this: %s. If it is really intended, "
                         "ask the human to run it themselves.\n" % reason)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
